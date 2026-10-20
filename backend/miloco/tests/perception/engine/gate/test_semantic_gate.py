"""Tests for the secondary semantic gate (StartLux sidecar client + gate policy)."""

import httpx
import numpy as np
import pytest
from miloco.perception.engine.config import GateConfig, SemanticGateConfig
from miloco.perception.engine.gate import gate as gate_mod
from miloco.perception.engine.gate import semantic_gate
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.gate.semantic_gate import (
    SemanticEvalResult,
    evaluate_semantic,
    select_frames,
)
from miloco.perception.engine.gate.visual_gate import _preprocess
from miloco.perception.engine.input.video_splitter import create_input_slice


@pytest.fixture(autouse=True)
def _reset_circuit():
    semantic_gate._FAILURE_COUNT.clear()
    semantic_gate._FAILURE_UNTIL.clear()
    yield
    semantic_gate._FAILURE_COUNT.clear()
    semantic_gate._FAILURE_UNTIL.clear()


class _FakeResponse:
    def __init__(self, noul: float):
        self._noul = noul

    def raise_for_status(self):
        return None

    def json(self):
        return {"answers": {"event": {"noul": self._noul}}}


class _FakeClient:
    def __init__(self, noul: float = 0.0, exc: Exception | None = None):
        self._noul = noul
        self._exc = exc
        self.calls = 0

    async def post(self, url, json=None):  # noqa: A002 - matches httpx signature
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._noul)


def _frames(n: int = 12, value: int = 10) -> list[np.ndarray]:
    return [np.full((180, 320, 3), value, dtype=np.uint8) for _ in range(n)]


def _cfg(**kw) -> SemanticGateConfig:
    base = dict(enabled=True, timeout_sec=1.0, min_score=0.5)
    base.update(kw)
    return SemanticGateConfig(**base)


def test_select_frames_spans_window():
    frames = _frames(12)
    picked = select_frames(frames, 4)
    assert len(picked) == 4
    assert picked[0] is frames[0]
    assert picked[-1] is frames[9]


async def test_disabled_does_not_call(monkeypatch):
    client = _FakeClient(noul=0.9)
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(_frames(), _frames(1)[0], _cfg(enabled=False))
    assert result.applied is False
    assert client.calls == 0


async def test_no_reference_frame_skips():
    result = await evaluate_semantic(_frames(), None, _cfg())
    assert result.applied is False


async def test_score_above_threshold_passes(monkeypatch):
    client = _FakeClient(noul=0.62)
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(_frames(), _frames(1)[0], _cfg())
    assert result.applied is True
    assert result.passed is True
    assert result.score == pytest.approx(0.62)


async def test_score_below_threshold_fails(monkeypatch):
    client = _FakeClient(noul=0.1)
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(_frames(), _frames(1)[0], _cfg())
    assert result.applied is True
    assert result.passed is False


async def test_http_failure_is_fail_open(monkeypatch):
    client = _FakeClient(exc=httpx.ConnectError("down"))
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(_frames(), _frames(1)[0], _cfg())
    assert result.applied is False
    assert result.passed is True
    assert result.error == "ConnectError"


async def test_circuit_opens_after_failures(monkeypatch):
    client = _FakeClient(exc=httpx.ConnectError("down"))
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)
    cfg = _cfg(failure_threshold=1, failure_cooldown_sec=60.0)

    first = await evaluate_semantic(_frames(), _frames(1)[0], cfg)
    second = await evaluate_semantic(_frames(), _frames(1)[0], cfg)
    assert first.error == "ConnectError"
    assert second.error == "cooldown"
    assert client.calls == 1


def _solid(value: int) -> np.ndarray:
    return np.full((100, 100, 3), value, dtype=np.uint8)


def _baseline() -> np.ndarray:
    return _preprocess(_solid(100))


def _slice_with_change():
    gray, white = _solid(100), _solid(255)
    return create_input_slice(
        "room",
        [gray, gray, white, white, white, white],
        np.zeros(16000, dtype=np.int16),
    )


async def test_shadow_records_score_without_veto(monkeypatch):
    async def _deny(*a, **kw):
        return SemanticEvalResult(
            applied=True, passed=False, score=0.01, latency_ms=900
        )

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _deny)
    cfg = GateConfig(semantic=SemanticGateConfig(enabled=True, mode="shadow"))

    packet, timing, *_ = await run_gate(
        _slice_with_change(), cfg, prev_raw_frame=_solid(100)
    )
    assert packet is not None
    assert timing.semantic_applied is True
    assert timing.semantic_pass is False
    assert timing.semantic_veto is False
    assert timing.video_effective_pass is True


async def test_enforce_veto_drops_window(monkeypatch):
    async def _deny(*a, **kw):
        return SemanticEvalResult(
            applied=True, passed=False, score=0.01, latency_ms=900
        )

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _deny)
    cfg = GateConfig(semantic=SemanticGateConfig(enabled=True, mode="enforce"))

    packet, timing, *_ = await run_gate(
        _slice_with_change(),
        cfg,
        prev_frame=_baseline(),
        prev_raw_frame=_solid(100),
    )
    assert packet is None
    assert timing.video_pass is True
    assert timing.video_effective_pass is False
    assert timing.semantic_veto is True


async def test_enforce_fail_open_keeps_window(monkeypatch):
    async def _fail(*a, **kw):
        return SemanticEvalResult(applied=False, passed=True, error="ConnectError")

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _fail)
    cfg = GateConfig(semantic=SemanticGateConfig(enabled=True, mode="enforce"))

    packet, timing, *_ = await run_gate(
        _slice_with_change(),
        cfg,
        prev_frame=_baseline(),
        prev_raw_frame=_solid(100),
    )
    assert packet is not None
    assert timing.semantic_applied is False
    assert timing.semantic_veto is False
