#!/usr/bin/env python3
"""网关和启动器共用的少量工具：日志配置、来源网段判断、读凭据。

只放两边逐字重复过的东西 —— 消重复，不造框架。
"""

import argparse
import ipaddress
import socket
import time
import json
import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setup_logging(log_path=None):
    """统一的日志配置。

    不能把 stream 和 filename 一起传给 basicConfig：Python 3.9 检查键是否存在而不是值，
    连 stream=None 都会抛 ValueError。
    """
    common = {"level": logging.INFO, "format": LOG_FORMAT}
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(log_path), **common)
    else:
        logging.basicConfig(stream=sys.stderr, **common)


def load_json_config(path, required=()):
    """读 JSON 配置；缺必填项直接退出 —— 带着错配置空转没有意义。"""
    real = Path(os.path.expanduser(str(path)))
    with open(real) as fh:
        cfg = json.load(fh)
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SystemExit("配置缺必填项: %s（文件: %s）" % (", ".join(missing), real))
    return cfg


def expand(path):
    return os.path.expanduser(str(path))


def read_secret(path):
    """读一个凭据文件。只返回值，不打印、不落别处。"""
    return Path(expand(path)).read_text().strip()


def parse_cidrs(specs):
    return [ipaddress.ip_network(s) for s in (specs or [])]


def canary_port(bind="0.0.0.0"):
    """开一个临时监听并返回 (socket, port)；调用方负责关。

    只用来判断"连不连得上"，不需要 accept —— TCP 握手由内核用 accept 队列完成，
    保持 socket 开着，容器侧的 /dev/tcp 就能连上。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, 0))
    s.listen(8)
    return s, s.getsockname()[1]


def guard_ports(gateway, exclude=(), only=None, netstat_text=None, limit=3):
    """宿主机上从容器够得着的监听端口，当出口自检的靶子（pf-setup.sh 与 launcher.py 共用）。

    只取绑在通配地址或网关地址上的监听：绑在回环或别的地址上的，容器走网关地址本就碰不到。
    返回 `(ports, ok)`，`ok=False` 表示探测本身没成立（netstat 跑不起来），要与"筛不出靶子"区分。
    """
    if netstat_text is None:
        import subprocess
        try:
            proc = subprocess.run(["netstat", "-an", "-f", "inet"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return [], False
        if proc.returncode != 0:
            return [], False
        netstat_text = proc.stdout
    skip = {int(p) for p in exclude}
    # `only` 必须在取前 limit 个之前收窄候选集；顺序反过来会让配置给的靶子变成空集。
    keep = None if only is None else {int(p) for p in only}
    found = set()
    for line in netstat_text.splitlines():
        if "LISTEN" not in line:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        addr, _, tail = fields[3].rpartition(".")   # '*.18080' / '127.0.0.1.22'
        if not tail.isdigit():
            continue
        if addr not in ("*", gateway):
            continue
        if keep is not None and int(tail) not in keep:
            continue
        found.add(int(tail))
    return sorted(p for p in found if p not in skip)[:limit], True


def client_allowed(addr, nets):
    """来源地址是否落在允许网段里。nets 为空表示不限制。

    宿主看到的容器地址可能带 IPv6 映射前缀（`::ffff:192.168.100.3`），要先剥掉再比，
    否则一条规则都匹配不上。
    """
    if not nets:
        return True
    raw = (addr or "").split("%")[0]
    if raw.startswith("::ffff:"):
        raw = raw[len("::ffff:"):]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return any(ip in net for net in nets)


if __name__ == "__main__":
    # 给 pf-setup.sh 用的最小 CLI：bash 里 import 不了，让"哪个宿主端口能当靶子"两边共用同一份实现。
    ap = argparse.ArgumentParser(description="从 netstat 里挑出容器够得着的宿主端口")
    ap.add_argument("--guard-ports", metavar="GATEWAY",
                    help="容器侧看到的宿主机地址（网关地址）")
    ap.add_argument("--exclude", default="", help="逗号分隔，要剔除的端口（网关自己的）")
    ap.add_argument("--canary-port", action="store_true",
                    help="开一个临时监听，打印端口后一直保持（给装机脚本当靶子）")
    _a = ap.parse_args()

    if _a.canary_port:
        _srv, _port = canary_port()
        print(_port, flush=True)
        try:
            while True:
                time.sleep(30)
        finally:
            _srv.close()

    if not _a.guard_ports:
        ap.error("要么给 --guard-ports，要么用 --canary-port")
    _ports, _ok = guard_ports(_a.guard_ports,
                              [p for p in _a.exclude.replace(" ", "").split(",") if p])
    if not _ok:
        raise SystemExit("netstat 跑不起来，探不到宿主端口")
    print(" ".join(str(p) for p in _ports))
