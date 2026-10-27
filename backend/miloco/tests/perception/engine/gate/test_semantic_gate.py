"""Tests for the secondary semantic gate (StartLux sidecar client + gate policy)."""

import time

import httpx
import numpy as np
import pytest
from miloco.perception.engine.config import GateConfig, SemanticGateConfig
from miloco.perception.engine.gate import gate as gate_mod
from miloco.perception.engine.gate import semantic_gate
from miloco.perception.engine.gate.gate import run_gate
from miloco.perception.engine.gate.semantic_gate import (
    SemanticEvalResult,
    build_payload,
    evaluate_semantic,
    select_frames,
)
from miloco.perception.engine.gate.visual_gate import _preprocess
from miloco.perception.engine.input.video_splitter import create_input_slice
from miloco.perception.engine.types import SemanticGateContext, SpecialPassItem


@pytest.fixture(autouse=True)
def _reset_circuit():
    semantic_gate._FAILURE_COUNT.clear()
    semantic_gate._FAILURE_UNTIL.clear()
    yield
    semantic_gate._FAILURE_COUNT.clear()
    semantic_gate._FAILURE_UNTIL.clear()


class _FakeResponse:
    def __init__(self, noul: float, answers: dict | None = None):
        self._answers = answers or {"event": {"noul": noul}}

    def raise_for_status(self):
        return None

    def json(self):
        return {"answers": self._answers}


class _FakeClient:
    def __init__(
        self,
        noul: float = 0.0,
        exc: Exception | None = None,
        answers: dict | None = None,
    ):
        self._noul = noul
        self._exc = exc
        self._answers = answers
        self.calls = 0

    async def post(self, url, json=None):  # noqa: A002 - matches httpx signature
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._noul, self._answers)


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


async def test_end_question_recovers_event_end(monkeypatch):
    client = _FakeClient(answers={"event": {"noul": 0.05}, "event_end": {"noul": 0.6}})
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(end_min_score=0.5)
    )
    assert result.applied is True
    assert result.passed is True  # 默认问题没过，但「消失/结束」补问过了
    assert result.end_score == pytest.approx(0.6)
    assert result.end_passed is True
    assert result.question_count == 3


async def test_physio_question_recovers_small_action(monkeypatch):
    client = _FakeClient(
        answers={"event": {"noul": 0.05}, "event_physio": {"noul": 0.6}}
    )
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(physio_min_score=0.5)
    )
    assert result.applied is True
    assert result.passed is True  # 默认问题没过，但生理关怀补问过了
    assert result.physio_score == pytest.approx(0.6)
    assert result.physio_passed is True
    assert result.question_count == 3


async def test_end_question_disabled(monkeypatch):
    client = _FakeClient(answers={"event": {"noul": 0.05}, "event_end": {"noul": 0.9}})
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(end_enabled=False)
    )
    assert result.passed is False
    assert result.end_passed is False
    assert result.question_count == 2


# =============================================================================
# 特殊放行问题（规则/档案经 LLM 提炼）
# =============================================================================


def _ctx(*actions: str, room: str = "客厅") -> SemanticGateContext:
    return SemanticGateContext(
        special_items=tuple(SpecialPassItem(action=a) for a in actions),
        room_name=room,
        current_time="10:00:00",
    )


def _slice_no_change():
    frame = _solid(100)
    return create_input_slice("room", [frame] * 6, np.zeros(16000, dtype=np.int16))


async def test_hold_semantic_pass_opens_window(monkeypatch):
    async def _allow(*a, **kw):
        return SemanticEvalResult(applied=True, passed=True, score=0.9)

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _allow)
    cfg = GateConfig(
        hold_duration_sec=360.0,
        semantic=SemanticGateConfig(
            enabled=True, mode="shadow", hold_semantic_gate=True
        ),
    )
    packet, timing, *_ = await run_gate(
        _slice_no_change(),
        cfg,
        prev_frame=_baseline(),
        last_visual_pass_ts=time.monotonic(),
        prev_raw_frame=_solid(100),
    )
    assert packet is not None
    assert packet.trigger.hold is True
    assert timing.hold_pass is True
    assert timing.hold_opened_window is True
    assert timing.semantic_applied is True
    assert timing.semantic_pass is True
    assert timing.hold_semantic_veto is False


async def test_hold_semantic_veto_drops_window(monkeypatch):
    async def _deny(*a, **kw):
        return SemanticEvalResult(applied=True, passed=False, score=0.01)

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _deny)
    cfg = GateConfig(
        hold_duration_sec=360.0,
        semantic=SemanticGateConfig(
            enabled=True, mode="shadow", hold_semantic_gate=True
        ),
    )
    packet, timing, *_ = await run_gate(
        _slice_no_change(),
        cfg,
        prev_frame=_baseline(),
        last_visual_pass_ts=time.monotonic(),
        prev_raw_frame=_solid(100),
    )
    assert packet is None
    assert timing.hold_pass is True
    assert timing.hold_opened_window is False
    assert timing.semantic_applied is True
    assert timing.semantic_pass is False
    assert timing.hold_semantic_veto is True


