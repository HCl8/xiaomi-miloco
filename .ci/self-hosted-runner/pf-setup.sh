#!/bin/bash
# 给这套 runner 的专属网段加宿主机侧出口过滤（pf）。
# hostOnly 网络里容器能碰到宿主机所有监听端口，container 侧关不掉，只能在宿主机收口。
# 用法: bash pf-setup.sh [--rollback|--persist]；日志在 ~/prreview/logs/pf-setup.log

set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

SUBNET="${PRR_SUBNET:-192.168.100.0/24}"
# 容器侧看到的宿主机地址，就是这个网段的网关地址。改 SUBNET 时必须一起改，否则下面
# 「必须连不上」的靶子会指向不存在的地址 —— 永远「不通」，block 侧恒判通过。
HOST_GW="${PRR_HOST_GATEWAY:-192.168.100.1}"
# 放行的两个端口必须与网关实际监听一致，所以从配置文件取（与 launcher 同一键）。
# 写死默认值的话，配置改了端口而 pf 只放行旧端口，容器出网会被静默丢掉。
PORTS="${PRR_PORTS:-}"
# 镜像引用也留空、从配置文件取（与 launcher 同一份，单一来源）；写错会让探针起不来容器。
IMAGE="${PRR_IMAGE:-}"
NETWORK="${PRR_NETWORK:-prr-int}"
CONF="${PRR_CONF:-$HOME/prreview/prreview.json}"
ANCHOR=prreview
ANCHOR_FILE="/etc/pf.anchors/$ANCHOR"
PF_CONF=/etc/pf.conf
BACKUP="$PF_CONF.prr-backup"
DAEMON=/Library/LaunchDaemons/com.prreview.pf.plist
CONTAINER=/usr/local/bin/container
LOG_DIR="$HOME/prreview/logs"
LOG="$LOG_DIR/pf-setup.log"

say()   { printf '  %s\n' "$*"; }
head1() { printf '\n=== %s ===\n' "$*"; }
die()   { printf '\n!! 错误: %s\n' "$*"; printf '!! 已中止。回滚：bash %s --rollback\n' "$0"; exit 1; }

mkdir -p "$LOG_DIR"
exec > >(tee "$LOG") 2>&1

# 镜像引用与两个代理端口都从配置文件读（与 launcher 同一份，单一来源），环境变量仍可覆盖。
cfg_get() {     # cfg_get <键> → 值（读不到打空串）
    CONF="$CONF" KEY="$1" python3 -c '
import json, os
try:
    print(json.load(open(os.environ["CONF"])).get(os.environ["KEY"], ""))
except Exception:
    print("")
' 2>/dev/null
}
[ -n "$IMAGE" ] || IMAGE="$(cfg_get image)"
[ -n "$IMAGE" ] || die "没拿到镜像引用：给 PRR_IMAGE，或在 $CONF 里写 image"
[ -n "$PORTS" ] || PORTS="$(cfg_get github_proxy_port), $(cfg_get model_proxy_port)"
printf '%s' "$PORTS" | grep -qE '^[[:space:]]*[0-9]+[[:space:]]*,[[:space:]]*[0-9]+[[:space:]]*$' \
    || die "没拿到有效的代理端口（形如 \"18080, 18081\"）：给 PRR_PORTS，或在 $CONF 里写 github_proxy_port / model_proxy_port"
ALLOW_PORTS="$(printf '%s' "$PORTS" | tr -d ' ' | tr ',' ' ')"   # 给探测与判定用的空格分隔形式

# "必须连不上"的靶子：宿主机上与本方案无关的服务端口、宿主机对外地址、公网。
# 端口和地址都现探，不写死具体值 —— 测的本来就是机器实际暴露了什么。
HOST_OWN_ADDR="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -vE '^127\.' | head -1)"
BLOCK_TARGETS=("1.1.1.1:443")
# 只取「通配」或「就绑在网关地址上」的监听当靶子：绑在回环或别的地址上的端口容器本就碰不到，
# 拿它当靶子永远不通，那条断言等于没做。判据在 prrlib.guard_ports，与 launcher.py 的出口自检共用。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
for _p in $(python3 "$SCRIPT_DIR/prrlib.py" --guard-ports "$HOST_GW" --exclude "$PORTS" 2>/dev/null); do
    BLOCK_TARGETS+=("${HOST_GW}:${_p}")
    [ -n "$HOST_OWN_ADDR" ] && BLOCK_TARGETS+=("${HOST_OWN_ADDR}:${_p}")
