#!/usr/bin/env python3
"""把审查镜像取到本机 —— 从 registry 拉。

只有一条路：构建在 CI 完成、推到 registry，运行机拉下来。
比"用 artifact 传一份几百 MB 的 tar 再 load"少一大截动作，而且拉下来的镜像有 digest，
可以写进配置钉死（`<镜像>@sha256:...`）。

包是 public 时**不需要任何凭据**；只有私有包才要一个带 read:packages 的凭据文件
（配置里 `registry_token_file`，给了就登录，不给就按匿名拉）。
公开仓出来的镜像里没有秘密，所以正常情况就是匿名那条。

用法:
    python3 pull_image.py [--config ~/prreview/prreview.json] [--reference <镜像引用>]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CONTAINER = os.environ.get("PRR_CONTAINER_BIN", "/usr/local/bin/container")


def run(args, **kw):
    return subprocess.run([CONTAINER] + args, capture_output=True, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path.home() / "prreview" / "prreview.json"))
    ap.add_argument("--reference", default=None, help="不给就读配置里的 image")
    ap.add_argument("--login-user", default=None, help="私有包才需要")
    args = ap.parse_args()

    cfg = {}
    cfg_path = Path(os.path.expanduser(args.config))
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text())
    reference = args.reference or cfg.get("image")
    if not reference:
        raise SystemExit("没拿到镜像引用：给 --reference，或在配置里写 image")

    # 私有包才需要登录；public 包直接拉。凭据从文件读，不进命令行。
    token_file = cfg.get("registry_token_file")
    if token_file and args.login_user:
        token = Path(os.path.expanduser(token_file)).read_text().strip()
        proc = subprocess.run([CONTAINER, "registry", "login", "ghcr.io",
                               "-u", args.login_user, "--password-stdin"],
                              input=token, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit("登录 registry 失败: %s" % (proc.stderr or proc.stdout).strip())
        print("已登录 ghcr.io")

    print("拉取 %s …" % reference)
    proc = run(["image", "pull", reference])
    tail = [ln for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if ln.strip()]
    for line in tail[-4:]:
        print("  " + line)
    if proc.returncode != 0:
        raise SystemExit(
            "拉取失败。若是私有包：把包设成 public，或在配置里给 registry_token_file + --login-user")

    # 把 digest 打出来，好写进配置钉住
    listing = run(["image", "list"])
    for line in (listing.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and reference.split(":")[0] in parts[0]:
            print("digest: %s" % parts[-1])
            print("想钉住的话，把配置里的 image 换成 %s@%s"
                  % (reference.split(":")[0], parts[-1]))
            break


if __name__ == "__main__":
    sys.exit(main())
