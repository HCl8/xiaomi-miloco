#!/usr/bin/env python3
"""宿主机上的出网网关 —— 一个进程，两个端口。

合成一个进程的理由：这两个代理形状完全一样（长驻、被动、无状态），而且是"要么都在、要么都不在"
——GitHub 那侧挂了接不到任务，模型那侧挂了审查跑不完，没有"只需要其中一个"的场景。
合成一个进程少一个活动部件，也少一处重复。

但它和**启动器刻意分开**：启动器会频繁重启（部署、改代码），而每次重启都会掐断正在飞的请求。
两者寿命不同，不该互相承担 —— 今天为了改启动器重启了四五次，每次都刻意等到审查跑完。

两个端口的分工：

  18080  GitHub 转发代理
         容器在 host-only 网络里没有到公网的路由，GitHub 流量只能从宿主机出去。
         按域名白名单放行（runner 要能到的那几个端点），其余 403。

  18081  模型代理
         拿容器递来的**假钥匙**换**真 key** 再发给上游。真 key 只在宿主机、永不进容器；
         容器拿到的一把假钥匙只在这层认。
"""

import argparse
import http.client
import logging
import selectors
import signal
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import prrlib

LOG = logging.getLogger("gateway")

# 隧道里单方向允许积压的未发字节上限。对端不收时断开，别把这层（跑在宿主机上、
# 和整台机器共用内存）拖垮。
MAX_PENDING = 8 * 1024 * 1024

# ---- 运行时配置（main 里填）----
ALLOWED_NETS = []
MODEL_UPSTREAM = None
MODEL_REAL_KEY = None
MODEL_EXPECT_TOKEN = None

_stopping = False

# GitHub 要求自托管 runner 能到的端点。取自官方 "self-hosted runners reference" 那张按用途分组的表。
#
# ⚠️ 少一项是**静默**坏功能，不是报错：我们最初漏了 `*.blob.core.windows.net`，
# 表现是任务正常跑完、结论正常发出，但 job 日志一条都取不到（网关日志里能看到它被 403 拒了）。
# 所以这里按官方清单逐组对齐，每组注明用途 —— 将来出错时能从注释直接对上"哪一组没放行"。
GH_HOSTS_EXACT = (
    # 基本运转
    "api.github.com",
    "codeload.github.com",       # 下载 actions
    # 上传/下载 任务摘要、日志、产物、缓存（blob 那半在下面的后缀里）
    "results-receiver.actions.githubusercontent.com",
    # 包与镜像
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
    "uploads.github.com",
)
GH_HOSTS_SUFFIXES = (
    "github.com",                        # 基本运转
    "actions.githubusercontent.com",     # 基本运转 + 取 OIDC token
    "githubusercontent.com",             # runner 版本更新、LFS、release assets
    "githubassets.com",
    "blob.core.windows.net",             # 任务摘要/日志/产物/缓存的存储端
    "pkg.github.com",                    # GitHub Packages
    "github-cloud.s3.amazonaws.com",     # Git LFS
)
# 刻意没放：*.core.windows.net（官方标为 VNet 场景）、githubapp.com（Dependabot）—— 我们不用。
# 另外注意 blob.core.windows.net 是 Azure：放行它意味着容器能往那儿写数据。
# 这是 GitHub Actions 存日志/产物的固有形态，不是我们选的路；审计时要知道这一条在。

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def host_allowed(host):
    host = (host or "").lower().rstrip(".")
    if host in GH_HOSTS_EXACT:
        return True
    return any(host == s or host.endswith("." + s) for s in GH_HOSTS_SUFFIXES)


# ---------------------------------------------------------------- GitHub 转发代理

class GitHubProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "prr-gw-github"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _gate(self):
        if not prrlib.client_allowed(self.client_address[0], ALLOWED_NETS):
            LOG.warning("拒绝：来源不在容器网段 %s", self.client_address[0])
            self.send_error(403, "source not allowed")
            return False
        return True

    def _target_ok(self, host):
        if host_allowed(host):
            return True
        LOG.warning("拒绝：域名不在白名单 %s", host)
        self.send_error(403, "host not allowed")
        return False

    def do_CONNECT(self):
        if not self._gate():
            return
        host, _, port = self.path.partition(":")
        if not self._target_ok(host):
            return
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=20)
        except OSError as exc:
            LOG.warning("连不上 %s:%s —— %s", host, port, exc)
            self.send_error(502, "upstream unreachable")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self._tunnel(upstream)

    def _tunnel(self, upstream):
        """两个 socket 之间双向搬字节。

        写这一侧**不能用 `sendall`**：两个 socket 都是非阻塞的，sendall 发不完会抛
        BlockingIOError，而那时前半截已经发出去了 —— 重试会重复发前缀，被外层 except 吞掉
        则表现为"对端一慢就静默拆掉整条连接"。所以自己记一份没发完的尾巴，等 socket 可写
        再接着发。

        `pending[x]` = 还要**写给** x 的字节。
        """
        downstream = self.connection
        downstream.setblocking(False)
        upstream.setblocking(False)
        sel = selectors.DefaultSelector()
        peers = {downstream: upstream, upstream: downstream}
        pending = {downstream: b"", upstream: b""}
        for sock in (downstream, upstream):
            sel.register(sock, selectors.EVENT_READ, sock)
        try:
            while True:
                for key, mask in sel.select(timeout=600):
                    sock = key.fileobj
                    if mask & selectors.EVENT_WRITE:
                        buf = pending[sock]
                        if not buf:                       # 已经发完了，回去只等可读
                            sel.modify(sock, selectors.EVENT_READ, sock)
                            continue
                        try:
                            sent = sock.send(buf)
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError:
                            return
                        pending[sock] = buf[sent:]
                        if not pending[sock]:
                            sel.modify(sock, selectors.EVENT_READ, sock)
                        continue
                    try:
                        data = sock.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    dst = peers[sock]
                    buf = pending[dst] + data
                    try:
                        sent = dst.send(buf)
                    except (BlockingIOError, InterruptedError):
                        sent = 0
                    except OSError:
                        return
                    pending[dst] = buf[sent:]
                    if len(pending[dst]) > MAX_PENDING:
                        # 对端一直不收（对端 socket 可读事件照常注册着，字节只进不出）。
                        # 断开比把宿主机内存吃光强 —— 这层在宿主上，和整台机器共用内存。
                        LOG.warning("对端长时间不收，待写缓冲超过 %d 字节，断开这条隧道",
                                    MAX_PENDING)
                        return
                    if pending[dst]:                      # 没发完 → 盯上它的可写事件
                        sel.modify(dst, selectors.EVENT_READ | selectors.EVENT_WRITE, dst)
        except OSError as exc:
            # 别静默：这一层是唯一的出口，链断了要能在日志里看见原因
            LOG.warning("隧道断开：%s", exc)
        finally:
            sel.close()
            try:
                upstream.close()
            except OSError:
                pass

    def _plain(self):
        if not self._gate():
            return
        if not self.path.startswith("http://"):
            self.send_error(400, "expect absolute URI")
            return
        rest = self.path[len("http://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        if not self._target_ok(host):
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            upstream = socket.create_connection((host, int(port or 80)), timeout=20)
        except OSError as exc:
            LOG.warning("连不上 %s:%s —— %s", host, port, exc)
            self.send_error(502, "upstream unreachable")
            return
        try:
            req = ["%s /%s HTTP/1.1" % (self.command, path), "Host: " + hostport]
            for key, value in self.headers.items():
                if key.lower() in ("proxy-connection", "connection", "host"):
                    continue
                req.append("%s: %s" % (key, value))
            req += ["Connection: close", "", ""]
            upstream.sendall("\r\n".join(req).encode() + body)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        finally:
            upstream.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = lambda self: self._plain()


# ---------------------------------------------------------------- 模型代理

class ModelProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "prr-gw-model"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _incoming_token(self):
        tok = self.headers.get("x-api-key")
        if tok:
            return tok.strip()
        auth = self.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _handle(self):
        if not prrlib.client_allowed(self.client_address[0], ALLOWED_NETS):
            LOG.warning("拒绝：来源不在容器网段 %s", self.client_address[0])
            self.send_error(403, "source not allowed")
            return

        # 存活探针刻意放在鉴权之前：它只回 "ok"，不泄露任何东西，方便本机自检
        if self.path.rstrip("/") == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if MODEL_EXPECT_TOKEN is not None and self._incoming_token() != MODEL_EXPECT_TOKEN:
            LOG.warning("拒绝：容器递来的钥匙不对")
            self.send_error(401, "bad token")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        target = MODEL_UPSTREAM.path.rstrip("/") + self.path
        headers = {}
        for key, value in self.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "content-length",
                                            "x-api-key", "authorization"):
                continue
            headers[key] = value
        headers["Host"] = MODEL_UPSTREAM.netloc
        headers["x-api-key"] = MODEL_REAL_KEY      # 真 key 只在这一步注入
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"

        conn = http.client.HTTPSConnection(MODEL_UPSTREAM.hostname,
                                           MODEL_UPSTREAM.port or 443,
                                           timeout=600,
                                           context=ssl.create_default_context())
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            LOG.warning("连不上上游 —— %s", exc)
            self.send_error(502, "upstream unreachable")
            conn.close()
            return

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        # 不给 Content-Length：靠 close 划界，这样 SSE 是一段段流出来的
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                # 必须用 read1：read(n) 会**阻塞到攒够 n 字节或流结束**（chunked 时逐块拼、
                # 非 chunked 时走 BufferedReader.read），于是模型的 SSE 变成一坨一坨地到，
                # 中间整段整段地停。read1 是"有多少给多少"。
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, BrokenPipeError):
            pass
        finally:
            conn.close()
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


