#!/bin/bash
# runner 容器入口：把一次性登记凭据安全地交给 runner 程序。参数已给 --jitconfig 则原样放行；
# 否则等启动器把凭据拷成文件（默认 /opt/runner/.jitconfig），读完立刻删。凭据不走命令行参数、
# 环境变量、tmpfs 或 stdin：会进宿主 ps / 容器 config.json，或根本传不进去。
set -euo pipefail

JIT_FILE="${PRR_JIT_FILE:-/opt/runner/.jitconfig}"

if [ "${1:-}" = "--jitconfig" ]; then
    exec /opt/runner/run.sh "$@"
fi

for _ in $(seq 1 "${PRR_JIT_WAIT_SECONDS:-90}"); do
    [ -s "$JIT_FILE" ] && break
    sleep 1
done

if [ ! -s "$JIT_FILE" ]; then
    echo "没等到登记凭据（${JIT_FILE}）：启动器是不是没拷进来？" >&2
    exit 64
fi

JIT="$(cat "$JIT_FILE")"
rm -f "$JIT_FILE"        # 读完立刻抹掉

exec /opt/runner/run.sh --jitconfig "$JIT" "$@"
