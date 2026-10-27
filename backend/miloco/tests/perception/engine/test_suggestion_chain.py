"""事件链 assign_id_and_update_link 的链接 / 文案 / 紧急度维护逻辑测试。

去重链接已从"模型填 prev_id"改为"代码按 event 句向量语义匹配"（停注入历史后模型
不再填 prev_id）：本轮 event 与已有链 event 余弦 ≥ SUGG_SIM_THRESHOLD 即判为同一桩
持续事件。为不依赖真实 bge 模型、保持确定性，这里用 FakeEmbedder 把 event 按预设分组
映射到正交单位向量——同组 cos=1（必链），异组 cos=0（必不链）。

重点仍覆盖"心跳不漂移、升级才更新"：模型在链接轮自作主张写新文本时（同一持续事件的
措辞漂移），链条规范描述不应被刷掉（曾出现"揉眼睛"被刷成"操作电脑"的 drift bug）。
"""
from __future__ import annotations

import numpy as np
from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.state_memory import StateContainer
from miloco.perception.engine.types import (
    BatchPipelineResult,
    DevicePipelineResult,
    RoomPipelineResult,
)
from miloco.perception.types import Suggestion


class _FakeEmbedder:
    """把 event 文本按预设分组映射到正交单位向量。

    同组 → 同向量（cos=1，≥阈值，必链）；异组 / 未登记 → 互相正交（cos=0，必不链）。
    """

    def __init__(self, groups: dict[str, str] | None = None):
        self._group = groups or {}
        self._basis: dict[str, np.ndarray] = {}

    def embed(self, text: str) -> np.ndarray:
        g = self._group.get(text, text)  # 未登记的 event 自成一组
        if g not in self._basis:
            v = np.zeros(32, dtype=np.float32)
            v[len(self._basis)] = 1.0
            self._basis[g] = v
        return self._basis[g]


def _engine(embedder=None) -> PerceptionEngine:
    """绕过重 __init__，只准备 assign_id_and_update_link 依赖的字段。

    默认不带 embedder（走精确文本匹配兜底）；传 FakeEmbedder 走语义匹配。
    """
    eng = object.__new__(PerceptionEngine)
    eng._sugg_table = {}
    eng._next_sugg_id = {}
    eng._embedder = embedder
    eng._current_frames = {}
    eng._current_rooms = {}
    eng._current_device_names = {}
    return eng


def _sugg(event, action="act", urgency="low") -> Suggestion:
    return Suggestion(event=event, action=action, urgency=urgency)


# 一组"同一持续事件的措辞漂移"——喂给 FakeEmbedder 视为同组（语义相同）
_DRIFT_GROUP = {
    "揉眼睛": "tired",
    "Ada在工位前操作电脑": "tired",
    "靠近刀具": "knife",
    "挥舞刀具": "knife",
}


def test_new_chain_reports_and_stores():
    eng = _engine(_FakeEmbedder())
    s = _sugg("揉眼睛", urgency="low")
    linked = eng.assign_id_and_update_link("room", s, now=100.0)
    assert linked is False  # 新链 → 上报
    assert s.id == 1
    assert eng._sugg_table["room"][1]["event"] == "揉眼睛"


def test_heartbeat_same_event_suppressed():
    """同一事件再次出现（语义命中已有链）→ 心跳，抑制上报。"""
    eng = _engine(_FakeEmbedder())
    eng.assign_id_and_update_link("room", _sugg("揉眼睛", urgency="low"), now=100.0)
    s = _sugg("揉眼睛", urgency="low")
    linked = eng.assign_id_and_update_link("room", s, now=110.0)
    assert linked is True  # 心跳 → 抑制上报
    assert s.id == 1
    assert len(eng._sugg_table["room"]) == 1  # 没开新链


def test_heartbeat_drifted_text_does_not_overwrite():
    """drift bug 回归：同紧急度、语义同链但模型写了漂移文本 → 链条描述保持不变。"""
    eng = _engine(_FakeEmbedder(_DRIFT_GROUP))
    eng.assign_id_and_update_link("room", _sugg("揉眼睛", urgency="low"), now=100.0)
    # 同一持续事件、措辞漂移（FakeEmbedder 判为同组 → 语义命中链）
    s = _sugg("Ada在工位前操作电脑", urgency="low")
    linked = eng.assign_id_and_update_link("room", s, now=110.0)
    assert linked is True
    assert eng._sugg_table["room"][1]["event"] == "揉眼睛"  # 未被刷掉
    assert s.event == "揉眼睛"  # 下游也拿到规范描述