# ---------------------------------------------------------------- 起两个监听

def _on_signal(signum, _frame):
    global _stopping
    LOG.info("收到信号 %s，准备退出", signum)
    _stopping = True


def main():
    global ALLOWED_NETS, MODEL_UPSTREAM, MODEL_REAL_KEY, MODEL_EXPECT_TOKEN

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/prreview/prreview.json")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--log", default=None, help="不给就打到 stderr")
    args = ap.parse_args()

    cfg = prrlib.load_json_config(args.config, required=(
        "github_proxy_port", "model_proxy_port", "model_upstream",
        "model_key_file", "container_token_file",
    ))
    log_path = args.log or (prrlib.expand(cfg["log_dir"]) + "/gateway.log"
                            if cfg.get("log_dir") else None)
    prrlib.setup_logging(log_path)

    ALLOWED_NETS = prrlib.parse_cidrs(cfg.get("allow_cidrs"))
    MODEL_UPSTREAM = urlsplit(cfg["model_upstream"])
    MODEL_REAL_KEY = prrlib.read_secret(cfg["model_key_file"])
    MODEL_EXPECT_TOKEN = prrlib.read_secret(cfg["container_token_file"])

    gh_port = int(cfg["github_proxy_port"])
    model_port = int(cfg["model_proxy_port"])

    servers = []
    for port, handler, what in ((gh_port, GitHubProxy, "GitHub 转发"),
                                (model_port, ModelProxy, "模型代理")):
        srv = ThreadingHTTPServer((args.bind, port), handler)
        srv.daemon_threads = True
        servers.append((srv, port, what))

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    LOG.info("启动：允许来源=%s 上游=%s",
             [str(n) for n in ALLOWED_NETS] or "不限", cfg["model_upstream"])
    for srv, port, what in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        LOG.info("%s 监听 %s:%d", what, args.bind, port)

    while not _stopping:
        time.sleep(1)

    for srv, port, _ in servers:
        srv.shutdown()
    LOG.info("已退出")


if __name__ == "__main__":
    main()
