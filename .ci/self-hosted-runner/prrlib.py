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
    # 给 bash 侧的装机脚本用：开一个临时监听并打印端口（给容器当"必须连不上"的靶子）。
    ap = argparse.ArgumentParser(description="开一个临时监听，打印端口后保持不动")
    ap.add_argument("--canary-port", action="store_true", required=True)
    ap.parse_args()
    srv, port = canary_port()
    print(port, flush=True)
    try:
        while True:
            time.sleep(30)
    finally:
        srv.close()
