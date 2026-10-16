#!/bin/bash
# runner 容器的入口包装。
#
# 存在的唯一理由：把一次性登记凭据安全地交给 runner 程序。
#
# 凭据怎么进来（两选一）：
#   1. 参数里已经给了 --jitconfig —— 原样放行
#   2. 等启动器把凭据**拷进容器文件系统**（默认 /opt/runner/.jitconfig），读完立刻删
#
# 为什么不用那两种更顺手的写法（都是实测踩出来的）：
#   - `container run ... --jitconfig <blob>`：凭据进宿主机的进程命令行，ps aux 人人可见。
#   - `container run -e PRR_JITCONFIG`：不进命令行，但容器运行时会把它写进
#     ~/Library/Application Support/com.apple.container/containers/<name>/config.json
#     （644 权限，而且容器删掉后记录不删）——落盘还带残留。
#   - 拷进 tmpfs：`container cp` 报告成功，但文件穿不进运行中 VM 的挂载点，容器里根本看不到。
#   - 走 stdin：`container run` 不转发 stdin。
#
# 现在这条是实测最干净的：凭据只在容器的 VM 里存在过，读完即删；
# 宿主侧只在 600 的临时文件里过一下、随即 unlink；容器记录里一个字节都不留。
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
    echo "没等到登记凭据（$JIT_FILE）：启动器是不是没拷进来？" >&2
    exit 64
fi

JIT="$(cat "$JIT_FILE")"
rm -f "$JIT_FILE"        # 读完立刻抹掉

exec /opt/runner/run.sh --jitconfig "$JIT" "$@"
