#!/usr/bin/env bash
# 审查 + 门禁 + 打状态的共用实现，供 /review 的自托管与云端两条路径共用。
#
# 不做成本地 composite action：issue_comment 的 job checkout 的是 refs/pull/<n>/head，
# 即 PR 那一侧的代码，本地 action 会从那里加载，等于让 PR 决定执行什么。故与门禁脚本
# 一样从 origin/main 取（调用方已 checkout 并 fetch）。
#
# 用法: git show origin/main:.ci/pr-review-run.sh | bash -s -- <pr> <head-sha> <repo>
# 退出码: 0 = 放行 / 1 = 有问题或执行失败（与门禁一致）
set -uo pipefail

PR_NUMBER="$1"
HEAD_SHA="$2"
REPO="$3"

# 钉死 head：维护者背书的是触发那一刻的 commit，审查也必须审它（防触发后换包）
GOT="$(git rev-parse HEAD)"
if [ "$GOT" != "$HEAD_SHA" ]; then
  echo "::error::PR head 从触发时的 $HEAD_SHA 变为 ${GOT}，疑似触发后改包，已中止"
  exit 1
fi

# 容器 / 不同 runner 的用户不同，工作区所有权会对不上
git config --global --add safe.directory "$GITHUB_WORKSPACE" 2>/dev/null || true
git fetch --no-tags origin main 2>/dev/null || true

STATE=failure
DESC="发现严重/重要问题"
# 跑 base 分支上的门禁（fork PR 改不动它；脚本内部还会清掉 PR 树里的不可信配置）
if git show origin/main:.ci/pr-review-gate.sh | bash -s -- "$PR_NUMBER" "$REPO"; then
  STATE=success
  DESC="未发现严重/重要问题"
fi

# issue_comment / 自托管 job 都不会自动成为 PR 的 check；打到 head SHA 的 commit status 上，
# 才能显示在 PR 里、也才能在分支保护里当 required check
gh api "repos/$REPO/statuses/$HEAD_SHA" \
  -f state="$STATE" -f context="pr-review" -f description="$DESC" >/dev/null

[ "$STATE" = success ]
