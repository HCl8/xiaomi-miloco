#!/bin/bash
# 隔离探针：逐条验证"任务跑在一次性容器里、碰不到内网和公网"。
# 断言本体只有这一份：runner-isolation-probe.yml 检出后直接调本脚本，不再内联副本。
# 手工跑：bash probe.sh。
fail=0
skipped=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; skipped=$((skipped + 1)); }
tcp() { timeout 6 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

HOST_GW="${HOST_GW:-192.168.100.1}"
GH_PROXY_PORT="${GH_PROXY_PORT:-18080}"
MODEL_PROXY_PORT="${MODEL_PROXY_PORT:-18081}"

# 这三个值在配置里是单一来源，这里是容器里的第二份（读不到宿主配置）。启动器把它们注入成
# 环境变量，拿它对一次：对不上说明取值漂了，别报成"到不了代理"那种与真实故障无关的红。
hostport() { printf '%s' "$1" | sed -E 's#^[a-z]+://##; s#/.*$##'; }
check_injected() {   # check_injected <变量名> <值> <期望的 host:port>
    [ -n "$2" ] || return 0
    [ "$(hostport "$2")" = "$3" ] && return 0
    printf '\n!! 靶子与启动器注入的不一致：%s=%s，脚本默认 %s\n' "$1" "$2" "$3" >&2
    printf '!! 改 probe.sh 顶部的默认值，或 export HOST_GW / GH_PROXY_PORT / MODEL_PROXY_PORT 后重跑。\n' >&2
    exit 2
}
check_injected HTTP_PROXY "${HTTP_PROXY:-}" "$HOST_GW:$GH_PROXY_PORT"
check_injected ANTHROPIC_BASE_URL "${ANTHROPIC_BASE_URL:-}" "$HOST_GW:$MODEL_PROXY_PORT"

echo "================ 1. 任务确实跑在容器里 ================"
[ "$(uname -s)" = "Linux" ] \
  && ok "内核是 Linux（宿主是 macOS，说明任务不在宿主上）" \
  || bad "内核不是 Linux：$(uname -s)"
echo "     hostname=$(hostname)  kernel=$(uname -r)  user=$(id -un)/$(id -u)"

echo "================ 2. 看不见宿主机 ================"
{ [ -e /Users ] || [ -e /System ]; } \
  && bad "能看到 macOS 的目录（/Users 或 /System 存在）" \
  || ok "看不到 macOS 的文件系统"
host_ssh=""
for h in /Users/*/.ssh /home/*/.ssh; do
  [ -r "$h" ] && { host_ssh="$h"; break; }
done 2>/dev/null || true
[ -n "$host_ssh" ] \
  && bad "能读到宿主机用户目录 $host_ssh" \
  || ok "读不到宿主机的用户目录"

echo "================ 3. 没有容器运行时接口 ================"
found=""
for s in /var/run/docker.sock /run/docker.sock \
         /run/containerd/containerd.sock /var/run/podman/podman.sock; do
  [ -e "$s" ] && found="$found $s"
done
[ -z "$found" ] && ok "没有可用的容器运行时 socket" || bad "发现 socket:$found"

echo "================ 4. 任务之间互不可见 ================"
residue=""
for m in /tmp/prr-probe-marker "$HOME/.prr-probe-marker"; do
  [ -e "$m" ] && residue="$residue $m"
done
[ -z "$residue" ] && ok "没有上一次任务留下的残留" || bad "看到残留:${residue}（容器没一次性销毁？）"
date > "$HOME/.prr-probe-marker" 2>/dev/null
date > /tmp/prr-probe-marker 2>/dev/null
ok "已埋下本次标记（下一次在别的容器里跑，应当看不到它）"

echo "================ 5. 唯一的出口是宿主机 ================"
tcp "$HOST_GW" "$GH_PROXY_PORT" \
  && ok "能到宿主机上的 GitHub 转发代理 $HOST_GW:$GH_PROXY_PORT" \
  || bad "到不了 GitHub 转发代理"
tcp "$HOST_GW" "$MODEL_PROXY_PORT" \
  && ok "能到宿主机上的模型代理 $HOST_GW:$MODEL_PROXY_PORT" \
  || bad "到不了模型代理"

echo "================ 6. 到不了别的地方 ================"
# 靶子必须是字面公网 IP：用主机名要先解析，"解析不出来"与"连不上"结果一样，断言恒绿。
tcp 1.1.1.1 443 \
  && bad "直连 1.1.1.1:443 通了（公网可达！）" \
  || ok "直连 1.1.1.1:443 不通（字面公网 IP，不经 DNS）"
tcp 8.8.8.8 53 \
  && bad "直连 8.8.8.8:53 通了（公网可达！）" \
  || ok "直连 8.8.8.8:53 不通（字面公网 IP，不经 DNS）"

# DNS 本身也该不可用：解析**成功**才是问题（说明有没料到的出网路径）。
resolve() { getent hosts "$1" 2>/dev/null | awk '{print $1; exit}'; }
if [ -n "$(resolve github.com)" ]; then
  bad "容器里能解析 github.com —— DNS 通了，说明有没料到的出网路径"
else
  ok "容器里解析不了 github.com（没有可用的 DNS）"
fi

# 不给内网靶子就 SKIP：不能拿恒不可达的地址充数，那等于没测。真实部署用 PRR_INTRANET_TARGET
# 指一台内网机器来真正加强它（别把那个地址写进脚本，这是公开仓的文件）。
if [ -n "${PRR_INTRANET_TARGET:-}" ]; then
  tcp "$PRR_INTRANET_TARGET" 443 \
    && bad "$PRR_INTRANET_TARGET:443 通了（有路由泄漏！）" \
    || ok "$PRR_INTRANET_TARGET:443 不通"
else
  skip "没给 PRR_INTRANET_TARGET，内网可达性这条没测"
fi

echo "======================================================="
# "全部通过"不能说满：SKIP 的断言一条证据都没产生，混进"通过"会让人以为覆盖得比实际多。
if [ "$fail" -ne 0 ]; then
  echo "结论：有断言失败"
elif [ "$skipped" -ne 0 ]; then
  echo "结论：没有失败，但有 $skipped 条没测到东西（见上面 SKIP）"
else
  echo "结论：全部通过"
fi
exit "$fail"
