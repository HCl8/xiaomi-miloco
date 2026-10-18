#!/usr/bin/env bash
# PR 审查 + 严重度门禁的单一实现，pr-review.yml / pr-review-command.yml 共用。
# 两条 workflow 都以 `git show origin/main:.ci/pr-review-gate.sh | bash -s -- <pr> <repo>`
# 调用，始终跑 base 上的本脚本：fork PR 改不动它，严重度正则也只有一处。
#
# 安全模型：调用方 checkout 的树可能是不可信 fork PR，审查 CLI 会自动加载 CLAUDE.md、
# .claude/settings.json（hooks=RCE）、.mcp.json（MCP server=RCE），.claude/commands/ 下的
# 文件会被当命令执行。故启动前清掉这些文件，只从 base 取回可信版本；调用前须已 fetch origin/main。
#
# 用法: pr-review-gate.sh <pr-number> <repo-slug>
# 退出码: 0=放行（无严重/重要问题，或 infra 故障放行不阻塞）, 1=发现严重或重要问题
set -uo pipefail

PR_NUMBER="$1"
REPO="$2"

# 取可信文件依赖 origin/main；调用方通常已 fetch，这里保底一次以支持脚本独立运行。
git fetch --no-tags origin main 2>/dev/null || true

# 未配置时 CI 会注入空串，空 base URL 会被 SDK 当成无效值；清掉以回退默认端点。
if [ -z "${ANTHROPIC_BASE_URL:-}" ]; then unset ANTHROPIC_BASE_URL; fi

# 清不可信配置，从 base 取回可信版本（见顶部安全模型）
find . \( -name 'CLAUDE.md' -o -name 'CLAUDE.local.md' \) -print0 | xargs -0 -r rm -f
rm -rf .claude .mcp.json
git show origin/main:CLAUDE.md > CLAUDE.md 2>/dev/null || true
mkdir -p .claude/commands
git show "origin/main:.agents/commands/review-pr.md" \
  | sed "s#XiaoMi/xiaomi-miloco#${REPO}#g" \
  > .claude/commands/review-pr.md

# 写死 Bash 子命令白名单：dontAsk 下只有 permissions.allow 内的命令能执行，其余自拒。
# 这份 settings 不会被自动加载，必须由 .ci/pr-agent.sh 用 --settings 显式喂给 CLI。
# Edit(//tmp/**) 放行所有写文件工具在 /tmp 的写入（路径规则只认 Edit(...)，Write(...) 永不匹配）；
# 长正文超过单条命令上限，只能落文件再 gh api -F body=@文件，故等于"只许写 /tmp"。
cat > .claude/settings.json <<'SETEOF'
{"permissions": {"allow": ["Bash(jq *)", "Bash(gh *)", "Bash(git *)", "Bash(md5sum *)", "Bash(diff *)", "Edit(//tmp/**)"]}}
SETEOF

# 跑审查：主模型失败时降级到备用模型/endpoint 重试一次；主备均失败则阻断 merge。
# < /dev/null 必须：脚本经 bash stdin 执行，-p 模式会读 stdin 当输入并吞掉后续门禁脚本，
# 导致整段门禁被静默跳过、恒放行。
# 跑审查前先记下现有 review-pr-ci 评论的更新时间：这条评论是复用的，"本次没产出"和
# "产出了结论"只看 body 无法区分，不比对就会把旧结论当本次结果（假绿/假红）。
comment_stamp() {
  gh api "/repos/$REPO/issues/$PR_NUMBER/comments" --paginate 2>/dev/null \
    | jq -rs 'add | [.[] | select((.body // "") | startswith("<!-- review-pr-ci -->"))] | last | (.updated_at // "")' 2>/dev/null \
    || echo ""
}
STAMP_BEFORE="$(comment_stamp)"

REVIEW_OK=0
if /usr/local/bin/pr-agent "/review-pr $PR_NUMBER --ci" < /dev/null; then
  REVIEW_OK=1
elif [ -n "${ANTHROPIC_FALLBACK_API_KEY:-}" ] && [ -n "${PR_AGENT_FALLBACK_MODEL:-}" ]; then
  echo "[WARN] 主模型不可用，尝试降级到备用模型"
  export ANTHROPIC_API_KEY="$ANTHROPIC_FALLBACK_API_KEY"
  [ -n "${ANTHROPIC_FALLBACK_BASE_URL:-}" ] && export ANTHROPIC_BASE_URL="$ANTHROPIC_FALLBACK_BASE_URL"
  export PR_AGENT_MODEL="$PR_AGENT_FALLBACK_MODEL"
  export PR_AGENT_FALLBACK_USED=1
  if /usr/local/bin/pr-agent "/review-pr $PR_NUMBER --ci" < /dev/null; then
    REVIEW_OK=1
  fi
fi
# 主备模型同时不可用（或未配 ANTHROPIC_FALLBACK_* secrets）时，下面的 exit 1 会让
# pr-review 这条 required check 变红、阻断全仓 merge。
if [ "$REVIEW_OK" -ne 1 ]; then
  echo "[FAIL] 审查执行失败（主备均不可用），阻断 merge"
  exit 1
fi

# gh api 瞬断时回退空串，交给下面的判空分支阻断。
NOTE_BODY=$(gh api "/repos/$REPO/issues/$PR_NUMBER/comments" --paginate \
  | jq -rs 'add | .[] | select((.body // "") | startswith("<!-- review-pr-ci -->")) | .body') || NOTE_BODY=""
if [ -z "$NOTE_BODY" ]; then
  echo "[FAIL] 未找到 review-pr-ci 评论（审查结果未产出），阻断 merge"
  exit 1
fi

# 评论必须被本次审查更新过；没更新说明读到的就是旧结论，不能当本次结果用。
STAMP_AFTER="$(comment_stamp)"
if [ -z "$STAMP_AFTER" ] || [ "$STAMP_AFTER" = "$STAMP_BEFORE" ]; then
  echo "[FAIL] review-pr-ci 评论未被本次审查更新（本次结果未产出，读到的会是旧结论），阻断 merge"
  exit 1
fi

# 锚定小节标题，避免概述里的提法被误判（review-pr 约定 #### 开头，留 1~4 个 # 余量）。
if echo "$NOTE_BODY" | grep -qE '^#{1,4} .*(🔴 严重|🟡 重要)'; then
  echo "[FAIL] Review 发现严重或重要问题"
  exit 1
fi
echo "[PASS] 未发现严重或重要问题"
exit 0