async def test_hold_semantic_gate_disabled_keeps_old_behavior(monkeypatch):
    async def _deny(*a, **kw):
        return SemanticEvalResult(applied=True, passed=False, score=0.01)

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _deny)
    cfg = GateConfig(
        hold_duration_sec=360.0,
        semantic=SemanticGateConfig(
            enabled=True, mode="shadow", hold_semantic_gate=False
        ),
    )
    packet, timing, *_ = await run_gate(
        _slice_no_change(),
        cfg,
        prev_frame=_baseline(),
        last_visual_pass_ts=time.monotonic(),
        prev_raw_frame=_solid(100),
    )
    assert packet is not None
    assert timing.hold_opened_window is True
    assert timing.hold_semantic_veto is False


def test_payload_adds_special_questions_and_context():
    payload = build_payload(
        _frames(1)[0], _frames(4), _cfg(), _ctx("老人捂胸口", "宝宝在爬行垫上")
    )
    assert set(payload["questions"]) == {
        "event", "event_end", "event_physio", "pass:0", "pass:1"
    }
    assert "老人捂胸口" in payload["questions"]["pass:0"]["instructions"]
    assert "参考帧" in payload["questions"]["pass:0"]["instructions"]
    # 特殊放行问的是「相对参考帧的变化」，不是静态状态
    assert "是否出现了以下变化" in payload["questions"]["pass:0"]["instructions"]
    assert "不要求跟参考帧相比有变化" not in payload["questions"]["pass:0"]["instructions"]
    assert "客厅" in payload["state"]
    assert "10:00:00" in payload["state"]


def test_payload_without_context_has_default_and_end_questions():
    payload = build_payload(_frames(1)[0], _frames(4), _cfg())
    assert set(payload["questions"]) == {"event", "event_end", "event_physio"}


async def test_special_item_pass_keeps_window(monkeypatch):
    client = _FakeClient(answers={"event": {"noul": 0.05}, "pass:0": {"noul": 0.8}})
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(extra_min_score=0.5), _ctx("老人捂胸口")
    )
    assert result.applied is True
    assert result.passed is True  # 默认问题 0.05 < 0.5，但特殊放行项过阈值
    assert result.score == pytest.approx(0.05)
    assert result.pass_score == pytest.approx(0.8)
    assert result.pass_hits == (0,)
    assert result.question_count == 4


async def test_special_item_below_threshold_still_vetoes(monkeypatch):
    client = _FakeClient(answers={"event": {"noul": 0.05}, "pass:0": {"noul": 0.1}})
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(extra_min_score=0.5), _ctx("老人捂胸口")
    )
    assert result.applied is True
    assert result.passed is False
    assert result.pass_hits == ()


async def test_missing_special_answer_is_ignored(monkeypatch):
    client = _FakeClient(answers={"event": {"noul": 0.9}})
    monkeypatch.setattr(semantic_gate, "_client", lambda *a: client)

    result = await evaluate_semantic(
        _frames(), _frames(1)[0], _cfg(), _ctx("老人捂胸口")
    )
    assert result.passed is True
    assert result.pass_hits == ()
    assert result.question_count == 4


async def test_force_pass_recheck_bypasses_visual_and_semantic(monkeypatch):
    """状态容器到点复核：无变化窗也直接放行，且不跑语义门禁。"""

    async def _deny(*a, **kw):
        return SemanticEvalResult(applied=True, passed=False, score=0.01)

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _deny)
    cfg = GateConfig(semantic=SemanticGateConfig(enabled=True, mode="enforce"))

    packet, timing, *_ = await run_gate(
        _slice_no_change(),
        cfg,
        prev_frame=_baseline(),
        prev_raw_frame=_solid(100),
        force_pass=True,
    )
    assert packet is not None
    assert timing.recheck is True
    assert timing.video_pass is False          # 原始视觉判定没变
    assert timing.video_effective_pass is True  # 复核强制放行
    assert timing.semantic_applied is False     # 复核窗不跑语义门禁
    assert packet.trigger.recheck is True


async def test_enforce_special_pass_keeps_window(monkeypatch):
    async def _pass(*a, **kw):
        return SemanticEvalResult(
            applied=True,
            passed=True,
            score=0.01,
            pass_score=0.9,
            pass_hits=(0,),
            pass_count=1,
            question_count=2,
        )

    monkeypatch.setattr(gate_mod, "evaluate_semantic", _pass)
    cfg = GateConfig(semantic=SemanticGateConfig(enabled=True, mode="enforce"))
    ctx = SemanticGateContext(
        special_items=(SpecialPassItem(action="老人捂胸口"),), room_name="客厅"
    )

    packet, timing, *_ = await run_gate(
        _slice_with_change(),
        cfg,
        prev_frame=_baseline(),
        prev_raw_frame=_solid(100),
        semantic_context=ctx,
    )
    assert packet is not None
    assert timing.semantic_pass is True
    assert timing.semantic_veto is False
    assert timing.semantic_pass_score == pytest.approx(0.9)
    assert timing.semantic_pass_count == 1
    assert timing.semantic_question_count == 2
    assert "老人捂胸口" in timing.semantic_pass_hits
