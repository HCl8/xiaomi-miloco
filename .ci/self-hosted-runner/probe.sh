#!/bin/bash
# 隔离探针：把"任务跑在一次性容器里、容器碰不到内网和公网"这几条说法逐条实测。
# 断言本体只有这一份 —— .github/workflows/runner-isolation-probe.yml 检出仓库后直接调本脚本，
# 不再内联一份副本。曾经是两份，漂移过一次（同一台机器、同一个坏状态，一边打通过一边 FAIL）。
# 手工跑也是它：bash probe.sh（明细直接看得到）。
fail=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
tcp() { timeout 6 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

HOST_GW="${HOST_GW:-192.168.100.1}"
GH_PROXY_PORT="${GH_PROXY_PORT:-18080}"
MODEL_PROXY_PORT="${MODEL_PROXY_PORT:-18081}"   # 显式设成空串才会跳过这条（见下面的 SKIP）

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
if [ -n "$MODEL_PROXY_PORT" ]; then
  tcp "$HOST_GW" "$MODEL_PROXY_PORT" \
    && ok "能到宿主机上的模型代理 $HOST_GW:$MODEL_PROXY_PORT" \
    || bad "到不了模型代理"
else
  # 显式打一条 SKIP。不给端口就整条不出现的话，手工跑会静默少一条断言，
  # 看着还是"结论：全部通过"。
  printf '  \033[33mSKIP\033[0m  没给 MODEL_PROXY_PORT，这条没测（workflow 里会给 18081）\n'
fi

echo "================ 6. 到不了别的地方 ================"
tcp github.com 443 \
  && bad "直连 github.com:443 通了（应该没有路由）" \
  || ok "直连 github.com:443 不通"
# 靶子默认用 RFC 5737 的保留地址（本就没有路由）。真实部署可以设 PRR_INTRANET_TARGET
# 指向一台内网机器来加强这条断言 —— 但别把那个地址写进脚本，这个是公开仓的文件。
INTRANET_TARGET="${PRR_INTRANET_TARGET:-192.0.2.1}"
tcp "$INTRANET_TARGET" 443 \
  && bad "$INTRANET_TARGET:443 通了（有路由泄漏！）" \
  || ok "$INTRANET_TARGET:443 不通"
tcp example.com 80 \
  && bad "example.com:80 通了（公网可达！）" \
  || ok "公网 example.com:80 不通"

echo "======================================================="
[ "$fail" -eq 0 ] && echo "结论：全部通过" || echo "结论：有断言失败"
exit "$fail"
