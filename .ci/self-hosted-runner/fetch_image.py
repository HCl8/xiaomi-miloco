#!/usr/bin/env python3
"""把 CI 构建好的镜像取到本机 —— 不需要 registry 凭据。

走仓库自己的 artifact：CI 除了推 ghcr，还会额外出一个 arm64 的 OCI tar。
artifact 用现有的仓库 token 就能下，不用去动 ghcr 上那个包的可见性。

用法:
    python3 fetch_image.py [--run <run-id>] [--config ~/prreview/prreview.json]
                           [--keep-tar] [--dry-run]

只依赖标准库。凭据从配置里指的 token 文件读，不落任何临时凭据。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CONTAINER = os.environ.get("PRR_CONTAINER_BIN", "/usr/local/bin/container")
API = "https://api.github.com"
WORKFLOW_NAME = "Build Runner Image"
ARTIFACT_NAME = "prr-review-arm64-oci"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自作主张跟跳转。

    artifact 下载会 302 到对象存储的带签名地址。默认的跳转处理会把 Authorization
    头一起带过去——等于把仓库 token 送给第三方存储。所以自己接跳转，第二步不带凭据。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_get(url, token, raw=False):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.read() if raw else json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location")
        raise


def latest_successful_run(repo, token, run_id=None):
    if run_id:
        return api_get("%s/repos/%s/actions/runs/%s" % (API, repo, run_id), token)
    data = api_get("%s/repos/%s/actions/runs?status=success&per_page=50" % (API, repo), token)
    for run in data.get("workflow_runs", []):
        if run.get("name") == WORKFLOW_NAME:
            return run
    raise SystemExit("没找到成功的 %s 运行记录" % WORKFLOW_NAME)


def find_artifact(repo, token, run_id):
    data = api_get("%s/repos/%s/actions/runs/%s/artifacts" % (API, repo, run_id), token)
    for art in data.get("artifacts", []):
        if art.get("name") == ARTIFACT_NAME and not art.get("expired"):
            return art
    raise SystemExit("这次运行里没有 %s（是不是还没跑完，或者 artifact 过期了）" % ARTIFACT_NAME)


def download_artifact(repo, token, artifact_id, dest_dir):
    """下 artifact 并解开，返回里面那个 tar 的路径。"""
    url = "%s/repos/%s/actions/artifacts/%s/zip" % (API, repo, artifact_id)
    signed = api_get(url, token)
    if not isinstance(signed, str):
        raise SystemExit("拿不到下载地址（服务端返回的不是跳转）")

    # 第二步刻意不带 Authorization：那是对象存储的带签名地址，凭据不该跟过去
    zip_path = Path(dest_dir) / "artifact.zip"
    with urllib.request.urlopen(signed, timeout=600) as resp, open(zip_path, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".tar")]
        if not names:
            raise SystemExit("artifact 里没有 tar：%s" % zf.namelist())
        zf.extract(names[0], dest_dir)
        return Path(dest_dir) / names[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path.home() / "prreview" / "prreview.json"))
    ap.add_argument("--run", type=int, default=None, help="指定运行 id，不给就用最近一次成功的")
    ap.add_argument("--keep-tar", action="store_true", help="保留下载的 tar")
    ap.add_argument("--dry-run", action="store_true", help="只报告会做什么，不动镜像")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    repo = cfg["github_repo"]
    token = Path(os.path.expanduser(cfg["github_token_file"])).read_text().strip()

    run = latest_successful_run(repo, token, args.run)
    print("运行 %s（%s，commit %s）" % (run["id"], run.get("status"), str(run.get("head_sha"))[:8]))

    art = find_artifact(repo, token, run["id"])
    print("artifact: %s  %.1f MB" % (art["name"], art["size_in_bytes"] / 1e6))

    if args.dry_run:
        print("（dry-run，到此为止）")
        return

    with tempfile.TemporaryDirectory(prefix="prr-fetch-") as tmp:
        tar = download_artifact(repo, token, art["id"], tmp)
        print("已下载: %s  %.1f MB" % (tar.name, tar.stat().st_size / 1e6))
        proc = subprocess.run([CONTAINER, "image", "load", "-i", str(tar)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit("载入镜像失败: %s" % (proc.stderr or proc.stdout).strip())
        print("载入完成: %s" % (proc.stdout or proc.stderr).strip())
        if args.keep_tar:
            keep = Path.home() / "prreview" / tar.name
            keep.write_bytes(tar.read_bytes())
            print("tar 已保留: %s" % keep)

    proc = subprocess.run([CONTAINER, "image", "list"], capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        if "prr-review" in line:
            print("镜像: %s" % line.strip())


if __name__ == "__main__":
    main()
