"""状态容器：记住当前状态 + 5 分钟强制复核（不喂任务时间样本）。"""

from miloco.perception.engine.state_memory import StateContainer


def _container(**kw) -> StateContainer:
    return StateContainer(recheck_interval_sec=300.0, **kw)


def test_matched_rule_stays_active_and_rechecks_on_interval():
    c = _container()
    c.note_omni("cam", ["r1"], ["r1"], now=100.0)

    assert c.is_active("cam") is True
    # 100 + 300 = 400 才到复核点
    assert c.recheck_due(399.0) == []
    assert c.recheck_due(400.0) == ["cam"]

    c.mark_recheck_issued("cam", now=400.0)
    assert c.recheck_due(650.0) == []
    assert c.recheck_due(700.0) == ["cam"]


def test_unmatched_rule_is_removed_immediately():
    c = _container()
    c.note_omni("cam", ["r1", "r2"], ["r1", "r2"], now=100.0)
    c.note_omni("cam", ["r1", "r2"], ["r1"], now=200.0)
    assert c._states["cam"].rules == {"r1": 200.0}


def test_two_missed_rechecks_clear_state():
    c = _container()
    c.note_omni("cam", ["r1"], ["r1"], now=100.0)

    c.note_omni("cam", ["r1"], [], now=200.0)
    assert c.is_active("cam") is True

    c.note_omni("cam", ["r1"], [], now=300.0)
    assert c.is_active("cam") is False
    assert c.recheck_due(400.0) == []


def test_suggestion_signal_keeps_state_alive_without_rules():
    c = _container()
    c.note_omni("cam", [], [], now=100.0, has_signal=True)
    assert c.is_active("cam") is True
    assert c.recheck_due(400.0) == ["cam"]


def test_disabled_container_is_noop():
    c = StateContainer(recheck_interval_sec=300.0, enabled=False)
    c.note_omni("cam", ["r1"], ["r1"], now=100.0)
    assert c.is_active("cam") is False
    assert c.recheck_due(400.0) == []


def test_reset_clears_all_cameras():
    c = _container()
    c.note_omni("cam", ["r1"], ["r1"], now=100.0)
    c.reset()
    assert c.is_active("cam") is False
