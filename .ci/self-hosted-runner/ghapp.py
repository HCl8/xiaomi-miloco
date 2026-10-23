#!/usr/bin/env python3
"""用 GitHub App 私钥现签 installation token，可选地打一遍接口做自检。

链路：私钥 → JWT（≤10 分钟）→ installation token（1 小时）→ REST API。
签名交给系统自带的 openssl，运行机上不需要装任何 Python 包。

用法：
  python3 ghapp.py --app-id 12345 --key ~/prreview/secrets/github-app.pem
  python3 ghapp.py --app-id 12345 --key ... --check --repo owner/name

不给 --installation 时自动取：只有一处安装就直接用，多处则列出来让你指定。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UA = "prr-ghapp/1"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_jwt(app_id: str, key_path: Path) -> str:
    """签一个 9 分钟有效的 JWT。

    iat 往前挪 60 秒：宿主机时钟比 GitHub 快一点时，紧贴 now 会被判 "'Issued at'
    claim is in the future"，报错完全看不出是时钟问题。exp 不贴 10 分钟上限同理。
    """
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    signing_input = "%s.%s" % (
        b64url(json.dumps(header, separators=(",", ":")).encode()),
        b64url(json.dumps(payload, separators=(",", ":")).encode()),
    )
    try:
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
            input=signing_input.encode(), capture_output=True, check=True,
        ).stdout
    except FileNotFoundError:
        raise RuntimeError("找不到 openssl") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("签名失败（私钥不对或格式不是 PEM）："
                           + exc.stderr.decode(errors="replace").strip()) from None
    return signing_input + "." + b64url(sig)


def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")[:300]


def list_installations(jwt: str) -> list[dict]:
    """这个 App 的所有安装。App ID 与私钥不配对时 GitHub 回 404。"""
    code, payload = api("GET", "/app/installations", jwt)
    if code != 200:
        raise RuntimeError("列不出安装记录（HTTP %s）：%s\n检查 App ID 与私钥是否配对" % (code, payload))
    return payload if isinstance(payload, list) else []


def _choose(installs: list[dict], wanted: str | None) -> str:
    if not installs:
        raise RuntimeError("这个 App 还没安装到任何账号 —— 先去 App 设置页点 Install")
    if wanted:
        if any(str(it.get("id")) == str(wanted) for it in installs):
            return str(wanted)
        raise RuntimeError("安装 id %s 不在列表里，现有：%s"
                           % (wanted, [it.get("id") for it in installs]))
    if len(installs) == 1:
        return str(installs[0]["id"])
    raise RuntimeError("这个 App 有多处安装（%s），用 --installation 指定"
                       % [it.get("id") for it in installs])


def installation_token(app_id, key_path, installation=None, permissions=None):
    """签 JWT → 换一枚 installation token，返回 (token, 响应体)。

    启动器直接调它：真凭据不落盘，一小时自动失效。
    permissions 不传就用 App 被授予的全部权限——下载窄会在这里报 422，配置和 App 一旦对不上
    就是启动失败，没有必要为此多加一个要维护的开关。
    """
    jwt = make_jwt(str(app_id), Path(key_path))
    inst = str(installation) if installation else _choose(list_installations(jwt), None)
    body = {"permissions": permissions} if permissions else None
    code, payload = api("POST", "/app/installations/%s/access_tokens" % inst, jwt, body=body)
    if code not in (200, 201):
        raise RuntimeError("换 installation token 失败（HTTP %s）：%s" % (code, payload))
    return payload["token"], payload


def pick_installation(jwt: str, wanted: str | None) -> str:
    installs = list_installations(jwt)
    inst = _choose(installs, wanted)
    it = next((i for i in installs if str(i.get("id")) == inst), {})
    print("  安装：id=%s  账号=%s  范围=%s"
          % (inst, (it.get("account") or {}).get("login"), it.get("repository_selection")))
    return inst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--installation", default=None)
    ap.add_argument("--repo", default=None, help="owner/name，配合 --check")
    ap.add_argument("--org", default=None, help="组织名；给了就走组织级接口（org 级 App 用）")
    ap.add_argument("--group", default=None, help="runner group id（组织级 JIT 必填）")
    ap.add_argument("--check", action="store_true", help="打一遍 runner 相关接口")
    ap.add_argument("--jwt-only", action="store_true", help="只打印 JWT（排签名问题时用）")
    args = ap.parse_args()

    if not args.key.is_file():
        sys.exit("私钥文件不存在：%s" % args.key)

    try:
        jwt = make_jwt(args.app_id, args.key)
    except RuntimeError as exc:
        sys.exit(str(exc))
    if args.jwt_only:
        print(jwt)
        return 0
    print("JWT 已签（9 分钟有效）")

    try:
        inst = pick_installation(jwt, args.installation)
        token, payload = installation_token(args.app_id, args.key, inst)
    except RuntimeError as exc:
        sys.exit(str(exc))
    print("  token：%s…（%s 过期）" % (token[:4] + "…" + token[-4:], payload.get("expires_at")))
    print("  实际授予：%s" % json.dumps(payload.get("permissions"), ensure_ascii=False))

    if not args.check:
        return 0
    if not args.repo and not args.org:
        sys.exit("--check 需要 --repo owner/name 或 --org <组织>")
    bad = 0

    if args.org:
        org = args.org.strip("/")
        print("\n自检组织 %s：" % org)
        code, payload = api("GET", "/orgs/%s/actions/runner-groups" % org, token)
        groups = (payload.get("runner_groups") or []) if isinstance(payload, dict) else []
        print("  GET  …/runner-groups        %s  %d 个" % (code, len(groups)))
        for g in groups:
            print("       id=%-4s %-16s visibility=%-9s default=%s"
                  % (g.get("id"), g.get("name"), g.get("visibility"), g.get("default")))
        bad += code != 200

        code, payload = api("GET", "/orgs/%s/actions/runners" % org, token)
        total = payload.get("total_count") if isinstance(payload, dict) else None
        print("  GET  …/actions/runners       %s  runners=%s" % (code, total))
        bad += code != 200

        if args.group:
            code, payload = api("POST", "/orgs/%s/actions/runners/generate-jitconfig" % org, token,
                                body={"name": "prr-probe-%d" % int(time.time()),
                                      "runner_group_id": int(args.group),
                                      "labels": ["prr-probe"]})
            ok = code in (200, 201) and isinstance(payload, dict) and payload.get("encoded_jit_config")
            print("  POST …/generate-jitconfig    %s  %s"
                  % (code, "拿到 encoded_jit_config" if ok else str(payload)[:160]))
            bad += not ok
        else:
            print("  （没给 --group，跳过 JIT 自检）")

        print("\n%s" % ("全部通过" if not bad else "有 %d 项没通过" % bad))
        return 0 if not bad else 1

    repo = args.repo.strip("/")
    print("\n自检 %s：" % repo)

    code, payload = api("GET", "/repos/%s" % repo, token)
    perm = payload.get("permissions") if isinstance(payload, dict) else None
    print("  GET  /repos/…                %s  %s" % (code, perm or str(payload)[:120]))

    code, payload = api("GET", "/repos/%s/actions/runners" % repo, token)
    total = payload.get("total_count") if isinstance(payload, dict) else None
    print("  GET  …/actions/runners       %s  runners=%s" % (code, total))
    bad += code != 200

    code, payload = api("POST", "/repos/%s/actions/runners/registration-token" % repo, token)
    print("  POST …/registration-token    %s%s" % (code, "" if code in (200, 201) else "  " + str(payload)[:160]))
    bad += code not in (200, 201)

    code, payload = api("POST", "/repos/%s/actions/runners/generate-jitconfig" % repo, token,
                        body={"name": "prr-probe-%d" % int(time.time()), "runner_group_id": 1,
                              "labels": ["prr-probe"]})
    ok = code in (200, 201) and isinstance(payload, dict) and payload.get("encoded_jit_config")
    print("  POST …/generate-jitconfig    %s  %s" % (code, "拿到 encoded_jit_config" if ok else str(payload)[:160]))
    bad += not ok

    print("\n%s" % ("全部通过" if not bad else "有 %d 项没通过" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
