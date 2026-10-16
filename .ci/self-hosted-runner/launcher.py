#!/usr/bin/env python3
"""一次性 runner 启动器 —— 跑在运行机上，由 launchd 托管。

每一轮做同一件事：要一份一次性登记凭据 → 起一个容器把凭据交给它 → 等容器退出 → 补下一个。
容器是本进程起的、参数是写死的，所以任务对容器形态没有话语权（挂不了宿主目录、提不了特权）。

两条不能破的性质：
  1. 凭据不进日志、不进任何进程的命令行参数（命令行参数在宿主机 ps 里人人可见）、
     也不经环境变量（会写进容器记录 config.json）。交给容器时在 ~/prreview 下用 mkstemp
     建一个 0600 临时文件过一手，拷完立刻 unlink —— 那是磁盘上唯一一次落地的窗口，
     进程被 SIGKILL 才会留下残留，所以启动时顺手清扫一遍。
  2. 容器寿命不超过凭据寿命：空等超过 idle_recycle_seconds 就主动回收重来，
     堵上"凭据 1 小时过期"和"容器可能空等很久"之间的缝。

只用标准库：这台机器上没有 Homebrew，系统 python3 是 3.9，引第三方依赖等于给自己加部署负担。
"""

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import prrlib

CONTAINER = os.environ.get("PRR_CONTAINER_BIN", "/usr/local/bin/container")
# 容器侧入口包装等待的凭据路径，必须与 .ci/runner-entrypoint.sh 的默认值一致
JIT_PATH = "/opt/runner/.jitconfig"
DEFAULT_CONFIG = Path.home() / "prreview" / "prreview.json"
GITHUB_API = "https://api.github.com"

log = logging.getLogger("launcher")

# 收到 TERM/INT 时置位，主循环据此收尾；不清空当前容器就是留一堆孤儿
_stopping = False


def _on_signal(signum, _frame):
    global _stopping
    log.info("收到信号 %s，准备退出", signum)
    _stopping = True


# ---------------------------------------------------------------- 配置

REQUIRED = (
    "image", "network", "host_gateway", "github_proxy_port", "model_proxy_port",
    "github_repo", "github_token_file",
)


def load_config(path):
    """读 JSON 配置。缺必填项就直接退出——启动器在一个错配置上转圈没有意义。"""
    cfg = prrlib.load_json_config(path, required=REQUIRED)
    cfg.setdefault("cpus", 4)
    cfg.setdefault("memory", "4g")
    cfg.setdefault("idle_recycle_seconds", 3000)   # 50 分钟，留足余量给 1 小时的凭据
    cfg.setdefault("name_prefix", "prr")
    cfg.setdefault("log_dir", str(Path.home() / "prreview" / "logs"))
    cfg.setdefault("runner_group_id", 1)
    # 默认值必须与 workflow 的 `runs-on` 对得上（那边是 [self-hosted, prr-test]）。
    # 对不上时 GitHub 不报错，只是永远不派活 —— 这台机器会一直"空闲且正常"。
    cfg.setdefault("runner_labels", ["self-hosted", "linux", "arm64", "prr-test"])
    # 出口自检：起 runner 之前先实测"宿主机上那些无关服务封住了没"。默认开。
    cfg.setdefault("require_egress_guard", True)
    cfg.setdefault("guard_recheck_seconds", 900)
    # 出口自检探哪几个端口：默认由 discover_guard_ports 现探（这台机器在听什么就测什么），
    # 想固定就显式给 guard_ports。
    # 池子：同时维持几个待命容器。每个容器只接一个任务，所以要 >1 才能扛并发。
    cfg.setdefault("replicas", 1)
    cfg.setdefault("poll_seconds", 5)
    # 宿主内存水位：可回收低于这个值就不再补新槽（宁可少接活，也别把机器压进压缩/换页）
    cfg.setdefault("min_free_gb", 2.5)
    # 定期清理：容器 churn 快（镜像与快照会持续堆积），一小时清一次
    cfg.setdefault("maintenance_interval_seconds", 3600)
    # JSON 里写不了 ~，所以路径字段在这里统一展开
    for key in ("log_dir", "github_token_file", "container_token_file"):
        if cfg.get(key):
            cfg[key] = prrlib.expand(cfg[key])
    return cfg


# ---------------------------------------------------------------- 出口自检

# 容器里跑这几条：能连上任一个，就说明 pf 那层不在了
GUARD_PROBE = (
    'rc=0; '
    'for p in {ports}; do '
    'if timeout 4 bash -c "exec 3<>/dev/tcp/{gw}/$p" 2>/dev/null; then '
    'echo "reachable:$p"; rc=1; fi; '
    'done; '
    'exit $rc'
)