done
# 放行端口出现在宿主**别的地址**上时必须不通 —— 规则只放行到 $HOST_GW，这一条守着它。
if [ -n "$HOST_OWN_ADDR" ]; then
    for _p in $ALLOW_PORTS; do BLOCK_TARGETS+=("${HOST_OWN_ADDR}:${_p}"); done
fi

# 探针要连网关的两个端口，所以**必须先把服务装起来再跑本脚本**（README 的装法顺序）。
# 顺序反了的话两个放行靶子必然不通，会把一份正确的规则判成坏的。
[ "$(id -u)" = 0 ] && die "别用 root 跑：脚本内部自己 sudo。容器命令必须用你自己的账号（container 的守护进程是按用户起的）"
command -v "$CONTAINER" >/dev/null || die "找不到 $CONTAINER"

head1 "准备"
echo "  需要 sudo，先验一次密码（缓存窗口内不会再问）："
sudo -v || die "sudo 不可用"
say "sudo OK"

# ---------------------------------------------------------------- 撤掉
if [ "${1:-}" = "--rollback" ]; then
    head1 "撤掉规则"
    sudo pfctl -a "$ANCHOR" -F rules 2>/dev/null && say "anchor 规则已清" || say "anchor 本来就没有"
    sudo rm -f "$DAEMON"
    sudo launchctl bootout system/com.prreview.pf 2>/dev/null
    if [ -f "$BACKUP" ]; then
        sudo cp "$BACKUP" "$PF_CONF" && say "pf.conf 已还原成备份那份"
    else
        sudo sed -i '' "/anchor \"$ANCHOR\"/d; /load anchor \"$ANCHOR\"/d" "$PF_CONF" && say "从 pf.conf 里删掉了引用"
    fi
    sudo pfctl -f "$PF_CONF" && say "已重新加载（原规则集回来）" || say "→ 重新加载失败，上面是报错"
    say "完成。pf 本身仍处于启用状态（它原本就是开着的）"
    exit 0
fi

# ---------------------------------------------------------------- 持久化
if [ "${1:-}" = "--persist" ]; then
    head1 "装 LaunchDaemon（开机启用 pf 并加载规则）"
    [ -s "$ANCHOR_FILE" ] || die "$ANCHOR_FILE 不存在 —— 先把规则装好再来"
    sudo tee "$DAEMON" >/dev/null <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- macOS 开机不会自动启用 pf，所以开机时补一次。规则在 /etc/pf.anchors/prreview，
     由 /etc/pf.conf 引用；这个守护进程只做"启用 + 加载"。 -->
<plist version="1.0">
<dict>
    <key>Label</key><string>com.prreview.pf</string>
    <key>ProgramArguments</key>
    <array>
        <string>/sbin/pfctl</string>
        <string>-E</string>
        <string>-f</string>
        <string>/etc/pf.conf</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
PLIST
    sudo chown root:wheel "$DAEMON" || die "chown 失败"
    sudo chmod 644 "$DAEMON" || die "chmod 失败"
    sudo launchctl bootout system/com.prreview.pf 2>/dev/null
    sudo launchctl bootstrap system "$DAEMON" || die "bootstrap 失败"
    say "已装 com.prreview.pf"
    say "pf 状态：$(sudo pfctl -s info 2>/dev/null | grep -o 'Status: [A-Za-z]*')"
    exit 0
fi

