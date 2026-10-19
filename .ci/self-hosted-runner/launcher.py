#!/usr/bin/env python3
"""一次性 runner 启动器 —— 跑在运行机上，由 launchd 托管。

每轮：要一份登记凭据 → 起容器交给它 → 等退出 → 补下一个。容器参数写死，任务没有话语权。
两条不能破：凭据不进日志/命令行/环境变量，只经 0600 临时文件且拷完即删；容器寿命不超过
凭据寿命（空等超过 idle_recycle_seconds 就回收）。只用标准库。
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

# 收到 TERM/INT 时置位，主循环据此收尾；不收容器就会留下孤儿
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
    # 对不上时 GitHub 不报错，只是永远不派活。
    cfg.setdefault("runner_labels", ["self-hosted", "linux", "arm64", "prr-test"])
    # 出口自检：起 runner 之前先确认容器连不上宿主上临时开的端口。默认开。
    cfg.setdefault("require_egress_guard", True)
    cfg.setdefault("guard_recheck_seconds", 900)
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
# 自检用的探针。两条连接，缺一不可：
#   control —— 连网关那个**必须通**的端口，证明探针自己能跑（bash 有 /dev/tcp、timeout 在）。
#              没有它，"连不上" 就同时代表"被挡住"和"探针坏了"，前者才能判通过。
#   canary  —— 连启动器刚开的临时端口，**必须不通**。
GUARD_PROBE = (
    'c=0; k=0; '
    'timeout 4 bash -c "exec 3<>/dev/tcp/{gw}/{control}" 2>/dev/null && c=1; '
    'timeout 4 bash -c "exec 3<>/dev/tcp/{gw}/{canary}" 2>/dev/null && k=1; '
    'echo "control=$c canary=$k"'
)


def check_egress_guard(cfg):
    """起一个一次性容器，看它能不能连上宿主上刚开的临时端口。

    连得上说明 pf 那层不在了 —— 此时起 runner 等于把网络摊在公网 PR 面前，宁可不干活。
    返回 (ok, 说明)。
    """
    srv, port = prrlib.canary_port()
    try:
        probe = GUARD_PROBE.format(gw=cfg["host_gateway"],
                                   control=cfg["github_proxy_port"], canary=port)
        args = ["run", "--rm", "--arch", "arm64", "--network", cfg["network"],
                "--entrypoint", "/bin/bash", cfg["image"], "-c", probe]
        proc = subprocess.run([CONTAINER] + args, capture_output=True,
                              text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "自检没跑起来: %s" % exc
    finally:
        srv.close()
    out = proc.stdout or ""
    if "control=1" not in out:
        # 正向对照没通：探针跑不起来（缺 timeout / 无 /dev/tcp）或网关端口不通。
        # 此时"连不上临时端口"证明不了任何事，不能判通过。
        return False, "自检没得出有效结果(%d): %s" % (proc.returncode, (proc.stderr or out).strip()[:200])
    if "canary=1" in out:
        return False, ("容器连上了宿主机上的临时端口 %d —— pf 那层不在了。"
                       "修法：bash ~/prreview/pf-setup.sh") % port
    return True, "宿主端口已封住"


def check_gateway(cfg):
    """我们的出网网关还在监听吗。

    与出口自检是两件事：出口自检管安全（隔离还在不在），这里管可用性（代理还活着没）。
    少了这一条，容器会注册成功、领到任务，审查时连不上模型，白白吃掉一个任务。
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
    """要一份一次性登记凭据（跑在宿主机：容器没有直连 api.github.com 的路由）。

    name 用容器名，GitHub 上的 runner 记录才能和容器一一对上，强杀留下的记录才清得掉。
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
    """容器里的 runner 是不是正在跑任务（看日志最后一行）。

    三态：True 在跑 / False 明确没在跑 / **None 问不出来**。调用方必须把 None 当"可能正在跑"：
    当成"没在跑"会掐断在跑的作业，并留下一条 GitHub 拒绝删（422）的记录堵住其 concurrency 组。
    """
    proc = _run(["logs", name])
    # **先看退出码**：这才是"这次调用成没成"的主判据。只看 stdout 空不空等于赌 CLI 把报错
    # 写在哪条流上，赌错了就在"命令失败"时返回 False（"确定没在跑"）。
    if proc.returncode != 0:
        return None
    # **只看 stdout**：容器日志在 stdout，stderr 是 CLI 的报错。拼起来的话"最后一行"会变成
    # stderr 的最后一行，"跑作业期间最后一行是 Running job:"这条判据就没了保证。
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None                      # 还没吐出任何日志（刚起来）→ 也算问不出来，别当"没在跑"
    return "Running job:" in lines[-1]


def container_state(name):
    """容器状态：running/stopped/...；**确实没有这个容器**返回 None；**问不出来**返回 "unknown"。

    三态必须分开：只看 inspect 退出码不行（容器停止后照样返回 0，看护循环会空等到回收时限）；
    把"问不出来"并进"已经没了"则会掐掉正在跑任务的容器，并留下 422 删不掉的记录。
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

    凭据用 `container cp` 拷进去，不走命令行参数（会进 `ps`）也不走环境变量（会进容器记录的
    `config.json`，删容器不清记录）。容器形态全部写死：不挂宿主目录、不开容器 socket、加限额。
    """
    env = dict(os.environ)
    env["HTTP_PROXY"] = "http://%s:%s" % (cfg["host_gateway"], cfg["github_proxy_port"])
    env["HTTPS_PROXY"] = env["HTTP_PROXY"]
    env["NO_PROXY"] = "%s,localhost,127.0.0.1" % cfg["host_gateway"]

    # 模型环境：容器只拿到"代理地址 + 假钥匙 + 模型名"，真 key 在宿主机代理里，永不进容器。
    # 这几项走环境变量会进容器记录，可以接受：假钥匙只在本地代理上有效，且代理限了来源网段。
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

    配置写 `latest` 时，两次构建后同一条日志一模一样。镜像自带 build-runner-image.yml 打的
    OCI label，直接读出来才能对上"换成了哪个 commit"。
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


def sweep_orphan_containers(cfg):
    """收掉上一次留下的容器。

    正常退出会先把容器收干净，但「补槽」与「收到 TERM」之间有个窗口：那一刻新建的容器
    既不在收尾快照里、也没进 slots，于是没人跟踪。它不会自己退出（runner 在等作业），
    会一直占着一个槽位和一条 runner 记录。启动时按名字前缀全收一遍。
    """
    prefix = cfg["name_prefix"] + "-"
    for name in live_container_names() or ():
        if isinstance(name, str) and name.startswith(prefix):
            log.warning("收掉上次遗留的容器 %s", name)
            kill_and_delete(name)
            forget_runners(cfg, [name])


def sweep_stale_jit_files(cfg):
    """清掉进程被强杀时留下的临时凭据文件。

    正常路径上 mkstemp 文件在 finally 里必然被 unlink，只有 SIGKILL/断电才留残。
    残留不影响功能，但它是一份躺在盘上的凭据。
    """
    for p in Path(cfg["log_dir"]).parent.glob(".prr-jit-*"):
        try:
            p.unlink()
            log.warning("清掉上次残留的临时凭据文件 %s", p.name)
        except OSError as exc:
            log.warning("清不掉残留的 %s：%s", p.name, exc)


def _hand_over_jit(cfg, jit, name):
    """把凭据拷进容器。临时文件用 mkstemp 建（天生 0600），拷完立刻 unlink。"""
    # 路径写死不做成配置项：容器侧入口包装读的也是这个常量，改了就是"拷到新路径、入口包装
    # 还在老路上等"，表现成 90 秒超时。
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

    可回收 ≈ free + inactive + speculative + purgeable —— 比"已用多少"更能反映还能不能再起
    容器；压缩内存开始涨说明压力上来了（系统先压缩、后换页）。
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

    记录是仓库级的、随容器 churn 堆积，一页装不下很正常；只取一页会漏，表现成
    "这几条记录怎么也清不掉"。
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

    一次性 runner 正常退出会自己注销，被我们杀掉的不会。容器名 == runner 名，所以能精确删掉
    那几条；删不到不算错。一次列全、再逐个删。
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

    容器被销毁、启动器被强杀都会留下记录。判据刻意保守：刚铸完凭据还没注册的 runner 也是
    离线状态，但那一刻容器已经存在，所以不会被误删。
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
    # 也在里面，不按前缀认领会把别人的删掉。
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

    容器高频 churn，不清快照会一直堆。镜像那一步只在至少有一个容器在跑时做：否则我们自己的
    镜像会变成"无引用"被一起删掉，重新取镜像不是随手能补的。
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

    容器名先生成再拿去铸凭据，让 GitHub 上的 runner 记录名 == 容器名，记录才清得掉。
    """
    name = "%s-%d-%d" % (cfg["name_prefix"], slot, int(time.time()))
    jit = mint_jit(cfg, name)
    try:
        start_container(cfg, jit, name, seq)
    except Exception:
        # 起容器是两步（先 run，再 cp 凭据），第二步失败时容器已创建好。不就地收掉，就得等
        # 定期清理（最长一小时）才轮到它。
        kill_and_delete(name)
        forget_runners(cfg, [name])
        raise
    finally:
        del jit                      # 凭据不再需要，尽早从内存里去掉
    return name


