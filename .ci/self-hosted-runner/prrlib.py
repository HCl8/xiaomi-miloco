#!/usr/bin/env python3
"""网关和启动器共用的那点东西。

刻意保持薄 —— 只有"两边逐字重复过"的东西才放进来：日志配置、来源网段判断、读凭据。
放这里的目的是消掉重复，不是造框架。
"""

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
