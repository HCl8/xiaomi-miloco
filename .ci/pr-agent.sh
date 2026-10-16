#!/usr/bin/env bash
# pr-agent：给非交互 CI 用的一层薄包装。
# - 把 PR_AGENT_MODEL 映射到底层 CLI 认的那几个模型环境变量
# - 用 dontAsk（只执行 allow 名单里的命令）+ --tools 白名单，做纵深防御
# - 跑底层 CLI 的 stream-json 输出，再用 jq 转成可读行
# 用法: pr-agent "<prompt>"
set -eo pipefail
export PATH="/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
if [ -n "${PR_AGENT_MODEL:-}" ]; then
    export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$PR_AGENT_MODEL}"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$PR_AGENT_MODEL}"
fi
# 单条 Bash 命令有长度上限（实测约 1.2 KB 以上一律拒绝），长正文一条命令发不出去。
# 所以提示它走"写文件 + gh api 读文件"。Write 只在 /tmp 下被放行（见 pr-review-gate.sh 里的
# 权限规则，路径规则要写成 Edit(...) 才生效 —— Write(...) 形式的规则不会被匹配）。
HINT="投递长文本（PR 评论正文等）时：先写到 /tmp 下的文件，再用 gh api -F body=@<文件> 发送；"
HINT="$HINT 单条 Bash 命令过长会被拒绝执行，不要试图把整篇正文塞进一条命令。"
# 降级模式要落在**报告自己的落款**上（末尾 <sub> 行里的版本号后面），不只是系统提示——
# 报告是给人看的，读到它的人得知道自己看的是降级模型的结论。
[ -n "${PR_AGENT_FALLBACK_USED:-}" ] &&
  HINT="$HINT 本次是降级模型：报告末尾 <sub> 行里的版本号后追加 (降级模式)。"

# 门禁启动前会把白名单写进 .claude/settings.json，但**非交互（-p）模式下项目级 settings
# 不会被自动加载** —— 实测那份文件里的规则完全不参与判定，真正生效的只有 skill 自己的
# allowed-tools（所以白名单外的 `gh issue list`、`git ls-tree` 会被拒，而它们本该被
# 门禁的 `Bash(gh *)` / `Bash(git *)` 覆盖）。显式 --settings 喂给它，走 CLI 这条确定生效的路。
SETTINGS_FILE="${PRR_SETTINGS_FILE:-$PWD/.claude/settings.json}"
SETTINGS_ARGS=()
if [ -r "$SETTINGS_FILE" ]; then
    SETTINGS_ARGS=(--settings "$SETTINGS_FILE")
fi

stdbuf -oL claude \
  --permission-mode dontAsk \
  --tools "Bash,Read,Glob,Grep,Write" \
  "${SETTINGS_ARGS[@]}" \
  --append-system-prompt "$HINT" \
  --verbose --output-format stream-json -p "$@" \
  | jq --unbuffered -rc '
      def T: if length>300 then .[:300]+"…" else . end;
      if .type=="assistant" then .message.content[]?
        | if .type=="text" then "[assistant] "+.text
          elif .type=="tool_use" then "[tool_use] "+.name+": "+(.input|tostring|T)
          else empty end
      elif .type=="user" then .message.content[]?
        | if .type=="tool_result" then "[tool_result] "+((.content|if type=="string" then . else tostring end)|T)
          else empty end
      elif .type=="result" then "[DONE] cost=$"+((.total_cost_usd//0)|tostring)+", turns="+((.num_turns//0)|tostring)
      else empty end'
