# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the in-memory live window ring (dashboard live view)."""

from __future__ import annotations

import base64

import numpy as np
import pytest
from miloco.perception import live_ring


@pytest.fixture(autouse=True)
def _clear():
    live_ring.clear()
    yield
    live_ring.clear()


def _frames(n: int = 4) -> list[np.ndarray]:
    return [np.full((90, 160, 3), 40 + i, dtype=np.uint8) for i in range(n)]


def _record(i: int, did: str = "cam_a", frames=None):
    live_ring.record_window(
        ts_ms=1000 + i,
        did=did,
        room="客厅" if did == "cam_a" else "玄关",
        device="阿三" if did == "cam_a" else "小C700",
        blocked=False,
        gate={"pass": True, "score": 0.02, "hold": False, "recheck": False},
        semantic={"applied": True, "pass": False, "veto": True, "score": 0.1},
        omni={"called": False, "ms": 0.0, "caption": "", "rules": []},
        frames=_frames() if frames is None else frames,
    )


def test_each_camera_keeps_five_newest_first():
    for i in range(7):
        _record(i, "cam_a")
    for i in range(3):
        _record(100 + i, "cam_b")

    cams = {c["did"]: c for c in live_ring.snapshot(5)}
    assert set(cams) == {"cam_a", "cam_b"}
    assert [w["ts"] for w in cams["cam_a"]["windows"]] == [1006, 1005, 1004, 1003, 1002]
    assert [w["ts"] for w in cams["cam_b"]["windows"]] == [1102, 1101, 1100]
    assert cams["cam_a"]["room"] == "客厅"
    assert cams["cam_a"]["device"] == "阿三"


def test_limit_is_clamped_per_camera():
    for i in range(3):
        _record(i, "cam_a")
    cams = live_ring.snapshot(1)
    assert len(cams) == 1
    assert len(cams[0]["windows"]) == 1


def test_frames_are_jpeg_base64_and_bounded():
    _record(0, frames=_frames(10))
    row = live_ring.snapshot(1)[0]["windows"][0]
    assert 0 < len(row["frames"]) <= live_ring.MAX_FRAMES
    for enc in row["frames"]:
        raw = base64.b64decode(enc)
        assert raw[:2] == b"\xff\xd8"  # JPEG SOI


def test_empty_frames_ok():
    _record(0, frames=[])
    row = live_ring.snapshot(1)[0]["windows"][0]
    assert row["frames"] == []