# ---------------------------------------------------------------- 找 prr-int 的宿主侧接口（备用匹配用）
# 注意：这个 bridge 只在「有容器挂在 prr-int 上」时才存在，所以得先挂一个临时的把它拉起来。
GW_IFACE=""
detect_gw_iface() {
    local tmp="prr-pfprobe-$$"
    "$CONTAINER" run --detach --name "$tmp" --arch arm64 --network "$NETWORK" \
        --entrypoint /bin/sleep "$IMAGE" 120 >/dev/null 2>&1
    sleep 2
    GW_IFACE=$(ifconfig -a 2>/dev/null | awk -v gw="$HOST_GW" '
        /^[a-z][a-z0-9]*:/ { iface=$1; sub(":", "", iface) }
        /inet / { if ($2 == gw) print iface }
    ' | head -1)
    "$CONTAINER" kill "$tmp" >/dev/null 2>&1
    "$CONTAINER" delete "$tmp" >/dev/null 2>&1
    [ -n "$GW_IFACE" ]
}

# ---------------------------------------------------------------- 写规则（每步都检查）
write_rules_subnet() {
    if ! sudo tee "$ANCHOR_FILE" >/dev/null <<EOF
# 这套 runner 的专属网段（${NETWORK}）在宿主侧的出口规则。
# 放行两个代理端口的 TCP 和 DHCP（免得容器续约失败），其余一律丢。
# 全部以「源 = 容器网段」为条件，其它网段的容器一条都不匹配。
pass  quick proto udp from $SUBNET port 68 to any port 67
pass  quick proto tcp from $SUBNET to $HOST_GW port { $PORTS } flags S/SA keep state
block drop quick from $SUBNET to any
EOF
    then
        return 1
    fi
    return 0
}

write_rules_iface() {
    [ -n "$GW_IFACE" ] || return 1
    sudo tee "$ANCHOR_FILE" >/dev/null <<EOF
# 备用匹配方式：按接口匹配。只在「按源地址匹配不生效」时用 —— bridge 编号在网络重建后会变。
pass  quick on $GW_IFACE proto udp from any port 68 to any port 67
pass  quick on $GW_IFACE proto tcp to $HOST_GW port { $PORTS } flags S/SA keep state
block drop quick on $GW_IFACE from any to any
EOF
    [ -s "$ANCHOR_FILE" ]
}

load_pf() {
    if ! grep -q "anchor \"$ANCHOR\"" "$PF_CONF"; then
        say "往 pf.conf 追加 anchor 引用"
        printf '\nanchor "%s"\nload anchor "%s" from "%s"\n' "$ANCHOR" "$ANCHOR" "$ANCHOR_FILE" \
            | sudo tee -a "$PF_CONF" >/dev/null || { say "追加失败"; return 1; }
    fi

    say "语法检查（pfctl -n）："
    if ! sudo pfctl -n -f "$PF_CONF"; then
        say "→ 语法检查没过，已中止。现有规则集没有被动过"
        return 1
    fi

    say "加载规则集（pfctl -f）："
    if ! sudo pfctl -f "$PF_CONF"; then
        say "→ 加载失败（上面的报错就是真因）"
        return 1
    fi

    say "确保 pf 已启用（pfctl -e）："
    sudo pfctl -e || say "→ pfctl -e 返回非零；若本来就启用着，通常无害"

    say "anchor 里现在生效的规则："
    sudo pfctl -a "$ANCHOR" -s rules
    return 0
}

probe() {
    local allow="" p
    for p in $ALLOW_PORTS; do allow="$allow \"${HOST_GW}:${p}\""; done
    "$CONTAINER" run --rm --arch arm64 --network "$NETWORK" --entrypoint /bin/bash "$IMAGE" -c "
      for t in $allow $(printf '"%s" ' "${BLOCK_TARGETS[@]}"); do
        printf '%s ' \"\$t\"
        timeout 5 bash -c \"exec 3<>/dev/tcp/\${t/:/\\/}\" 2>/dev/null && echo 通 || echo 不通
      done" 2>/dev/null | grep -v '^\['
}

judge() {
    local allow_ok=1 block_ok=1 n=0 target result p is_allow
    while read -r target result; do
        case "$result" in 通|不通) ;; *) continue ;; esac
        n=$((n + 1))
        # 只有「宿主机在容器网段上的那个地址 + 放行端口」才算该通；
        # 同一个端口出现在别的地址上，必须不通。
        is_allow=0
        for p in $ALLOW_PORTS; do
            case "$target" in "$HOST_GW:$p") is_allow=1 ;; esac
        done
        if [ "$is_allow" = 1 ]; then
            [ "$result" = "通" ]   || allow_ok=0
        else
            [ "$result" = "不通" ] || block_ok=0
        fi
    done
    # 下限 = 两个放行靶子 + 1.1.1.1 + 至少一个端口靶子。
    # 1.1.1.1 只证明容器出不了公网，不能代替「宿主端口挡没挡住」那一条；
    # 端口靶子为空说明兜底没成立，应判失败，不要给出未经确认的通过。
    [ "$n" -ge 4 ] || return 1
    [ "$allow_ok" = 1 ] && [ "$block_ok" = 1 ]
}

