"""macOS launchd 分支：plist 生成 + 平台选路 + launchctl 生命周期。"""

from __future__ import annotations

import plistlib
import subprocess

import pytest

from miloco_cli.commands import service


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=rc, stdout=out, stderr=err
    )


@pytest.fixture(autouse=True)
def _redirect_plist(monkeypatch, tmp_path):
    """把 LaunchAgent plist 重定向到 tmp，避免污染真实 ~/Library/LaunchAgents。"""
    monkeypatch.setattr(
        service, "_launchagent_plist", lambda: tmp_path / "com.xiaomi.miloco.backend.plist"
    )


def test_use_launchd_matches_platform(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service._use_launchd() is True
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service._use_launchd() is False


def test_generate_launchagent_plist_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    cmd = ["/opt/py/bin/python", "-m", "miloco.main"]
    service._generate_launchagent_plist(cmd)

    plist_path = service._launchagent_plist()
    assert plist_path.exists()
    data = plistlib.loads(plist_path.read_bytes())

    assert data["Label"] == "com.xiaomi.miloco.backend"
    # 启动器在前，backend 命令在后 —— python 作为签名启动器的子进程运行
    launcher = str(tmp_path / "miloco.app" / "Contents" / "MacOS" / "miloco")
    assert data["ProgramArguments"] == [launcher, *cmd]
    assert data["RunAtLoad"] is True
    # 对齐 supervisord autorestart=true:非 0 退出码或信号崩溃都重拉,clean exit 不拉
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["WorkingDirectory"] == str(tmp_path)
    assert data["StandardOutPath"] == str(tmp_path / "log" / "miloco-backend.log")

    env = data["EnvironmentVariables"]
    assert env["MILOCO_SUPERVISED"] == "1"
    assert env["MILOCO_HOME"] == str(tmp_path)
    assert env["HOME"]  # 必须显式给（launchd 不保证）
    assert "/opt/homebrew/bin" in env["PATH"]  # 后端 shell 调 ffmpeg 需要
    assert "/.local/bin" in env["PATH"]  # uv 工具所在，不能丢失继承
    # 未配置时区 → 不注入 TZ
    assert "TZ" not in env and "MILOCO_TIMEZONE" not in env


def test_generate_launchagent_plist_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: "Asia/Shanghai")

    service._generate_launchagent_plist(["/opt/py/bin/python", "-m", "miloco.main"])
    env = plistlib.loads(service._launchagent_plist().read_bytes())[
        "EnvironmentVariables"
    ]
    assert env["TZ"] == "Asia/Shanghai"
    assert env["MILOCO_TIMEZONE"] == "Asia/Shanghai"


def test_generate_launchagent_plist_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_resolve_timezone", lambda: None)

    cmd = ["/opt/py/bin/python", "-m", "miloco.main"]
    service._generate_launchagent_plist(cmd)
    plist_path = service._launchagent_plist()
    mtime1 = plist_path.stat().st_mtime_ns
    service._generate_launchagent_plist(cmd)  # 内容不变 → 不重写
    assert plist_path.stat().st_mtime_ns == mtime1


# ─── launchctl 生命周期 ──────────────────────────────────────────────────────


def test_launchd_backend_pid_parses(monkeypatch):
    monkeypatch.setattr(
        service, "_launchctl", lambda *a: _cp(0, "\tstate = running\n\tpid = 4242\n")
    )
    assert service._launchd_backend_pid() == 4242
    # 未加载(print 非零)→ None
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(1))
    assert service._launchd_backend_pid() is None


def test_launchd_is_loaded(monkeypatch):
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(0))
    assert service._launchd_is_loaded() is True
    monkeypatch.setattr(service, "_launchctl", lambda *a: _cp(1))
    assert service._launchd_is_loaded() is False


