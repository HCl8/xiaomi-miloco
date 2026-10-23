#!/bin/bash
# 装三个 LaunchAgent：com.prreview.gateway（一个进程两个端口）、com.prreview.launcher，
# 以及 com.prreview.container-system（登录后把 Apple container 的服务拉起来）。
# 网关与启动器分成两个进程是因为寿命不同：启动器频繁重启会掐断在飞请求，网关不该陪它一起重启。
# 幂等：重复跑等于重装。凭据文件不在这里创建，见 README。
set -euo pipefail

PRR_DIR="${PRR_DIR:-$HOME/prreview}"
CONF="${PRR_CONF:-$PRR_DIR/prreview.json}"
AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
PY=/usr/bin/python3
CONTAINER_BIN=/usr/local/bin/container

say() { printf '  %s\n' "$*"; }

# 端口从配置读：写死的话改了配置自检会敲没人听的端口，红/绿都与真实故障无关。
cfg_get() {     # cfg_get <键> <默认值>
    CONF="$CONF" KEY="$1" DEF="${2:-}" python3 -c '
import json, os
try:
    v = json.load(open(os.environ["CONF"])).get(os.environ["KEY"])
except Exception:
    v = None
print(v if v not in (None, "") else os.environ["DEF"])
' 2>/dev/null
}
GH_PORT="$(cfg_get github_proxy_port 18080)"
MODEL_PORT="$(cfg_get model_proxy_port 18081)"

[ -x "$CONTAINER_BIN" ] || { echo "找不到 Apple container CLI（${CONTAINER_BIN}），先装它" >&2; exit 1; }
[ -r "$CONF" ] || { echo "找不到配置 $CONF" >&2; exit 1; }

mkdir -p "$AGENTS" "$PRR_DIR/logs"

write_agent() {  # write_agent <label> <plist路径> <可执行> <参数...>
    local label="$1" plist="$2"; shift 2
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        echo '<plist version="1.0"><dict>'
        echo "  <key>Label</key><string>$label</string>"
        echo '  <key>ProgramArguments</key><array>'
        for a in "$@"; do
            # 参数里的 & < > 要转义，否则 plist 不合法
            esc=$(printf '%s' "$a" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
            echo "    <string>$esc</string>"
        done
        echo '  </array>'
        echo '  <key>RunAtLoad</key><true/>'
        echo '  <key>KeepAlive</key><true/>'
        echo '  <key>ThrottleInterval</key><integer>30</integer>'
        # TERM 之后要给足收尾时间：启动器每次都要杀掉并删掉它管的容器，默认宽限期会强杀并留下孤儿
        echo '  <key>ExitTimeOut</key><integer>60</integer>'
        echo "  <key>StandardOutPath</key><string>$PRR_DIR/logs/$label.out</string>"
        echo "  <key>StandardErrorPath</key><string>$PRR_DIR/logs/$label.err</string>"
        echo '</dict></plist>'
    } > "$plist"
}

# Apple container 的服务（apiserver）不是登录项：机器重启或重新登录之后不会自己回来，
# 而启动器的出口自检要起容器，于是会一直报 XPC connection error、拒绝干活。
# 这条只在登录时跑一次就退出，所以不能沿用上面的 KeepAlive=true（那会变成每 30 秒重启一次，
# 永远起不去），改成只在失败时重试。
write_container_agent() {  # write_container_agent <plist路径>
    local plist="$1"
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        echo '<plist version="1.0"><dict>'
        echo '  <key>Label</key><string>com.prreview.container-system</string>'
        echo '  <key>ProgramArguments</key><array>'
        echo "    <string>$CONTAINER_BIN</string>"
        echo '    <string>system</string><string>start</string>'
        echo '  </array>'
        echo '  <key>RunAtLoad</key><true/>'
        echo '  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>'
        echo '  <key>ThrottleInterval</key><integer>30</integer>'
        echo "  <key>StandardOutPath</key><string>$PRR_DIR/logs/com.prreview.container-system.out</string>"
        echo "  <key>StandardErrorPath</key><string>$PRR_DIR/logs/com.prreview.container-system.err</string>"
        echo '</dict></plist>'
    } > "$plist"
}

bootstrap() {
    local label="$1" plist="$2"
    cp "$plist" "$AGENTS/$label.plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # 必须等它真从域里消失再 bootstrap，否则会撞 "Bootstrap failed: 5" 且不易发现。
    # 启动器收干净它管的容器可能要十几秒。
    for _ in $(seq 1 40); do
        launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
        sleep 1
    done
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
    say "已装 $label"
}

# 旧的两个代理 agent 会和新网关抢同一端口：停掉并删干净
for old in com.prreview.github-proxy com.prreview.model-proxy; do
    if [ -e "$AGENTS/$old.plist" ]; then
        launchctl bootout "$DOMAIN/$old" 2>/dev/null || true
        rm -f "$AGENTS/$old.plist"
        say "清掉旧的 $old"
    fi
done

echo "== 装 container 服务自启 =="
write_container_agent "$PRR_DIR/logs/container-system.plist"
bootstrap com.prreview.container-system "$PRR_DIR/logs/container-system.plist"

echo "== 装网关（两个端口一个进程）=="
write_agent com.prreview.gateway "$PRR_DIR/logs/gateway.plist" \
    "$PY" "$PRR_DIR/gateway.py" --config "$CONF"
bootstrap com.prreview.gateway "$PRR_DIR/logs/gateway.plist"

echo "== 装启动器 =="
if [ -r "$PRR_DIR/secrets/github-token" ]; then
    write_agent com.prreview.launcher "$PRR_DIR/logs/launcher.plist" \
        "$PY" "$PRR_DIR/launcher.py" --config "$CONF"
    bootstrap com.prreview.launcher "$PRR_DIR/logs/launcher.plist"
else
    echo "  跳过：缺 secrets/github-token（能申请 runner 登记凭据的那把凭据）"
    echo "  放好之后再跑一次本脚本即可。"
fi

echo
echo "== 自检 =="
sleep 4
printf "  container 服务: "
"$CONTAINER_BIN" system status 2>/dev/null | awk '$1=="status"{print $2}' || echo "取不到（起容器会失败）"
printf "  网关 %s 转发 GitHub: " "$GH_PORT"
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 -x "http://127.0.0.1:$GH_PORT" https://api.github.com || echo "  连不上"
printf "  网关 %s 模型存活探针: " "$MODEL_PORT"
curl -s --max-time 8 "http://127.0.0.1:$MODEL_PORT/healthz"; echo
echo
echo "== 当前状态 =="
launchctl list 2>/dev/null | grep -E "prreview" | sed 's/^/  /' || echo "  （无）"
