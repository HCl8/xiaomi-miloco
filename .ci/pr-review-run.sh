#!/usr/bin/env bash
# 审查 + 门禁 + 打状态的共用实现，自动审查与 /review 两条路径共用。
#
# 不做成本地 composite action：job checkout 的是 PR 那一侧的代码，本地 action 会从
# 那里加载，等于让 PR 决定执行什么。故与门禁脚本一样从 origin/main 取。
#
# 用法: git show origin/main:.ci/pr-review-run.sh | bash -s -- <pr> <head-sha> <repo>
# 退出码: 0 = 放行 / 1 = 没放行（具体是「有问题」还是「链路故障」看 status 的 description）
set -uo pipefail

PR_NUMBER="$1"
HEAD_SHA="$2"
REPO="$3"

# 容器 / 不同 runner 的用户不同，工作区所有权会对不上
git config --global --add safe.directory "$GITHUB_WORKSPACE" 2>/dev/null || true

# 审的是**合并结果**（base 与 PR head 合出来的提交），base 前进带出的冲突也会落进审查。
# 有冲突时这个 ref 不存在 —— 没有合并结果可审，直接报错，审 head 没有意义。
if ! merge_err="$(git fetch --no-tags origin "refs/pull/$PR_NUMBER/merge" 2>&1)"; then
  case "$merge_err" in
    *"couldn't find remote ref"*)
      echo "::error::PR 有冲突，取不到合并结果（refs/pull/$PR_NUMBER/merge），本次不审查；请先解决冲突" ;;
    *)
      echo "::error::取不到合并结果，原因不是冲突：${merge_err}" ;;
  esac
  exit 1
fi
if ! git checkout --detach FETCH_HEAD >/dev/null 2>&1; then
  echo "::error::取到了合并结果却切不过去"
  exit 1
fi
GOT="$(git rev-parse --verify -q HEAD^2 || true)"   # 合并提交的第二个父就是 PR head
if [ "$GOT" != "$HEAD_SHA" ]; then
  echo "::error::PR head 从触发时的 $HEAD_SHA 变为 ${GOT:-（取不到）}，疑似触发后改包，已中止"
  exit 1
fi

git fetch --no-tags origin main 2>/dev/null || true

# 跑 base 分支上的门禁（PR 改不动它；脚本内部还会清掉 PR 树里的不可信配置）。
# 退出码：0 放行 / 1 发现严重或重要问题 / 2 基础设施故障（没产出结论）。
# 取不到门禁脚本时不能当成"审查通过"，也不能当成"发现问题" —— 那是链路问题
if ! gate_src="$(git show origin/main:.ci/pr-review-gate.sh 2>/dev/null)"; then
  echo "::error::取不到门禁脚本（origin/main:.ci/pr-review-gate.sh）"
  STATE=error
  DESC="审查链路故障，未产出结论"
else
  printf '%s' "$gate_src" | bash -s -- "$PR_NUMBER" "$REPO"
  case $? in
    0) STATE=success; DESC="未发现严重/重要问题" ;;
    2) STATE=error;   DESC="审查链路故障，未产出结论" ;;
    *) STATE=failure; DESC="发现严重/重要问题" ;;
  esac
fi

# issue_comment / 自托管 job 都不会自动成为 PR 的 check；打到 head SHA 的 commit status 上，
# 才能显示在 PR 里、也才能在分支保护里当 required check
gh api "repos/$REPO/statuses/$HEAD_SHA" \
  -f state="$STATE" -f context="pr-review" -f description="$DESC" >/dev/null

[ "$STATE" = success ]
