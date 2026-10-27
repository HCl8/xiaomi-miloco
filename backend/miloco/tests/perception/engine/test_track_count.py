"""Tests for tracking line-crossing counter (enter / exit / pass)."""

from __future__ import annotations

from miloco.perception.engine.config import TrackLineCameraConfig
from miloco.perception.engine.track_count import TrackLineCounter


def _cfg(**kw) -> TrackLineCameraConfig:
    base = dict(
        line=[[0.0, 0.4], [1.0, 0.4]],
        inside=[0.5, 0.7],
        task_id="t",
        pass_min_dx=0.15,
        max_gap_sec=8.0,
        line_deadband=0.015,
    )
    base.update(kw)
    return TrackLineCameraConfig(**base)


def _box(cx: float, cy: float) -> tuple[int, int, int, int]:
    # width=height=100, so normalized == pixels
    return (int(cx) - 5, int(cy) - 5, int(cx) + 5, int(cy) + 5)


def _frames(*frames: dict[int, tuple[int, int, int, int]]):
    return list(frames)


def _run(counter: TrackLineCounter, frames, start_ms=0, fps=1, now_ms=None):
    if now_ms is None:
        now_ms = start_ms + max(1, len(frames)) * 1000 // max(1, fps)
    return counter.process(
        width=100,
        height=100,
        start_ms=start_ms,
        fps=fps,
        per_frame_boxes=frames,
        now_ms=now_ms,
    )


def test_enter_then_exit_counts_both():
    c = TrackLineCounter(_cfg())
    ev = _run(c, _frames({1: _box(50, 20)}, {1: _box(50, 60)}))
    assert [(e.label, e.track_id) for e in ev] == [("进入", 1)]
    ev = _run(c, _frames({1: _box(50, 60)}, {1: _box(50, 20)}), start_ms=2000)
    assert [(e.label, e.track_id) for e in ev] == [("出去", 1)]


def test_pass_counted_on_eviction_when_never_crossed():
    c = TrackLineCounter(_cfg())
    ev = _run(c, _frames({2: _box(10, 20)}, {2: _box(80, 20)}), now_ms=1_000)
    assert ev == []
    ev = _run(c, [], start_ms=10_000, now_ms=11_000)
    assert [(e.label, e.track_id) for e in ev] == [("经过", 2)]


def test_no_pass_when_track_crossed():
    c = TrackLineCounter(_cfg())
    _run(c, _frames({3: _box(10, 20)}), now_ms=1_000)
    ev = _run(c, _frames({3: _box(80, 60)}), start_ms=1000)
    assert [(e.label, e.track_id) for e in ev] == [("进入", 3)]
    ev = _run(c, [], start_ms=20_000, now_ms=21_000)
    assert ev == []  # crossed, not a pass


def test_deadband_does_not_count_as_crossing():
    c = TrackLineCounter(_cfg())
    # first sighting exactly on the line -> side unknown, no count
    ev = _run(c, _frames({4: _box(50, 40)}, {4: _box(50, 60)}))
    assert ev == []
    ev = _run(c, _frames({4: _box(50, 20)}), start_ms=2000)
    assert [(e.label, e.track_id) for e in ev] == [("出去", 4)]


def test_multiple_crossings_each_counted():
    c = TrackLineCounter(_cfg())
    ev = _run(
        c,
        _frames(
            {6: _box(50, 20)},
            {6: _box(50, 60)},
            {6: _box(50, 20)},
            {6: _box(50, 60)},
        ),
    )
    assert [(e.label, e.track_id) for e in ev] == [("进入", 6), ("出去", 6), ("进入", 6)]


def test_two_tracks_same_window_are_independent():
    c = TrackLineCounter(_cfg())
    ev = _run(
        c,
        _frames(
            {7: _box(10, 20), 8: _box(70, 20)},
            {7: _box(10, 60), 8: _box(70, 60)},
        ),
    )
    assert sorted((e.label, e.track_id) for e in ev) == [("进入", 7), ("进入", 8)]


def test_pass_requires_min_displacement():
    c = TrackLineCounter(_cfg(pass_min_dx=0.5))
    _run(c, _frames({9: _box(40, 20)}, {9: _box(50, 20)}), now_ms=1_000)
    ev = _run(c, [], start_ms=10_000, now_ms=11_000)
    assert ev == []


def test_short_track_start_inside_no_pass():
    c = TrackLineCounter(_cfg())
    _run(c, _frames({10: _box(10, 60)}, {10: _box(80, 60)}), now_ms=1_000)
    ev = _run(c, [], start_ms=10_000, now_ms=11_000)
    assert ev == []


def test_crossing_outside_door_segment_not_counted():
    # line 只覆盖右侧门洞 [0.7,1.0]；左侧/中部的跨线（门外走廊）不算进入/出去
    c = TrackLineCounter(_cfg(line=[[0.7, 0.4], [1.0, 0.4]]))
    ev = _run(
        c,
        _frames(
            {11: _box(10, 20)},  # outside
            {11: _box(10, 60)},  # 跨线在 x=0.1，门洞外 -> 不计数
            {11: _box(90, 20)},  # 斜穿到右侧，交点 x=0.5，仍在门洞外
            {11: _box(90, 60)},  # 门洞内 x=0.9 跨线 -> 进入
        ),
    )
    assert [(e.label, e.track_id) for e in ev] == [("进入", 11)]