# ---------------------------------------------------------------- 主流程
head1 "0. 现状"
say "pf: $(sudo pfctl -s info 2>/dev/null | grep -o 'Status: [A-Za-z]*' || echo 未知)"
say "ip.forwarding: $(sysctl -n net.inet.ip.forwarding)（上游那个 hostOnly 泄漏开关，1 = 条件成立）"

head1 "1. 备份 + 清掉上次可能的残留"
if [ ! -f "$BACKUP" ]; then
    sudo cp "$PF_CONF" "$BACKUP" || die "备份失败"
    say "已备份到 $BACKUP"
else
    say "备份已存在，保留最早那份"
fi
if grep -q "anchor \"$ANCHOR\"" "$PF_CONF"; then
    sudo sed -i '' "/anchor \"$ANCHOR\"/d; /load anchor \"$ANCHOR\"/d" "$PF_CONF" || die "清残留失败"
    say "清掉了上次残留的 anchor 引用"
fi

head1 "2. 写规则 + 加载"
if write_rules_subnet; then
    say "规则文件已写：$ANCHOR_FILE"
    sed 's/^/      /' "$ANCHOR_FILE" | grep -vE '^ +#'
else
    say "写 $ANCHOR_FILE 失败，试一下直接 sudo 写它看看报什么："
    sudo tee "$ANCHOR_FILE" </dev/null >/dev/null
    die "写 anchor 文件失败（多半是权限或目录问题）"
fi
load_pf || die "加载 pf 失败（上面有 pfctl 的报错原文）"
say "已加载"

head1 "3. 从容器实测"
RESULT=$(probe)
echo "$RESULT" | sed 's/^/    /'

if judge <<<"$RESULT"; then
    OK=1
    say "✅ 按源网段匹配生效"
else
    OK=0
    say "⚠ 按源网段匹配没生效（pf 看到的源地址可能不是容器地址）"
    head1 "4. 改用按接口匹配重试"
    say "先挂个临时容器把 prr-int 的 bridge 拉起来再探测…"
    if detect_gw_iface; then
        say "prr-int 的宿主侧接口: $GW_IFACE"
        say "⚠ 这个 bridge 只在有容器挂在该网络上时才存在，规则可能随之失效 —— 只当临时手段"
        if write_rules_iface; then
            load_pf || die "加载失败"
            RESULT=$(probe)
            echo "$RESULT" | sed 's/^/    /'
            if judge <<<"$RESULT"; then OK=1; say "✅ 按接口匹配生效（但要盯住 bridge 是否还在）"; else say "❌ 两种匹配都没生效"; fi
        else
            say "写接口版规则失败"
        fi
    else
        say "找不到 prr-int 的宿主侧接口，无法用备用方式"
    fi
fi

head1 "5. 确认别的容器没受影响（走 default 网段）"
"$CONTAINER" run --rm --arch arm64 --entrypoint /bin/bash "$IMAGE" -c '
    timeout 6 bash -c "exec 3<>/dev/tcp/github.com/443" 2>/dev/null \
      && echo "  default 网段不受影响 ✓" || echo "  ⚠ default 也不通了，规则过宽"' 2>/dev/null

head1 "结论"
if [ "$OK" = 1 ]; then
    echo "  出口已收口。接着跑：bash $0 --persist   （开机自动生效）"
else
    echo "  没成功。回滚：bash $0 --rollback"
fi
echo "  回滚随时可用：bash $0 --rollback"
echo "  本次全过程已写到：$LOG"