def test_escalation_within_cooldown_updates_text_but_suppressed():
    """事态升级但仍在冷却内：刷新描述 + urgency（记录峰值），但不复报（没过 high 冷却）。

    新语义：升级本身不再强制复报；复报只看「距上次上报是否过了当前 urgency 的冷却」。
    """
    eng = _engine(_FakeEmbedder(_DRIFT_GROUP))
    eng.assign_id_and_update_link("room", _sugg("靠近刀具", urgency="low"), now=100.0)
    s = _sugg("挥舞刀具", urgency="high")  # 同组 → 语义命中链；升级到 high
    linked = eng.assign_id_and_update_link("room", s, now=110.0)  # 距首报仅 10s < 60s
    assert linked is True  # 没过 high 冷却(60s) → 抑制
    entry = eng._sugg_table["room"][1]
    assert entry["event"] == "挥舞刀具"      # 升级时更新描述
    assert entry["urgency"] == "high"


def test_heartbeat_after_old_cooldown_still_suppressed():
    """3.2 上报策略：心跳不再按 urgency cooldown 复报，改为状态定时器补提醒。"""
    eng = _engine(_FakeEmbedder(_DRIFT_GROUP))
    eng.assign_id_and_update_link("room", _sugg("靠近刀具", urgency="low"), now=100.0)  # 首报 low
    # 升级到 high，但距首报仅 10s < 60s → 抑制
    assert eng.assign_id_and_update_link("room", _sugg("挥舞刀具", urgency="high"), now=110.0) is True
    # 即使超过旧的 high 冷却，也仍是心跳 → 抑制；提醒由 state_memory 到点发
    s = _sugg("挥舞刀具", urgency="high")
    assert eng.assign_id_and_update_link("room", s, now=161.0) is True
    assert s.urgency == "high"


def test_persistent_event_heartbeat_never_rereports():
    """3.2：持续事件只在状态进入时报一次；之后心跳一律抑制，不再按 cooldown 复报。"""
    eng = _engine(_FakeEmbedder())
    assert eng.assign_id_and_update_link("room", _sugg("冰箱门敞开", urgency="low"), now=0.0) is False  # 首报
    # 之后所有心跳都只维持状态，不再复报
    assert eng.assign_id_and_update_link("room", _sugg("冰箱门敞开", urgency="low"), now=100.0) is True
    assert eng.assign_id_and_update_link("room", _sugg("冰箱门敞开", urgency="low"), now=299.0) is True
    assert eng.assign_id_and_update_link("room", _sugg("冰箱门敞开", urgency="low"), now=300.0) is True
    assert eng.assign_id_and_update_link("room", _sugg("冰箱门敞开", urgency="low"), now=400.0) is True


def test_state_timer_reminder_only():
    """3.2 上报：状态进入报一次；到点发提醒；不做帧差/不清状态。"""
    eng = _engine(_FakeEmbedder())
    s = _sugg("冰箱门敞开", action="关门", urgency="low")
    assert eng.assign_id_and_update_link("room", s, now=0.0) is False
    entry = eng._sugg_table["room"][1]
    assert entry["next_check_ts"] == 300.0
    assert entry["state_suggestion"].event == "冰箱门敞开"

    assert eng._collect_state_reminders(299.0) == []
    reminders = eng._collect_state_reminders(300.0)
    assert len(reminders) == 1
    assert reminders[0].id == 1
    assert reminders[0].event == "冰箱门敞开"
    assert entry["next_check_ts"] == 600.0
    assert eng._collect_state_reminders(599.0) == []
    assert len(eng._collect_state_reminders(600.0)) == 1
    # 状态链仍在（不再有帧差清除逻辑）
    assert 1 in eng._sugg_table["room"]
def test_downgrade_ignored_keeps_peak_urgency():
    """urgency 单调不降级：链接轮报更低 urgency → 忽略，沿用历史峰值，仍当心跳抑制。"""
    eng = _engine(_FakeEmbedder(_DRIFT_GROUP))
    eng.assign_id_and_update_link("room", _sugg("挥舞刀具", urgency="high"), now=100.0)
    s = _sugg("靠近刀具", urgency="low")  # 同组 → 命中链；urgency 更低
    linked = eng.assign_id_and_update_link("room", s, now=110.0)
    assert linked is True                        # 降级仍是心跳 → 抑制
    entry = eng._sugg_table["room"][1]
    assert entry["urgency"] == "high"            # 未被降到 low（单调不降级）
    assert s.urgency == "high"                   # 对外 urgency 对齐链条值


def test_dissimilar_event_opens_new_chain():
    """语义不同的事件 → 开新链并上报。"""
    eng = _engine(_FakeEmbedder())
    eng.assign_id_and_update_link("room", _sugg("男子在工位处喝水", urgency="low"), now=100.0)
    s = _sugg("玻璃破碎", urgency="high")  # 异组 → 不链
    linked = eng.assign_id_and_update_link("room", s, now=103.0)
    assert linked is False             # 不同事件 → 开新链
    assert s.id == 2
    assert len(eng._sugg_table["room"]) == 2