def test_launchd_reload_retries_on_eio(monkeypatch):
    """bootout→bootstrap 撞 EIO 竞态 → 重试后成功。"""
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    calls = {"bootstrap": 0}

    def fake(*args):
        cmd = args[0]
        if cmd == "print":  # 一直未加载 → wait 循环秒退、事后判定也 False
            return _cp(1)
        if cmd == "bootout":
            return _cp(0)
        if cmd == "bootstrap":
            calls["bootstrap"] += 1
            if calls["bootstrap"] == 1:
                return _cp(5, "", "Bootstrap failed: 5: Input/output error")
            return _cp(0)
        return _cp(0)

    monkeypatch.setattr(service, "_launchctl", fake)
    ok, err = service._launchd_reload()
    assert ok is True and err == ""
    assert calls["bootstrap"] == 2  # 首次 EIO → 重试第二次成功


def test_launchd_reload_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)

    def fake(*args):
        if args[0] == "print":
            return _cp(1)
        if args[0] == "bootout":
            return _cp(0)
        return _cp(5, "", "persistent EIO")  # bootstrap 永远失败

    monkeypatch.setattr(service, "_launchctl", fake)
    ok, err = service._launchd_reload()
    assert ok is False and "persistent EIO" in err


def test_launchd_crashloop_check(monkeypatch):
    """窗口内 backend 换 ≥3 个 pid → 判 crashloop 并 bootout。单次重启(2 pid)容忍。"""
    pids = iter([100, 100, 200, 300, 300])
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: next(pids, None))
    booted = {"n": 0}
    monkeypatch.setattr(
        service,
        "_launchctl",
        lambda *a: (booted.__setitem__("n", booted["n"] + 1), _cp(0))[1],
    )
    check = service._launchd_crashloop_check()
    assert check() is False  # {100}
    assert check() is False  # {100}
    assert check() is False  # {100,200} = 2，容忍单次重启
    assert check() is True  # {100,200,300} = 3 → crashloop
    assert booted["n"] >= 1  # 已 bootout 停掉


def test_reap_legacy_supervisord(monkeypatch, tmp_path):
    """darwin 升级迁移:reap 残留 supervisord + 清老运行时文件。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_find_supervisord_pids", lambda: [111, 222])
    terminated: list[int] = []
    monkeypatch.setattr(service, "_terminate", lambda pid, *a, **k: terminated.append(pid))
    (tmp_path / "supervisord.conf").write_text("x")
    (tmp_path / "supervisord.pid").write_text("1")
    (tmp_path / "supervisor.sock").write_text("")

    reaped = service._reap_legacy_supervisord()
    assert reaped == [111, 222]
    assert terminated == [111, 222]
    assert not (tmp_path / "supervisord.conf").exists()
    assert not (tmp_path / "supervisord.pid").exists()
    assert not (tmp_path / "supervisor.sock").exists()


def test_reap_legacy_supervisord_noop(monkeypatch, tmp_path):
    """无残留时 no-op:不 terminate、不误删文件。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_find_supervisord_pids", lambda: [])
    monkeypatch.setattr(
        service, "_terminate", lambda *a, **k: pytest.fail("不应 terminate")
    )
    (tmp_path / "supervisord.conf").write_text("keep")
    assert service._reap_legacy_supervisord() == []
    assert (tmp_path / "supervisord.conf").exists()  # 未误删


def test_tail_lines(tmp_path):
    f = tmp_path / "log"
    f.write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
    assert service._tail_lines(f, 3) == ["line4", "line5", "line6"]
    assert service._tail_lines(f, 10) == [
        "line1", "line2", "line3", "line4", "line5", "line6"
    ]
    assert service._tail_lines(tmp_path / "nope", 3) == []


def test_extract_log_ts():
    ts = service._extract_log_ts(
        "2026-07-30 12:34:56 - miot.central_hub - ERROR - ..."
    )
    # time.mktime 按本地时区解析;只要 > 0 即可（时区不等同,不判绝对值）
    assert ts > 0
    assert service._extract_log_ts("not a log line") == 0.0


def test_launchd_start_already_running(monkeypatch):
    monkeypatch.setattr(service, "_launchd_backend_pid", lambda: 999)
    captured: dict = {}
    monkeypatch.setattr(
        service, "print_result", lambda payload, pretty: captured.update(payload)
    )
    cfg = {"server": {"url": "http://127.0.0.1:1810"}}
    with pytest.raises(SystemExit):
        service._launchd_start(cfg, pretty=False)
    assert "already running" in captured.get("message", "")