def discover_guard_ports(cfg, restrict=None):
    """宿主机正在监听的 TCP 端口（去掉我们自己的两个网关端口）。

    `restrict` 给了就只在其中挑（配置里的 guard_ports 走这条）。**配置给的靶子也要过同一道
    筛**：写进来一个绑在回环上的端口，容器走网关地址永远够不着，那条自检就成了永远绿的摆设
    —— 而它本该是"够不够得着"的实测。

    出口自检要证明的是"容器够不着宿主机上那些与本方案无关的服务"，所以靶子应当由
    **这台机器实际在听什么**决定，而不是写死两个端口 —— 写死既在代码里留下环境痕迹，
    又测不到这台机器上真正暴露的东西。

    返回 None 表示**探测本身失败**（跟"确实没有别的端口"是两回事）：安全检查不能因为
    工具出错就放行，所以这两种情况必须分开，由调用方区别对待。
    """
    try:
        proc = subprocess.run(["netstat", "-an", "-f", "inet"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    mine = {int(cfg["github_proxy_port"])}
    if cfg.get("model_proxy_port"):
        mine.add(int(cfg["model_proxy_port"]))
    ports = set()
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        addr, _, tail = fields[3].rpartition(".")    # '*.18080' → '*'+'18080'；'127.0.0.1.22' → '127.0.0.1'+'22'
        if not tail.isdigit():
            continue
        # 只收「通配」或「就绑在网关地址上」的监听：只有这两种在防火墙缺位时真的够得着。
        # 绑在 127.0.0.1 或别的地址上的端口，容器走网关地址本就碰不到 —— 拿它当靶子永远是
        # "没连上"，那条断言等于没做（假绿）。
        if addr not in ("*", cfg["host_gateway"]):
            continue
        ports.add(int(tail))
    if restrict is not None:
        ports &= {int(p) for p in restrict}
    # 只取前几个：这是"够不够得着"的抽样，不是端口清单。每个靶子都要等一次超时，
    # 取全部会让自检从几秒变成近一分钟。
    return sorted(p for p in ports if p not in mine)[:3]


def check_egress_guard(cfg):
    """起一个一次性容器，实测容器还能不能碰到宿主机上那些与本方案无关的服务端口。

    这是唯一不依赖 sudo 的验证方式——不去读防火墙配置，直接问"够不够得着"。
    够得着说明 pf 那层不在了（规则被清、开机没加载、网段变了……），
    此时起 runner 等于把网络摊在公网 PR 面前，所以宁可不干活。

    探测哪几个端口由配置给（guard_ports），函数本身不写死。
    返回 (ok, 说明)。
    """
    ports = discover_guard_ports(cfg, cfg.get("guard_ports"))
    if ports is None:
        # 探测失败 ≠ 没有目标。宁可挡住，也不假装检查过了
        return False, "探不到宿主机在监听哪些端口（netstat 失败），无法完成出口自检"
    if not ports:
        # 一个靶子都筛不出来 → **判失败**，与 pf-setup.sh 的 judge（要求至少一个端口靶子）
        # 在同条件下判决一致。不能当"没有可测目标"放过：真正的"这台机器只听了网关"与
        # "我们的解析坏了/输出格式变了"在这里长得一模一样，而后者会让这条安全检查
        # 静默失效（一个探针容器都没跑，日志上却是"自检通过"）。
        return False, "筛不出可测的宿主端口（netstat 输出没有通配/网关地址上的监听？），无法完成出口自检"
    probe = GUARD_PROBE.format(gw=cfg["host_gateway"],
                               ports=" ".join(str(p) for p in ports))
    args = ["run", "--rm", "--arch", "arm64", "--network", cfg["network"],
            "--entrypoint", "/bin/bash", cfg["image"], "-c", probe]
    try:
        proc = subprocess.run([CONTAINER] + args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "自检没跑起来: %s" % exc
    if proc.returncode == 0:
        return True, "宿主端口已封住"
    hit = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("reachable:")]
    if hit:
        return False, "从容器里能连到宿主机端口 %s —— pf 那层不在了" % ", ".join(hit)
    return False, "自检异常退出(%d): %s" % (proc.returncode, (proc.stderr or proc.stdout).strip()[:200])


def check_gateway(cfg):
    """我们的出网网关还在监听吗。

    和出口自检是两件事，都要在起容器前确认：
      - 出口自检 = 安全（隔离还在不在）
      - 这一条   = 可用性（代理还活着没）
    少确认后者的话，容器会注册成功、领到任务，然后在审查时连不上模型 —— 白白吃掉一个任务。
    """
    down = []
    for port in (cfg["github_proxy_port"], cfg.get("model_proxy_port")):
        if not port:
            continue
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                pass
        except OSError as exc:
            down.append("%d(%s)" % (port, type(exc).__name__))
    if down:
        return False, "网关端口连不上: %s" % ", ".join(down)
    return True, "网关两个端口都在监听"


# ---------------------------------------------------------------- 凭据

def mint_jit(cfg, name=None):
    """要一份一次性登记凭据。跑在宿主机上：宿主机能直连 api.github.com，容器没有路由。

    name 用容器名，这样 GitHub 上的 runner 记录能和容器一一对上 ——
    强杀留下的记录才清得掉（ephemeral runner 正常退出会自己注销，被杀的不会）。
    凭据只作为返回值在内存里流动，不写文件也不打日志。
    """
    token = Path(cfg["github_token_file"]).read_text().strip()
    body = json.dumps({
        "name": name or ("%s-%d" % (cfg["name_prefix"], int(time.time()))),
        "runner_group_id": cfg["runner_group_id"],
        "labels": cfg["runner_labels"],
        "work_folder": "_work",
    }).encode()
    req = urllib.request.Request(
        "%s/repos/%s/actions/runners/generate-jitconfig" % (GITHUB_API, cfg["github_repo"]),
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    jit = payload.get("encoded_jit_config")
    if not jit:
        raise RuntimeError("GitHub 没返回 encoded_jit_config")
    return jit


# ---------------------------------------------------------------- 容器

def _run(args, **kw):
    return subprocess.run([CONTAINER] + args, capture_output=True, text=True, **kw)


def container_running_job(name):
    """容器里的 runner 是不是正在跑任务（看它日志最后一行）。

    三态：True 在跑 / False 明确没在跑 / **None 问不出来**（读日志失败）。调用方必须把
    None 当"可能正在跑"：把"没问到"当"没在跑"，下手就掐断一个在跑的作业，还会留下一条
    GitHub 拒绝删（422）的 runner 记录，占住该 PR 的 concurrency 组直到 job 超时。
    """
    proc = _run(["logs", name])
    # **先看退出码**：这才是"这次调用成没成"的主判据。只看 stdout 空不空等于假设
    # CLI 把报错写在别的流上 —— 那是实现细节，赌错了就在"命令失败"时返回 False
    # （"确定没在跑"），三处调用照样下手杀容器。
    if proc.returncode != 0:
        return None
    # **只看 stdout**：`container logs` 把容器的日志打在它自己的 stdout 上，stderr 是 CLI 的
    # 报错。把两条流拼起来的话，"最后一行"变成 stderr 的最后一行 —— 而"跑作业期间最后一行
    # 恰好是 Running job:"这个位置性质就没了保证（runner 期间任何一条 stderr 都会顶掉它）。
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None                      # 还没吐出任何日志（刚起来）→ 也算问不出来，别当"没在跑"
    return "Running job:" in lines[-1]


def container_state(name):
    """容器状态：running/stopped/... ；**确实没有这个容器**返回 None ；**问不出来**返回 "unknown"。

    三态必须分开，两个方向都踩过/会踩：
      - 只看 `container inspect` 的退出码不行：容器停止之后它照样返回 0，看护循环会以为它
        还活着、一直空等到回收时限（实测踩过：跑完一个任务后启动器五十分钟才补下一个）。
      - 把"问不出来"并进"已经没了"同样不行：调用方据此收容器，于是 inspect 偶发失败或输出
        形状变了，就会掐掉一个正在跑任务的容器 —— 白赔一个作业，还留下一条 GitHub 拒绝删
        （422）的 runner 记录堵住该 PR 的 concurrency 组。
    """
    proc = _run(["inspect", name])
    if proc.returncode != 0:
        blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
        # CLI 对不存在的容器报 `container not found: <名字>`
        return None if ("not found" in blob or "no such" in blob) else "unknown"
    try:
        return json.loads(proc.stdout)[0]["status"]["state"]
    except (ValueError, KeyError, IndexError, TypeError):
        return "unknown"


def kill_and_delete(name):
    _run(["kill", name])
    _run(["delete", name])


def start_container(cfg, jit, name, round_no):
    """起一个一次性容器，把凭据交给它。

    凭据用 `container cp` 拷进容器文件系统，**不走命令行参数、也不走环境变量**——
    那两条路实测都会把凭据留在宿主上：命令行参数进 `ps`（人人可见），
    环境变量进容器记录 `config.json`（644 权限，而且删容器不清记录）。
    拷文件这条走完，宿主机磁盘上搜不到凭据，容器记录里也没有；容器侧读完即删。

    容器形态全部写死在这里：不挂宿主目录、不开容器 socket、非 root（镜像里定的）、
    加 CPU/内存限额。任务对这些没有话语权。
    """
    env = dict(os.environ)
    env["HTTP_PROXY"] = "http://%s:%s" % (cfg["host_gateway"], cfg["github_proxy_port"])
    env["HTTPS_PROXY"] = env["HTTP_PROXY"]
    env["NO_PROXY"] = "%s,localhost,127.0.0.1" % cfg["host_gateway"]

    # 模型环境：容器只拿到"代理地址 + 一把假钥匙 + 模型名"，真 key 在宿主机代理里，永不进容器。
    # 这几项走环境变量、因而会进容器记录——可以接受：假钥匙是我们自己造的，
    # 只在本地代理上有效，而代理本身还限了来源网段。真凭据一个都不走这条路。
    token_file = cfg.get("container_token_file")
    if cfg.get("model_proxy_port") and token_file and Path(token_file).is_file():
        env["ANTHROPIC_API_KEY"] = Path(token_file).read_text().strip()
        env["ANTHROPIC_BASE_URL"] = "http://%s:%s" % (cfg["host_gateway"], cfg["model_proxy_port"])
        # pr-agent 靠这个决定用哪个模型；漏了就会用 CLI 自己的默认模型
        if cfg.get("agent_model"):
            env["PR_AGENT_MODEL"] = cfg["agent_model"]
    else:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("PR_AGENT_MODEL", None)

    args = [
        "run", "--detach",
        "--name", name,
        "--label", "prr.round=%d" % round_no,
        "--arch", "arm64",
        "--network", cfg["network"],
        "--cpus", str(cfg["cpus"]),
        "--memory", cfg["memory"],
        # 只传键名让容器从本进程环境继承，所以值不进命令行
        "--env", "HTTP_PROXY", "--env", "HTTPS_PROXY", "--env", "NO_PROXY",
    ]
    if "ANTHROPIC_API_KEY" in env:
        args += ["--env", "ANTHROPIC_API_KEY", "--env", "ANTHROPIC_BASE_URL"]
    if "PR_AGENT_MODEL" in env:
        args += ["--env", "PR_AGENT_MODEL"]
    args.append(cfg["image"])

    proc = subprocess.run([CONTAINER] + args, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("起容器失败: %s" % (proc.stderr or proc.stdout).strip())

    _hand_over_jit(cfg, jit, name)


def image_info(ref):
    """镜像引用解析出的 (digest, 构建它的 commit)；取不到的项为 None。

    日志里只打配置里的字面值不够用：配置写 `latest` 时，两次构建之后同一条日志一模一样。
    digest 能区分"换没换"，但要对回"换成了哪个 commit"还得翻 registry —— 而镜像自己就带着
    这个信息（build-runner-image.yml 打的 OCI label），直接读出来。
    """
    proc = _run(["image", "inspect", ref])
    if proc.returncode != 0:
        return None, None
    try:
        items = json.loads(proc.stdout)
    except ValueError:
        return None, None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None, None
    top = items[0]
    digest = top.get("id") if isinstance(top.get("id"), str) else None
    revision = None
    for variant in top.get("variants") or []:
        labels = (((variant or {}).get("config") or {}).get("config") or {}).get("Labels") or {}
        if labels.get("org.opencontainers.image.revision"):
            revision = labels["org.opencontainers.image.revision"]
            break
    return digest, revision


def sweep_stale_jit_files(cfg):
    """清掉上次被强杀时留下的临时凭据文件。

    正常路径上那个 mkstemp 文件在 finally 里必然被 unlink，只有 SIGKILL / 断电才会留残。
    入口包装读不到凭据会一直等到超时，所以残留不影响功能，但它是一份躺在盘上的凭据。
    """
    for p in Path(cfg["log_dir"]).parent.glob(".prr-jit-*"):
        try:
            p.unlink()
            log.warning("清掉上次残留的临时凭据文件 %s", p.name)
        except OSError as exc:
            log.warning("清不掉残留的 %s：%s", p.name, exc)


def _hand_over_jit(cfg, jit, name):
    """把凭据拷进容器。临时文件用 mkstemp 建（天生 0600），拷完立刻 unlink。"""
    # 路径写死，不做成配置项：容器侧入口包装读的也是这个常量（`PRR_JIT_FILE` 只是给镜像
    # 自己留的兜底），启动器又不会把那个环境变量传进去 —— 做成配置项只会让人以为能改，
    # 改了却是"拷到新路径、入口包装还在老的路上等"，表现成 90 秒超时。
    target = "%s:%s" % (name, JIT_PATH)
    fd, tmp = tempfile.mkstemp(prefix=".prr-jit-", dir=str(Path(cfg["log_dir"]).parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(jit)
        proc = _run(["cp", tmp, target])
        if proc.returncode != 0:
            raise RuntimeError("把凭据拷进容器失败: %s" % (proc.stderr or proc.stdout).strip())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def tail_container_log(name, lines=12):
    """容器结束后把它的日志尾巴捞出来，写进启动器日志——出问题时这是唯一的线索。"""
    proc = _run(["logs", name])
    text = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join("    | " + ln for ln in tail)


# ---------------------------------------------------------------- 主循环

def host_reclaimable_gb():
    """宿主机可回收内存（GB）。取不到返回 None。

    macOS 上约等于 free + inactive + speculative + purgeable —— 这部分内存系统能拿回来用，
    所以它比"已用多少"更能反映"还能不能再起容器"。要留意压缩内存：它开始涨就说明压力上来了
    （系统先压缩、后换页），代价是 CPU 和延迟 —— 而审查本来就是延迟敏感的。
    """
    try:
        ps = int(subprocess.check_output(["sysctl", "-n", "hw.pagesize"]).strip())
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    pages = {}
    for line in out.splitlines():
        m = re.match(r'\s*"?([^":]+)"?:\s+(\d+)', line)
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    total = sum(pages.get(k, 0) for k in
                ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return total * ps / 1024 ** 3 if total else None


def _list_runners(base, headers, timeout=30):
    """列出**全部** runner 记录（跟着 Link 头翻页）。

    这个列表是仓库级的、记录会随容器 churn 堆积，一页装不下很正常。不带 per_page 只看得到
    30 条，带了但只取一页则只看到 100 条 —— 两种都会漏，表现成"这几条记录怎么也清不掉"。
    """
    out = []
    url = base + "?per_page=100"
    for _ in range(20):                      # 兜底上限，别在异常情况下无限翻
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            out.extend(json.load(resp).get("runners", []))
            link = resp.headers.get("Link", "") or ""
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not m:
            break
        url = m.group(1)
    return out


def _runners_api(cfg):
    token = Path(cfg["github_token_file"]).read_text().strip()
    base = "%s/repos/%s/actions/runners" % (GITHUB_API, cfg["github_repo"])
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    return base, headers


def forget_runners(cfg, names):
    """把一批容器对应的 runner 记录删掉（best-effort）。

    一次性 runner 正常退出时会自己注销，但**被我们杀掉的不会** —— 每轮空闲回收、每次重启
    都会留下记录。容器名就是 runner 名（铸凭据时用的同一个名字），所以这里能精确删掉那几条，
    不必等定期清理去扫。

    删不到不算错：正常退出的那些记录本来就已经没了。一次列全、再逐个删，避免每个名字都列一遍。
    """
    names = set(names)
    if not names:
        return
    base, headers = _runners_api(cfg)
    try:
        runners = _list_runners(base, headers, timeout=20)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("查 runner 记录失败，这几条留到定期清理再收：%s", exc)
        return
    hit = [r for r in runners if r.get("name") in names]
    for r in hit:
        req = urllib.request.Request("%s/%s" % (base, r["id"]), headers=headers, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=20)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("删 runner 记录 %s 失败：%s", r.get("name"), exc)
    if hit:
        log.info("顺手清掉 %d 条 runner 记录", len(hit))


def live_container_names():
    """所有容器（含已停止）的名字。拿不到返回 None。"""
    proc = _run(["list", "--all", "--format", "json"])
    if proc.returncode != 0:
        return None
    try:
        items = json.loads(proc.stdout)
    except ValueError:
        return None
    names = set()
    for item in items:
        for key in ("id", "name"):
            if isinstance(item.get(key), str):
                names.add(item[key])
    return names


def cleanup_runner_records(cfg):
    """删掉"离线、且对应容器已经不存在"的 runner 记录。

    容器被销毁、启动器被强杀，GitHub 上都会留下记录 —— 正常退出的 ephemeral runner
    会自己注销，被杀的不会 —— 重启几次就会堆起来。

    判据刻意保守：只删**离线**的、且容器确实不在的。刚铸完凭据还没注册的 runner
    也是"离线"状态，但那一刻容器已经存在，所以不会被误删。
    """
    live = live_container_names()
    if live is None:
        log.warning("拿不到容器清单，跳过 runner 记录清理")
        return
    base, headers = _runners_api(cfg)
    try:
        runners = _list_runners(base, headers)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("查 runner 记录失败：%s", exc)
        return

    # 只认本实例铸出来的记录（名字前缀）。runner 列表是**仓库级**的：别的机器、别人手工注册的
    # runner 也在里面，名字同样可能不在本机容器清单里 —— 不按前缀认领就会把别人的删掉。
    prefix = cfg["name_prefix"] + "-"
    gone = [r for r in runners
            if r.get("status") != "online"
            and str(r.get("name", "")).startswith(prefix)
            and r.get("name") not in live]
    for r in gone:
        req = urllib.request.Request("%s/%s" % (base, r["id"]), headers=headers, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=30)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("删残留 runner 记录 %s 失败：%s", r.get("name"), exc)
    if gone:
        log.info("清掉 %d 条残留 runner 记录（原有 %d 条）", len(gone), len(runners))


def maintain(cfg, have_slots):
    """定期清理。

    容器是高频 churn 的（每个任务一个新容器，池子里还会每 50 分钟整体轮换一轮），
    不清理快照会一直堆。

    镜像那一步有前提：**只在至少有一个容器在跑的时候清**。否则我们自己的镜像会变成
    "无引用"被一起删掉，而重新取镜像要走 ghcr（pull_image.py），不是随手能补的。
    """
    log.info("定期清理：删掉已停止的容器")
    proc = _run(["prune"])
    out = ((proc.stdout or "") + (proc.stderr or "")).strip().replace("\n", " | ")
    if out:
        log.info("清理结果：%s", out[:300])

    if have_slots:
        log.info("定期清理：删掉无引用的镜像（此刻我们自己的镜像有容器引用着）")
        proc = _run(["image", "prune", "--all"])
        out = ((proc.stdout or "") + (proc.stderr or "")).strip().replace("\n", " | ")
        if out:
            log.info("镜像清理结果：%s", out[:300])
    else:
        log.info("池子空着，跳过镜像清理（免得把我们自己的镜像清掉）")

    cleanup_runner_records(cfg)


def start_slot(cfg, slot, seq):
    """铸凭据 → 起一个容器 → 把凭据交给它。返回容器名。

    每个容器只登记成一个"只接一个任务"的 runner，跑完自己注销退出。
    容器名先生成再拿去铸凭据 —— 让 GitHub 上的 runner 记录名 == 容器名，记录才清得掉。
    """
    name = "%s-%d-%d" % (cfg["name_prefix"], slot, int(time.time()))
    jit = mint_jit(cfg, name)
    try:
        start_container(cfg, jit, name, seq)
    except Exception:
        # 起容器是两步（先 run，再 cp 凭据进去），第二步失败时容器已经创建好了。调用方只会
        # 记一条 warning 就去补下一个槽，这个容器不在池子里、此后没人认识它 —— 不就地收掉，
        # 就得等定期清理（最长一小时）才轮到它。
        kill_and_delete(name)
        forget_runners(cfg, [name])
        raise
    finally:
        del jit                      # 凭据不再需要，尽早从内存里去掉
    return name


def manage_pool(cfg):
    """把「同时有 replicas 个待命容器」这件事维护住。

    池子里放多个的理由：每个一次性 runner 只接一个任务，而同时可能有多个任务在排队
    （审查、探针、别的 workflow）。只有一个待命容器时，谁先入队谁后入队不由我们控制 ——
    更晚入队的任务可能先被领走。所以按并发数维护一个池子。

    池子里的每个槽独立：容器结束就补，空等超时也补（避免凭据过期后还挂着）。
    """
    slots = {}                       # slot -> (容器名, 空等截止时刻)
    seq = 0
    cooldown_until = 0.0             # 铸凭据失败后的冷却，别把 GitHub API 打爆
    guard_checked_at = 0.0
    maintained_at = time.time()

    # 启动时先扫一遍：上一次进程被强杀时留下的 runner 记录，别等到一小时后那轮定期清理
    try:
        cleanup_runner_records(cfg)
    except Exception:                              # noqa: BLE001
        log.exception("启动时清理 runner 记录出错（不影响继续）")

    try:
        while not _stopping:
            now = time.time()

            # 定期清理磁盘：容器 churn 很快，不清会一直堆
            if now - maintained_at > cfg["maintenance_interval_seconds"]:
                try:
                    maintain(cfg, bool(slots))
                except Exception:                  # noqa: BLE001 —— 清理失败不该拖垮池子
                    log.exception("定期清理出错，下个周期再试")
                maintained_at = time.time()

            # 起容器前先确认两件事：隔离还在（安全）、网关还活着（可用性）。
            # 任一条不过就停掉待命容器且不再起新的 —— 这台机器要接公网 PR 的任务。
            if cfg["require_egress_guard"] and now - guard_checked_at > cfg["guard_recheck_seconds"]:
                # 两条都跑、两条都记：失败时能一眼看出是"隔离破了"还是"网关挂了"
                egress_ok, egress_detail = check_egress_guard(cfg)
                gateway_ok, gateway_detail = check_gateway(cfg)
                detail = "出口[%s] 网关[%s]" % (egress_detail, gateway_detail)
                if not (egress_ok and gateway_ok):
                    log.error("启动前自检没过：%s", detail)
                    # 修法要按**具体哪一条**给。以前不管哪种失败都写"隔离修法：pf-setup.sh"，
                    # 而"筛不出可测的宿主端口"根本不是 pf 的问题（是端口探测没拿到东西），
                    # 照着去重跑 pf-setup 只会白跑一趟，还错过真正的线索。
                    log.error("停掉待命容器且不再起新的，按下面具体那条修：")
                    if not egress_ok:
                        log.error("  出口自检不过（%s）。提示是「能连到宿主机端口」→ "
                                  "bash ~/prreview/pf-setup.sh；提示是「筛不出可测的宿主端口」→ "
                                  "那是端口探测没拿到东西，先看 netstat -an -f inet 的输出。",
                                  egress_detail)
                    if not gateway_ok:
                        log.error("  网关自检不过（%s）。修法：launchctl kickstart -k gui/%s/com.prreview.gateway",
                                  gateway_detail, os.getuid())
                    # 同一条判据：只收空闲的。自检没过不该牵连正在跑任务的容器 —— 杀它 = 掐断那个
                    # 任务，且 GitHub 拒绝删"正在跑任务的 runner"记录（422），那条会占住该 PR 的
                    # concurrency 组直到 job 自己超时（与退出收尾处同理）。
                    names = [n for n, _ in slots.values()]
                    # `is not False`：查不出来的一律当"可能正在跑"，不动它
                    busy = [n for n in names if container_running_job(n) is not False]
                    idle = [n for n in names if n not in busy]
                    if busy:
                        log.warning("%d 个容器正在跑任务，不杀它们，让它们跑完自己退出：%s",
                                    len(busy), ", ".join(busy))
                    for name in idle:
                        kill_and_delete(name)
                    # 跑着任务的那几个槽留着，等容器自然结束由回收逻辑收（否则就成孤儿了）
                    slots = {s: v for s, v in slots.items() if v[0] in busy}
                    forget_runners(cfg, idle)
                    time.sleep(60)
                    continue
                guard_checked_at = time.time()
                log.info("启动前自检通过：%s", detail)

            # 回收：容器已经结束的、或空等超时的。收掉容器时顺手把它的 runner 记录也删了 ——
            # 被我们杀掉的容器不会自己注销，不删就堆着。
            for slot, (name, deadline) in list(slots.items()):
                state = container_state(name)
                if state == "unknown":
                    # "问不出来"不是"已经没了"。这一支里动手就是拿没问到当没有，可能掐掉一个
                    # 正在跑任务的容器。留着，下一轮再问。
                    log.warning("槽 %d：容器 %s 状态查不出来，本轮不动它", slot, name)
                    continue
                if state != "running":
                    log.info("槽 %d：容器 %s 已结束（state=%s），日志尾部如下\n%s",
                             slot, name, state, tail_container_log(name))
                    kill_and_delete(name)
                    del slots[slot]
                    forget_runners(cfg, [name])
                elif time.time() >= deadline:
                    # 空等超时的回收**只看时钟是不够的**：作业可能正好落在容器生命的最后一段，
                    # 这时容器还在跑任务，按时钟收掉就是把它掐断。判据与另外两处（自检不过、
                    # 进程退出收尾）一致：正在跑任务的不收。
                    if container_running_job(name) is not False:
                        # 截止时刻往后推，免得每轮轮询都去读一次容器日志。任务跑完 runner 会自己
                        # 退出，那时由上面「已结束」那条分支收，推后的截止时刻不会真的用到。
                        slots[slot] = (name, time.time() + 60)
                        log.info("槽 %d：容器 %s 已过空等时限但正在跑任务，放它跑完", slot, name)
                    else:
                        log.info("槽 %d：空等超时，回收 %s（避免凭据过期后还挂着）", slot, name)
                        kill_and_delete(name)
                        del slots[slot]
                        forget_runners(cfg, [name])

            # 补齐空的槽 —— 但先看宿主内存水位：宁可少接活，也不把机器压进压缩/换页
            free_gb = host_reclaimable_gb()
            if free_gb is not None and free_gb < cfg["min_free_gb"]:
                log.warning("宿主可回收内存只剩 %.2f GB（阈值 %.2f），本轮不补新槽",
                            free_gb, cfg["min_free_gb"])
            elif time.time() >= cooldown_until:
                for slot in range(cfg["replicas"]):
                    if _stopping or slot in slots:
                        continue
                    seq += 1
                    try:
                        name = start_slot(cfg, slot, seq)
                    except Exception as exc:      # noqa: BLE001 —— 单槽失败不该拖垮池子
                        log.warning("槽 %d：起容器失败（%s: %s），30 秒后再试",
                                    slot, type(exc).__name__, str(exc)[:200])
                        cooldown_until = time.time() + 30
                        break
                    slots[slot] = (name, time.time() + cfg["idle_recycle_seconds"])
                    log.info("槽 %d：容器 %s 已起（在跑 %d/%d，宿主可回收 %.2f GB）",
                             slot, name, len(slots), cfg["replicas"], free_gb or 0)

            # 用短睡轮询，别直接 sleep(poll_seconds)：SIGTERM 之后要立刻能进收尾，
            # 否则 launchd 会在我们收干净容器之前把进程杀掉，留下一批孤儿容器。
            for _ in range(max(1, int(cfg["poll_seconds"] * 2))):
                if _stopping:
                    break
                time.sleep(0.5)
    finally:
        # 收尾：**空闲的容器收掉，正在跑任务的放它跑完**（收到 TERM 时 launchd 会等着我们做完）。
        #
        # 不杀正在跑任务的容器的理由：杀了 = 掐断那个任务，而且后果远不止于此 ——
        # GitHub 会**拒绝删除"正在跑任务的 runner"记录**（422），那条记录会一直占着它所在的
        # concurrency 组，把同一 PR 的后续 run 全堵在 pending，直到那个 job 自己超时。
        #
        # 一次性 runner 跑完一个任务就自己退出并注销，所以"放它跑完"是有界的（上界就是 job
        # 本身的超时）。停下来的容器由定期清理的 `container prune` 收走。
        names = [n for n, _ in slots.values()]
        busy = [n for n in names if container_running_job(n) is not False]   # 查不出来的一律不动
        idle = [n for n in names if n not in busy]
        if busy:
            log.info("%d 个容器正在跑任务，不杀它们，让它们跑完自己退出：%s",
                     len(busy), ", ".join(busy))
        for name in idle:
            log.info("退出中：收掉空闲容器 %s", name)
            kill_and_delete(name)
        slots.clear()
        # 只清空闲那批的记录；跑着任务的那批会在自己退出时注销，此刻删也删不掉（422）
        forget_runners(cfg, idle)


def main():
    # 必须能接受 --config：launchd 的 LaunchAgent **不继承调用方 shell 的环境变量**，
    # 只靠 PRR_CONF 的话，装机时用了非默认路径就会变成「网关读你指定的那份、启动器读默认
    # 路径」，读不到就退出，KeepAlive 每 30 秒再拉起来挂一次。
    ap = argparse.ArgumentParser(description="一次性 runner 启动器")
    ap.add_argument("--config", default=os.environ.get("PRR_CONF", str(DEFAULT_CONFIG)))
    cfg_path = Path(ap.parse_args().config)
    try:
        cfg = load_config(cfg_path)
    except (OSError, ValueError) as exc:
        raise SystemExit("读配置失败（%s）: %s" % (cfg_path, exc))

    log_dir = Path(cfg["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [launcher] %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(str(log_dir / "launcher.log")),
            logging.StreamHandler(sys.stderr),
        ],
    )

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    sweep_stale_jit_files(cfg)

    digest, revision = image_info(cfg["image"])
    log.info("启动：镜像=%s digest=%s commit=%s 网络=%s 仓库=%s 标签=%s 待命容器=%d 空闲回收=%ds",
             cfg["image"], digest or "未解析",
             revision[:12] if revision else "未标注（镜像没带 revision label）",
             cfg["network"], cfg["github_repo"], ",".join(cfg["runner_labels"]),
             cfg["replicas"], cfg["idle_recycle_seconds"])
    # 标签是唯一让 GitHub 把作业派到这台机器上的东西：runner 报的标签与 workflow 的 runs-on
    # 对不上时**不报错**，表现是作业一直排队、这台机器一直空闲。所以把它打出来，对得上眼见为实。
    log.info("（workflow 的 runs-on 必须包含上面这些标签，否则作业永远排队、这里看着一切正常）")

    # 池子循环万一因为意外异常退出，重建它 —— 但不能空转刷日志，所以加个间隔
    while not _stopping:
        try:
            manage_pool(cfg)
        except Exception:                          # noqa: BLE001
            log.exception("池子循环异常退出，30 秒后重建")
            time.sleep(30)

    log.info("已退出")


if __name__ == "__main__":
    main()