def test_empty_event_is_dropped():
    """event 为空 → 丢弃，不开空链、不上报（停注入历史后模型基本不会再产空 event，留作兜底）。"""
    eng = _engine(_FakeEmbedder())
    s = _sugg("", action="", urgency="low")
    linked = eng.assign_id_and_update_link("room", s, now=100.0)
    assert linked is True                        # 抑制上报
    assert eng._sugg_table.get("room", {}) == {}  # 没有开新链


def test_fallback_exact_match_when_no_embedder():
    """embedder 不可用时降级为精确文本匹配：同文本归并、不同文本开新链。"""
    eng = _engine(embedder=None)
    eng.assign_id_and_update_link("room", _sugg("男子在工位处喝水", urgency="low"), now=100.0)
    # 完全相同文本 → 精确匹配命中
    same = _sugg("男子在工位处喝水", urgency="low")
    assert eng.assign_id_and_update_link("room", same, now=103.0) is True
    assert same.id == 1
    assert len(eng._sugg_table["room"]) == 1
    # 不同文本 → 开新链
    other = _sugg("男子在工位处打哈欠", urgency="low")
    assert eng.assign_id_and_update_link("room", other, now=106.0) is False
    assert other.id == 2
    assert len(eng._sugg_table["room"]) == 2


def test_touch_active_chain_keeps_it_past_reminder_deadline():
    """门禁拦截窗：状态容器续命后，链不会在提醒到点那一刻被 TTL 淘汰。"""
    eng = _engine(_FakeEmbedder())
    eng._state_container = StateContainer(recheck_interval_sec=300.0)
    assert (
        eng.assign_id_and_update_link(
            "room", _sugg("冰箱门敞开", urgency="low"), now=0.0
        )
        is False
    )
    eng._state_container.note_omni("room", ["r1"], ["r1"], now=0.0)

    # 对照组：不续命时，300s 恰好触发 low 的淘汰阈值
    untouch = _engine(_FakeEmbedder())
    untouch.assign_id_and_update_link(
        "room", _sugg("冰箱门敞开", urgency="low"), now=0.0
    )
    untouch._evict_expired_links("room", now=300.0)
    assert untouch._sugg_table.get("room", {}) == {}

    # 续命后 last_ts 被刷新 → 链保留，提醒定时器仍能到点
    eng._touch_active_chains("room", now=290.0)
    assert eng._sugg_table["room"][1]["last_ts"] == 290.0
    eng._evict_expired_links("room", now=300.0)
    assert 1 in eng._sugg_table["room"]
    reminders = eng._collect_state_reminders(300.0)
    assert len(reminders) == 1


def test_touch_skips_chain_without_recent_heartbeat():
    """被新事件替换掉的旧链不会被无限续命。"""
    eng = _engine(_FakeEmbedder())
    eng._state_container = StateContainer(recheck_interval_sec=300.0)
    eng.assign_id_and_update_link("room", _sugg("旧事件", urgency="low"), now=0.0)
    eng.assign_id_and_update_link("room", _sugg("新事件", urgency="low"), now=0.0)
    eng._state_container.note_omni("room", [], [], now=0.0, has_signal=True)

    # 旧链的最近心跳是 0；在 2×复核间隔之后不再 touch
    eng._touch_active_chains("room", now=601.0)
    assert eng._sugg_table["room"][1]["last_ts"] == 0.0


def test_merge_results_gate_blocked_touches_chain_without_feeding_rules():
    """整间房被门禁拦住时：收集 blocked did、刷新链 TTL；不产生规则续命样本。"""
    eng = _engine(_FakeEmbedder())
    eng._state_container = StateContainer(recheck_interval_sec=300.0)
    assert (
        eng.assign_id_and_update_link(
            "cam_A", _sugg("人在工位工作", urgency="low"), now=0.0
        )
        is False
    )
    eng._state_container.note_omni("cam_A", ["rule_work"], ["rule_work"], now=0.0)

    result = BatchPipelineResult(
        rooms={
            "客厅": RoomPipelineResult(
                room_name="客厅",
                skipped=True,
                device_results={
                    "cam_A": DevicePipelineResult(
                        device_id="cam_A", skipped=True, gate_blocked=True
                    )
                },
            )
        }
    )
    merged = eng._merge_results(
        result, device_rule_map={"cam_A": ["rule_work"]}, now=290.0
    )

    assert merged.skipped is True
    assert merged.gate_blocked_dids == ["cam_A"]
    assert eng._sugg_table["cam_A"][1]["last_ts"] == 290.0
