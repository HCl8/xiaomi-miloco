# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""门禁阶段（gate_batch）与 omni 阶段（realtime_perceive+pregated）的拆分。"""

from __future__ import annotations

import numpy as np
import pytest
from miloco.perception import live_ring
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.config import (
    GateConfig,
    PerceptionConfig,
    SemanticGateConfig,
)
from miloco.perception.types import (
    BatchedSnapshot,
    DeviceSnapshot,
    PerceptionDevice,
    VideoFrame,
    VideoStream,
)


def _frames(value: int, n: int = 4) -> VideoStream:
    return VideoStream(
        frames=[
            VideoFrame(data=np.full((64, 64, 3), value, dtype=np.uint8), timestamp=i * 1000)
            for i in range(n)
        ],
        width=64,
        height=64,
    )


def _batch(value: int = 10) -> BatchedSnapshot:
    dev = PerceptionDevice(
        did="cam_a", name="cam", device_type="camera", room_name="客厅"
    )
    snap = DeviceSnapshot(
        device=dev,
        start_timestamp=0,
        end_timestamp=4000,
        video=_frames(value),
    )
    return BatchedSnapshot(snapshots=[snap], captured_at=4000)


def _engine() -> PerceptionEngine:
    cfg = PerceptionConfig(
        gate=GateConfig(
            hold_duration_sec=0,
            semantic=SemanticGateConfig(enabled=False),
        )
    )
    return PerceptionEngine(cfg)


@pytest.fixture(autouse=True)
def _clear_ring():
    live_ring.clear()
    yield
    live_ring.clear()


async def test_gate_phase_records_window_before_omni():
    eng = _engine()
    prepared = await eng.gate_batch(_batch(), [])
    assert prepared is not None
    gated = prepared.gated
    assert set(gated) == {"cam_a"}
    g = gated["cam_a"]
    assert g.timing is not None
    assert g.snapshot is not None
    # 首窗 cold-start 放行；门禁结果已经在环里（不等 omni）
    assert g.packet is not None
    cams = live_ring.snapshot(5)
    assert len(cams) == 1
    assert len(cams[0]["windows"]) == 1
    assert cams[0]["windows"][0]["gate"]["pass"] is True
    assert cams[0]["windows"][0]["omni"]["pending"] is True


async def test_second_identical_window_is_blocked_in_gate_phase():
    eng = _engine()
    await eng.gate_batch(_batch(10), [])
    prepared = await eng.gate_batch(_batch(10), [])
    assert prepared is not None
    g = prepared.gated["cam_a"]
    assert g.packet is None  # 与上一窗完全相同 → 视觉门禁拦截
    latest = live_ring.snapshot(5)[0]["windows"][0]
    assert latest["blocked"] is True


async def test_omni_phase_uses_pregated_without_rerunning_gate():
    eng = _engine()
    await eng.gate_batch(_batch(10), [])
    prepared = await eng.gate_batch(_batch(10), [])

    call_count = {"n": 0}
    import miloco.perception.engine.pipeline as pipeline_mod

    real_run_gate = pipeline_mod.run_gate

    async def _counting_gate(*args, **kwargs):
        call_count["n"] += 1
        return await real_run_gate(*args, **kwargs)

    pipeline_mod.run_gate = _counting_gate
    try:
        result = await eng.realtime_perceive(_batch(10), [], prepared=prepared)
    finally:
        pipeline_mod.run_gate = real_run_gate

    # 拦截窗在 omni 阶段直接返回，不重跑门禁、不调 omni
    assert call_count["n"] == 0
    assert result is not None
    assert result.skipped is True
