# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""最近若干感知窗口的内存环形缓冲，供看板实时看窗。

按**机位**各留最近 N 窗（默认 5），看板两排显示。只放内存、不落盘：
每窗最多 4 帧 JPEG（短边 480），2 机位 5 窗 ≈ 1MB 以内。
"""

from __future__ import annotations

import base64
import threading
from collections import deque

import cv2
import numpy as np

MAX_WINDOWS = 5
MAX_CAMERAS = 8
MAX_FRAMES = 4
SHORT_EDGE = 480
JPEG_QUALITY = 70

_lock = threading.Lock()
_by_did: dict[str, deque[dict]] = {}
_meta: dict[str, dict] = {}


def _encode(frame: np.ndarray) -> str | None:
    """BGR 帧 → base64 jpeg（data URL 内容，不含前缀）。"""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    if not h or not w:
        return None
    scale = SHORT_EDGE / min(h, w)
    if scale < 1:
        frame = cv2.resize(
            frame,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _pick_frames(frames: list[np.ndarray]) -> list[str]:
    if not frames:
        return []
    n = min(MAX_FRAMES, len(frames))
    idx = [round(i * (len(frames) - 1) / max(1, n - 1)) for i in range(n)] if n > 1 else [0]
    out = []
    for i in idx:
        enc = _encode(frames[int(i)])
        if enc:
            out.append(enc)
    return out


def record_window(
    *,
    ts_ms: int,
    did: str,
    room: str,
    device: str,
    blocked: bool,
    gate: dict,
    semantic: dict,
    omni: dict,
    frames: list[np.ndarray],
) -> None:
    """记一个设备窗。帧在调用线程里立刻编码，调用后就不再持有 numpy 数组。"""
    entry = {
        "ts": int(ts_ms),
        "did": did,
        "room": room,
        "device": device,
        "blocked": bool(blocked),
        "gate": gate,
        "semantic": semantic,
        "omni": omni,
        "frames": _pick_frames(frames),
    }
    with _lock:
        if did not in _by_did:
            if len(_by_did) >= MAX_CAMERAS:
                # 只保留最近有数据的机位；超出上限时丢掉最早那台。
                oldest = next(iter(_by_did))
                _by_did.pop(oldest, None)
                _meta.pop(oldest, None)
            _by_did[did] = deque(maxlen=MAX_WINDOWS)
        _by_did[did].append(entry)
        _meta[did] = {"room": room, "device": device}


def update_omni(did: str, ts_ms: int, omni: dict) -> None:
    """门禁记录先落环，omni 跑完再把结论回填到同一条（did, ts）。"""
    with _lock:
        dq = _by_did.get(did)
        if not dq:
            return
        for entry in reversed(dq):
            if entry["ts"] == int(ts_ms):
                entry["omni"] = omni
                return


def snapshot(limit: int = MAX_WINDOWS) -> list[dict]:
    """按机位分组，机位内最新的在前。"""
    n = max(1, min(int(limit or MAX_WINDOWS), MAX_WINDOWS))
    with _lock:
        out = []
        for did, dq in _by_did.items():
            rows = list(dq)
            rows.reverse()
            meta = _meta.get(did) or {}
            out.append(
                {
                    "did": did,
                    "room": meta.get("room", ""),
                    "device": meta.get("device", did),
                    "windows": rows[:n],
                }
            )
    return out


def clear() -> None:
    with _lock:
        _by_did.clear()
        _meta.clear()
