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
stdbuf -oL claude \
  --permission-mode dontAsk \
  --tools "Bash,Read,Glob,Grep" \
  ${PR_AGENT_FALLBACK_USED:+--append-system-prompt "审查报告末尾 <sub> 行里的版本号后追加 (降级模式)"} \
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
