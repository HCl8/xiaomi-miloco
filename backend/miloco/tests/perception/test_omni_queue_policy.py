# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""omni 队列的取舍：门禁全拦的窗不能把真变化窗挤掉。

拆分后门禁全拦的窗也会入队（只为落 trace），队列满时按「最旧」丢的话，一窗
真变化会被它挤掉 —— 不调 omni、不落 traces、不推规则边沿。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from miloco.perception.processor import PipelineProcessor


def _item(trace: str, *, packet, did="cam1", recheck=()):
    gated = {did: SimpleNamespace(packet=packet)}
    prepared = SimpleNamespace(gated=gated, recheck_dids=list(recheck))
    return SimpleNamespace(
        trace_id=trace,
        prepared=prepared,
        batch=SimpleNamespace(devices={did: None}),
    )


def _processor(maxsize=2):
    p = object.__new__(PipelineProcessor)
    p._omni_queue = asyncio.Queue(maxsize=maxsize)
    return p


def test_needs_omni_tracks_gate_packet_and_forced_recheck():
    assert PipelineProcessor._needs_omni(_item("a", packet=object())) is True
    assert PipelineProcessor._needs_omni(_item("b", packet=None)) is False
    # 门禁全拦但有强制复核：仍要跑 omni
    assert PipelineProcessor._needs_omni(_item("c", packet=None, recheck=("cam1",))) is True


def test_full_queue_drops_the_blocked_window_not_the_real_one():
    p = _processor(maxsize=2)
    blocked = _item("blocked", packet=None)
    real = _item("real", packet=object())
    p._enqueue_omni(blocked)
    p._enqueue_omni(real)

    # 再来一窗：队列满，应该丢 blocked 而不是 real
    p._enqueue_omni(_item("blocked2", packet=None))

    kept = []
    while not p._omni_queue.empty():
        kept.append(p._omni_queue.get_nowait().trace_id)
    assert "real" in kept, f"真变化窗被挤掉了: {kept}"


def test_full_queue_with_only_real_windows_drops_oldest():
    p = _processor(maxsize=2)
    p._enqueue_omni(_item("r1", packet=object()))
    p._enqueue_omni(_item("r2", packet=object()))
    p._enqueue_omni(_item("r3", packet=object()))

    kept = []
    while not p._omni_queue.empty():
        kept.append(p._omni_queue.get_nowait().trace_id)
    assert kept == ["r2", "r3"], kept
