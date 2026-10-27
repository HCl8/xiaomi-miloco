"""Tracking 过线计数：进入 / 出去 / 经过。

不走 rule 状态机（会被边沿去重、单帧抗抖合并），直接用 tracker 的
``per_frame_track_boxes``（逐抽帧、带 track_id、xyxy 像素）跟每个 track
的框中心：跨过门线 → 进入/出去；始终在门外且横向位移够 → 经过。

计数结果直接写 task_record 的 event，不依赖 agent。
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from miloco.perception.engine.config import TrackLineCameraConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackCountEvent:
    label: str  # 进入 / 出去 / 经过
    track_id: int
    at_ms: int


@dataclass
class _TrackState:
    initialized: bool
    last_side: bool | None  # True = inside
    last_seen_ms: int
    last_cx: float
    last_cy: float
    min_x: float
    max_x: float
    started_outside: bool
    crossed: bool = False
    pass_counted: bool = False


class TrackLineCounter:
    """单机位、跨窗口的 track 过线状态机。线程/协程内串行使用。"""

    def __init__(self, config: TrackLineCameraConfig) -> None:
        (ax, ay), (bx, by) = config.line
        self._ax, self._ay = ax, ay
        self._vx, self._vy = bx - ax, by - ay
        self._length = math.hypot(self._vx, self._vy) or 1.0
        ix, iy = config.inside
        inside_cross = self._vx * (iy - ay) - self._vy * (ix - ax)
        if abs(inside_cross) / self._length < 1e-6:
            raise ValueError("inside point lies on the line")
        self._inside_sign = inside_cross > 0
        self._deadband = max(0.0, float(config.line_deadband))
        self._pass_min_dx = max(0.0, float(config.pass_min_dx))
        self._max_gap_ms = max(1.0, float(config.max_gap_sec) * 1000)
        self._tracks: dict[int, _TrackState] = {}

    def _side(self, cx: float, cy: float) -> bool | None:
        cross = self._vx * (cy - self._ay) - self._vy * (cx - self._ax)
        if abs(cross) / self._length < self._deadband:
            return None
        return (cross > 0) == self._inside_sign

    def _crossing_u(self, x0: float, y0: float, x1: float, y1: float) -> float | None:
        """两点连线与门线的交点在线段上的参数 u（0..1 在门洞内）。"""
        dx, dy = x1 - x0, y1 - y0
        denom = self._vx * dy - self._vy * dx
        if abs(denom) < 1e-9:
            return None
        px, py = x0 - self._ax, y0 - self._ay
        return (px * dy - py * dx) / denom

    def process(
        self,
        *,
        width: int,
        height: int,
        start_ms: int,
        fps: int,
        per_frame_boxes: list[dict[int, tuple[int, int, int, int]]],
        now_ms: int,
    ) -> list[TrackCountEvent]:
        events: list[TrackCountEvent] = []
        if width <= 0 or height <= 0:
            return events
        fps = max(1, int(fps or 1))
        for i, boxes in enumerate(per_frame_boxes):
            ts = start_ms + int(i * 1000 / fps)
            for track_id, box in boxes.items():
                x1, y1, x2, y2 = box
                cx = ((x1 + x2) / 2.0) / width
                cy = ((y1 + y2) / 2.0) / height
                side = self._side(cx, cy)
                state = self._tracks.get(int(track_id))
                if state is None:
                    self._tracks[int(track_id)] = _TrackState(
                        initialized=side is not None,
                        last_side=side,
                        last_seen_ms=ts,
                        last_cx=cx,
                        last_cy=cy,
                        min_x=cx,
                        max_x=cx,
                        started_outside=(side is False),
                    )
                    continue
                state.last_seen_ms = ts
                state.min_x = min(state.min_x, cx)
                state.max_x = max(state.max_x, cx)
                if side is None:
                    continue
                if not state.initialized:
                    state.initialized = True
                    state.last_side = side
                    state.last_cx, state.last_cy = cx, cy
                    state.started_outside = not side
                    continue
                if side == state.last_side:
                    state.last_cx, state.last_cy = cx, cy
                    continue
                u = self._crossing_u(state.last_cx, state.last_cy, cx, cy)
                state.last_side = side
                state.last_cx, state.last_cy = cx, cy
                # 只有交点落在门洞线段内才算进入/出去；门外走廊横向走过
                # （交点在线段外）不计数，留作"经过"结算。
                if u is None or not (0.0 <= u <= 1.0):
                    continue
                state.crossed = True
                label = "进入" if side else "出去"
                events.append(TrackCountEvent(label=label, track_id=int(track_id), at_ms=ts))
        self._evict(now_ms, events)
        return events

    def _evict(self, now_ms: int, events: list[TrackCountEvent]) -> None:
        stale = [
            tid
            for tid, st in self._tracks.items()
            if now_ms - st.last_seen_ms > self._max_gap_ms
        ]
        for tid in stale:
            st = self._tracks.pop(tid)
            if (
                st.initialized
                and st.started_outside
                and not st.crossed
                and not st.pass_counted
                and st.max_x - st.min_x >= self._pass_min_dx
            ):
                st.pass_counted = True
                events.append(
                    TrackCountEvent(label="经过", track_id=tid, at_ms=st.last_seen_ms)
                )


_COUNTERS: dict[str, TrackLineCounter] = {}
_SERVICE = None


def _service():
    global _SERVICE
    if _SERVICE is None:
        from miloco.task_record.service import TaskRecordService

        _SERVICE = TaskRecordService()
    return _SERVICE


def _append_event(task_id: str, event: TrackCountEvent) -> None:
    from miloco.utils.time_utils import ms_to_iso_local

    _service().event_append(
        task_id,
        f"{event.label} | track {event.track_id}",
        at=ms_to_iso_local(event.at_ms),
    )


async def run_track_count(did: str, packet, config: TrackLineCameraConfig) -> None:
    """处理一个窗口的 IdentityPacket；失败只告警，不影响感知链路。"""
    try:
        frames = getattr(packet, "all_frames", None) or []
        if not frames:
            return
        height, width = frames[0].shape[:2]
        info = packet.frame_info
        counter = _COUNTERS.get(did)
        if counter is None:
            counter = TrackLineCounter(config)
            _COUNTERS[did] = counter
        events = counter.process(
            width=int(width),
            height=int(height),
            start_ms=int(info.start_timestamp),
            fps=int(info.fps or 1),
            per_frame_boxes=getattr(packet, "per_frame_track_boxes", None) or [],
            now_ms=int(info.end_timestamp),
        )
        for event in events:
            try:
                await asyncio.to_thread(_append_event, config.task_id, event)
            except Exception:  # noqa: BLE001 — 单条写失败不能中断感知
                logger.warning(
                    "track_count event append failed did=%s label=%s track=%s",
                    did,
                    event.label,
                    event.track_id,
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.warning("track_count failed did=%s", did, exc_info=True)
