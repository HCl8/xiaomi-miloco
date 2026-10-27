"""门禁之外的状态容器：记住「当前状态还在」，用定时器强制复核。

这里**不做**任何画面基线/帧差判断——「状态是否变了」由本地 VLM 门禁和 omni 回答。
本模块只负责：

- 新事件链进入时记一个提醒定时器；
- 心跳不再按 cooldown 复报；
- 到点发一次提醒（沿用链 id / event / action / urgency），然后重置；
- 记住每个相机当前被 omni 确认过的规则/事件状态；状态还在就每隔
  ``recheck_interval_sec`` 强制放一窗给 omni 复核，并刷新建议链 TTL。

容器**不**给规则/任务喂时间样本：任务只靠状态边沿 + 定时器/record 推进。
没有容器时，门禁拦截的静态窗没有 omni 心跳，建议链会在提醒到点那一刻被 TTL
淘汰；长状态也没有复核机会。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from miloco.perception.types import Suggestion

# 提醒间隔沿用旧复报节奏：high 60s / medium 120s / low 300s。
URGENCY_INTERVAL_SEC: dict[str, float] = {
    "high": 60.0,
    "medium": 120.0,
    "low": 300.0,
}


@dataclass
class StateTick:
    reminder: Suggestion | None = None


def interval_for(urgency: str) -> float:
    return URGENCY_INTERVAL_SEC.get(urgency, URGENCY_INTERVAL_SEC["medium"])


def remember_state(entry: dict, suggestion: Suggestion, now: float) -> None:
    """新事件链登记提醒定时器；不记录基线、不做帧差。"""
    entry["next_check_ts"] = now + interval_for(suggestion.urgency)
    entry["state_suggestion"] = suggestion.model_copy(deep=True)


def tick_state(
    entry: dict,
    now: float,
    *,
    eid: int,
    did: str = "",
    room: str = "",
    device_name: str = "",
) -> StateTick:
    """到点生成一条提醒；状态是否解除不在此判断。"""
    if not entry.get("state_suggestion"):
        return StateTick()
    if now < float(entry.get("next_check_ts") or 0.0):
        return StateTick()

    reminder = entry["state_suggestion"].model_copy(deep=True)
    reminder.id = eid
    reminder.event = str(entry.get("event") or reminder.event)
    reminder.action = str(entry.get("action") or reminder.action)
    reminder.urgency = str(entry.get("urgency") or reminder.urgency)
    if not reminder.source_device_ids and did:
        reminder.source_device_ids = [did]
    if not reminder.room_name and room:
        reminder.room_name = room
    if not reminder.device_name and device_name:
        reminder.device_name = device_name

    entry["next_check_ts"] = now + interval_for(reminder.urgency)
    entry["last_report_ts"] = now  # 仅观测用
    return StateTick(reminder=reminder)


# =============================================================================
# 状态容器：门禁拦截时维持「当前状态还在」
# =============================================================================


@dataclass
class CameraState:
    """单相机当前状态的证据。

    ``rules`` 保存「最后一次被 omni 确认满足」的规则及其确认时刻；``signal``
    表示最近一次 omni 还产出了 suggestion（规则之外的持续事件）。容器只负责
    「状态还在 → 到点强制复核」和上报，不给规则/任务喂时间样本。
    """

    last_check_ts: float = 0.0
    last_omni_ts: float = 0.0
    rules: dict[str, float] = field(default_factory=dict)
    signal: bool = False
    misses: int = 0

    def active(self) -> bool:
        return bool(self.rules) or self.signal


class StateContainer:
    """按相机记住当前状态；门禁拦截窗的续命与强制复核都由它决定。"""

    def __init__(
        self,
        recheck_interval_sec: float = 300.0,
        max_misses: int = 2,
        enabled: bool = True,
    ) -> None:
        self.recheck_interval_sec = max(1.0, float(recheck_interval_sec))
        self.max_misses = max(1, int(max_misses))
        self.enabled = enabled
        self._states: dict[str, CameraState] = {}

    # ── omni 结果回填 ────────────────────────────────────────────

    def note_omni(
        self,
        did: str,
        dispatched: list[str] | set[str],
        matched: list[str] | set[str],
        now: float,
        *,
        has_signal: bool = False,
    ) -> None:
        """omni 跑完后回填本相机状态。

        ``dispatched`` 是本窗真正下发给 omni 的规则；没命中的规则立即移除。
        连续 ``max_misses`` 次复核都没有任何命中/信号，容器整体清空。
        这里不产生任何「喂给规则/任务」的时间样本。
        """
        if not self.enabled:
            return
        st = self._states.setdefault(did, CameraState())
        dispatched_set = set(dispatched)
        matched_set = set(matched)

        for rid in list(st.rules):
            if rid in dispatched_set and rid not in matched_set:
                st.rules.pop(rid, None)
        for rid in matched_set:
            st.rules[rid] = now

        st.last_omni_ts = now
        st.last_check_ts = now
        if matched_set or has_signal:
            st.signal = True
            st.misses = 0
            return

        st.misses += 1
        if st.misses >= self.max_misses:
            st.rules.clear()
            st.signal = False
            st.misses = 0

    def is_active(self, did: str) -> bool:
        if not self.enabled:
            return False
        st = self._states.get(did)
        return st is not None and st.active()

    # ── 定时强制复核 ────────────────────────────────────────────

    def recheck_due(self, now: float) -> list[str]:
        """到点的相机：状态还在、且距上次 omni/复核已超过复核间隔。"""
        if not self.enabled:
            return []
        return [
            did
            for did, st in self._states.items()
            if st.active() and now - st.last_check_ts >= self.recheck_interval_sec
        ]

    def mark_recheck_issued(self, did: str, now: float) -> None:
        """复核窗已放行（即使之后 omni 失败也顺延一个间隔，避免每窗强放）。"""
        st = self._states.setdefault(did, CameraState())
        st.last_check_ts = now

    # ── 生命周期 ────────────────────────────────────────────────

    def reset(self) -> None:
        self._states.clear()
