#!/bin/bash
# 装两个 LaunchAgent（原来是三个，两个代理已合成一个网关进程）：
#   1. com.prreview.gateway   宿主机出网网关（一个进程两个端口：GitHub 转发 + 模型代理）
#   2. com.prreview.launcher  一次性 runner 启动器
#
# 网关和启动器分成两个进程的理由：寿命不同。启动器会频繁重启（部署、改代码），
# 每次重启都会掐断正在飞的请求；网关重启的代价只是几十毫秒的 502。两者不该互相承担。
#
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

[ -x "$CONTAINER_BIN" ] || { echo "找不到 Apple container CLI（$CONTAINER_BIN），先装它" >&2; exit 1; }
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
        # 收到 TERM 之后要给足时间收尾：启动器要杀掉并删掉它管的每一个容器，
        # 默认宽限期不够，会被强杀 → 留下一批孤儿容器
        echo '  <key>ExitTimeOut</key><integer>60</integer>'
        echo "  <key>StandardOutPath</key><string>$PRR_DIR/logs/$label.out</string>"
        echo "  <key>StandardErrorPath</key><string>$PRR_DIR/logs/$label.err</string>"
        echo '</dict></plist>'
    } > "$plist"
}

bootstrap() {
    local label="$1" plist="$2"
    cp "$plist" "$AGENTS/$label.plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # 必须等它真的从域里消失再 bootstrap —— 只 sleep 一秒会撞上 "Bootstrap failed: 5:
    # Input/output error"，服务起不来还不容易看出来。
    # 启动器要收干净它管的容器，可能花十几秒。
    for _ in $(seq 1 40); do
        launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
        sleep 1
    done
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
    say "已装 $label"
}

# 旧的两个代理 agent（已被 gateway 取代）：停掉并删干净，免得它们和新网关抢同一个端口
for old in com.prreview.github-proxy com.prreview.model-proxy; do
    if [ -e "$AGENTS/$old.plist" ]; then
        launchctl bootout "$DOMAIN/$old" 2>/dev/null || true
        rm -f "$AGENTS/$old.plist"
        say "清掉旧的 $old"
    fi
done

echo "== 装网关（两个端口一个进程）=="
write_agent com.prreview.gateway "$PRR_DIR/logs/gateway.plist" \
    "$PY" "$PRR_DIR/gateway.py" --config "$CONF"
bootstrap com.prreview.gateway "$PRR_DIR/logs/gateway.plist"

echo "== 装启动器 =="
if [ -r "$PRR_DIR/secrets/github-token" ]; then
    write_agent com.prreview.launcher "$PRR_DIR/logs/launcher.plist" \
        "$PY" "$PRR_DIR/launcher.py"
    bootstrap com.prreview.launcher "$PRR_DIR/logs/launcher.plist"
else
    echo "  跳过：缺 secrets/github-token（能申请 runner 登记凭据的那把凭据）"
    echo "  放好之后再跑一次本脚本即可。"
fi

echo
echo "== 自检 =="
sleep 4
printf "  网关 18080 转发 GitHub: "
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 -x http://127.0.0.1:18080 https://api.github.com || echo "  连不上"
printf "  网关 18081 模型存活探针: "
curl -s --max-time 8 http://127.0.0.1:18081/healthz; echo
echo
echo "== 当前状态 =="
launchctl list 2>/dev/null | grep -E "prreview" | sed 's/^/  /' || echo "  （无）"
