#!/usr/bin/env python3
"""网关和启动器共用的那点东西。

刻意保持薄 —— 只有"两边逐字重复过"的东西才放进来：日志配置、来源网段判断、读凭据。
放这里的目的是消掉重复，不是造框架。
"""

import argparse
import ipaddress
import json
import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setup_logging(log_path=None):
    """统一的日志配置。

    注意不能把 stream 和 filename 一起传给 basicConfig：Python 3.9 检查的是"键是否存在"，
    不是值是否为真，所以连 stream=None 都会抛 ValueError（踩过这个坑）。
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


def guard_ports(gateway, exclude=(), only=None, netstat_text=None, limit=3):
    """宿主机上**从容器够得着**的监听端口，当出口自检的靶子。

    判据（`pf-setup.sh` 与 `launcher.py` 共用这一份，别在别处再写一遍）：只取绑在
    通配地址或网关地址上的监听。绑在回环或别的地址上的端口，容器走网关地址本就碰不到，
    拿它当靶子永远是"没连上" —— 那条断言等于没做（假绿）。

    返回 `(ports, ok)`：`ok=False` 表示**这次探测本身没成立**（netstat 跑不起来），
    与"确实筛不出靶子"（ok=True, ports=[]）是两回事，调用方要区别对待。
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
    # `only` 在**取前 limit 个之前**收窄候选集。顺序反过来（先截断再收窄）会让"配置里给的
    # 靶子恰好不在前几个里"变成空集 —— 那不是配置错，是我们自己截断早了。
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

    宿主机看到的容器地址带 IPv6 映射前缀（`::ffff:192.168.100.3`），要先剥掉再比 ——
    某些平台下容器地址就是这个形式，不剥的话一条规则都匹配不上。
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
    # 给 pf-setup.sh 用：bash 里 import 不了，所以留一个最小 CLI，让"哪个宿主端口能当靶子"
    # 这道判据两边共用同一份实现（以前是 bash 的 awk 和 Python 各写一遍，靠注释手工同步）。
    ap = argparse.ArgumentParser(description="从 netstat 里挑出容器够得着的宿主端口")
    ap.add_argument("--guard-ports", metavar="GATEWAY", required=True,
                    help="容器侧看到的宿主机地址（网关地址）")
    ap.add_argument("--exclude", default="", help="逗号分隔，要剔除的端口（网关自己的）")
    _a = ap.parse_args()
    _ports, _ok = guard_ports(_a.guard_ports,
                              [p for p in _a.exclude.replace(" ", "").split(",") if p])
    if not _ok:
        raise SystemExit("netstat 跑不起来，探不到宿主端口")
    print(" ".join(str(p) for p in _ports))
