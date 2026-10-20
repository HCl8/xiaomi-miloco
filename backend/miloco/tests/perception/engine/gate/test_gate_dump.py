"""Tests for the first-stage gate window dump (mp4 + meta retention)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pytest
from miloco.perception.engine.config import GateDumpConfig
from miloco.perception.engine.gate import dump
from miloco.perception.engine.types import GateTiming


@pytest.fixture(autouse=True)
def _reset_cache():
    dump._TOTAL_CACHE.clear()
    dump._LAST_FULL_LOG = 0.0
    yield
    dump._TOTAL_CACHE.clear()
    dump._LAST_FULL_LOG = 0.0


def _timing() -> GateTiming:
    return GateTiming(
        video_ms=6.0,
        audio_ms=0.1,
        video_pass=True,
        audio_pass=False,
        video_score=0.12,
        semantic_applied=True,
        semantic_pass=False,
        semantic_score=0.05,
        semantic_ms=900.0,
    )


def _frames(n: int = 4) -> list[np.ndarray]:
    return [np.full((36, 64, 3), i * 20, dtype=np.uint8) for i in range(n)]


def test_dump_writes_clip_ref_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(dump, "_encode_clip", lambda *a, **k: (b"fake-mp4", None))
    cfg = GateDumpConfig(enabled=True, dir=str(tmp_path))
    start_ms = int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp() * 1000)

    ok = dump.dump_gate_window(
        did="cam/1",
        room_name="客厅",
        start_ms=start_ms,
        end_ms=start_ms + 4000,
        frames=_frames(),
        prev_raw_frame=_frames(1)[0],
        audio_clip=np.zeros(0, dtype=np.int16),
        sample_rate=16000,
        fps=3,
        timing=_timing(),
        config=cfg,
    )

    assert ok is True
    out = tmp_path / "20260102" / "cam_1" / str(start_ms)
    assert (out / "clip.mp4").read_bytes() == b"fake-mp4"
    assert (out / "ref.jpg").is_file()
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["semantic_score"] == pytest.approx(0.05)
    assert meta["video_pass"] is True
    assert meta["clip_bytes"] == len(b"fake-mp4")
    assert meta["did"] == "cam/1"


def test_dump_disabled_is_noop(tmp_path):
    cfg = GateDumpConfig(enabled=False, dir=str(tmp_path))
    ok = dump.dump_gate_window(
        did="cam1",
        room_name="r",
        start_ms=0,
        end_ms=1,
        frames=_frames(),
        prev_raw_frame=None,
        audio_clip=np.zeros(0, dtype=np.int16),
        sample_rate=16000,
        fps=3,
        timing=_timing(),
        config=cfg,
    )
    assert ok is False
    assert not any(tmp_path.iterdir())


def test_dump_cap_stops_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(dump, "_encode_clip", lambda *a, **k: (b"x" * 100, None))
    cfg = GateDumpConfig(enabled=True, dir=str(tmp_path), max_disk_gb=0.0)
    ok = dump.dump_gate_window(
        did="cam1",
        room_name="r",
        start_ms=0,
        end_ms=1,
        frames=_frames(),
        prev_raw_frame=None,
        audio_clip=np.zeros(0, dtype=np.int16),
        sample_rate=16000,
        fps=3,
        timing=_timing(),
        config=cfg,
    )
    assert ok is False


def test_dump_encoder_failure_still_writes_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(dump, "_encode_clip", lambda *a, **k: (None, None))
    cfg = GateDumpConfig(enabled=True, dir=str(tmp_path))
    ok = dump.dump_gate_window(
        did="cam1",
        room_name="r",
        start_ms=1000,
        end_ms=2000,
        frames=_frames(),
        prev_raw_frame=None,
        audio_clip=np.zeros(0, dtype=np.int16),
        sample_rate=16000,
        fps=3,
        timing=_timing(),
        config=cfg,
    )
    assert ok is True
    meta = json.loads((tmp_path / "19700101" / "cam1" / "1000" / "meta.json").read_text())
    assert meta["clip_bytes"] == 0