def manage_pool(cfg):
    """把「同时有 replicas 个待命容器」这件事维护住。

    每个一次性 runner 只接一个任务，而同时可能有多个任务排队，所以要按并发数维护池子。
    每个槽独立：容器结束就补，空等超时也补（避免凭据过期后还挂着）。
    """
    slots = {}                       # slot -> (容器名, 空等截止时刻)
    seq = 0
    cooldown_until = 0.0             # 铸凭据失败后的冷却，别把 GitHub API 打爆
    guard_checked_at = 0.0
    maintained_at = time.time()

    # 启动时先扫一遍：进程被强杀会留下 runner 记录，别等到一小时后的定期清理
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
            # 任一条不过就停掉待命容器且不再起新的 —— 要接公网 PR 的任务。
            if cfg["require_egress_guard"] and now - guard_checked_at > cfg["guard_recheck_seconds"]:
                # 两条都跑、两条都记：失败时能一眼看出是"隔离破了"还是"网关挂了"
                egress_ok, egress_detail = check_egress_guard(cfg)
                gateway_ok, gateway_detail = check_gateway(cfg)
                detail = "出口[%s] 网关[%s]" % (egress_detail, gateway_detail)
                if not (egress_ok and gateway_ok):
                    log.error("启动前自检没过：%s", detail)
                    # 修法按**具体哪一条**给：「筛不出可测的宿主端口」不是 pf 的问题（是端口
                    # 探测没拿到东西），照着重跑 pf-setup 只会白跑一趟，还错过真正的线索。
                    log.error("停掉待命容器且不再起新的，按下面具体那条修：")
                    if not egress_ok:
                        # 只原样打出，不按文案分流：分流要匹配消息里的字面量，消息一改就静默
                        # 失效。修法由 check_egress_guard 写在 detail 里。
                        log.error("  出口自检不过 —— %s", egress_detail)
                    if not gateway_ok:
                        log.error("  网关自检不过（%s）。修法：launchctl kickstart -k gui/%s/com.prreview.gateway",
                                  gateway_detail, os.getuid())
                    # 同一条判据：只收空闲的。杀正在跑任务的容器 = 掐断那个任务，且 GitHub
                    # 拒绝删"正在跑任务的 runner"记录（422），会占住该 PR 的 concurrency 组。
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

            # 回收：容器已结束的、或空等超时的。收掉容器时顺手删它的 runner 记录 ——
            # 被我们杀掉的容器不会自己注销，不删就堆着。
            for slot, (name, deadline) in list(slots.items()):
                state = container_state(name)
                if state == "unknown":
                    # "问不出来"不是"已经没了"：这一支动手就可能掐掉正在跑任务的容器。
                    # 留着，下一轮再问。
                    log.warning("槽 %d：容器 %s 状态查不出来，本轮不动它", slot, name)
                    continue
                if state != "running":
                    log.info("槽 %d：容器 %s 已结束（state=%s），日志尾部如下\n%s",
                             slot, name, state, tail_container_log(name))
                    kill_and_delete(name)
                    del slots[slot]
                    forget_runners(cfg, [name])
                elif time.time() >= deadline:
                    # 空等超时只看时钟不够：作业可能正好落在容器生命的最后一段。判据与另外
                    # 两处（自检不过、进程退出收尾）一致：正在跑任务的不收。
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
        # 收尾：**空闲的容器收掉，正在跑任务的放它跑完**（launchd 会等我们收完）。杀正在
        # 跑任务的 = 掐断作业，且 GitHub 拒绝删这类记录（422），会堵住 concurrency 组直到
        # job 自己超时。一次性 runner 跑完就退出，所以"放它跑完"有界。
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
    # 必须能接受 --config：launchd 的 LaunchAgent 不继承调用方 shell 的环境变量，只靠 PRR_CONF
    # 的话，装机用了非默认路径就会变成网关读指定的那份、启动器读默认路径，读不到就退出，
    # KeepAlive 反复拉起。
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
    sweep_orphan_containers(cfg)

    digest, revision = image_info(cfg["image"])
    log.info("启动：镜像=%s digest=%s commit=%s 网络=%s 仓库=%s 标签=%s 待命容器=%d 空闲回收=%ds",
             cfg["image"], digest or "未解析",
             revision[:12] if revision else "未标注（镜像没带 revision label）",
             cfg["network"], cfg["github_repo"], ",".join(cfg["runner_labels"]),
             cfg["replicas"], cfg["idle_recycle_seconds"])
    # 标签对不上时 GitHub **不报错**，只是作业永远排队、这里看着一切正常，所以打出来供核对。
    log.info("（workflow 的 runs-on 必须包含上面这些标签，否则作业永远排队、这里看着一切正常）")

    # 池子循环意外退出就重建 —— 加个间隔，别空转刷日志
    while not _stopping:
        try:
            manage_pool(cfg)
        except Exception:                          # noqa: BLE001
            log.exception("池子循环异常退出，30 秒后重建")
            time.sleep(30)

    log.info("已退出")


if __name__ == "__main__":
    main()
