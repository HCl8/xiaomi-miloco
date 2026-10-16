#!/bin/bash
# runner 容器的入口包装。
#
# 存在的唯一理由：把一次性登记凭据**从环境变量**转成 `--jitconfig` 参数。
# 直接 `container run ... --jitconfig <blob>` 会让凭据出现在宿主机的进程命令行里
# （`ps aux` 谁都能看）；走环境变量继承（`container run -e KEY` 不带值）就不会。
#
# runner 程序本身不认这个环境变量（在 Runner.Listener 里找不到对应常量），
# 所以转换只能由这层做。转换后立刻把变量从环境里抹掉，
# 免得它顺着环境传给任务里跑的每一步。
set -euo pipefail

args=("$@")

if [ -n "${PRR_JITCONFIG:-}" ]; then
    jit="$PRR_JITCONFIG"
    unset PRR_JITCONFIG
    args=(--jitconfig "$jit" "${args[@]}")
fi

exec /opt/runner/run.sh "${args[@]}"
