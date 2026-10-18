#!/usr/bin/env python3
"""从 registry 拉审查镜像。

public 包不需要任何凭据；私有包才要带 read:packages 的凭据文件（配置里 registry_token_file，
配合 --login-user）。拉下来的镜像有 digest，可以写进配置钉死（`<镜像>@sha256:...`）。

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

    # 打出摘要，方便核对拉到的是哪一份
    listing = run(["image", "list"])
    for line in (listing.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and reference.split(":")[0] in parts[0]:
            print("digest(短): %s" % parts[-1])
            print("要钉死版本，优先用构建时按 commit 打的 tag（:latest 会随重建前移，"
                  "而 :<commit-sha> 不可变）；按 digest 也行，但多架构镜像的索引 digest "
                  "要从 `container image inspect` 里取，别用上面这个短摘要拼引用。")
            break


if __name__ == "__main__":
    sys.exit(main())
