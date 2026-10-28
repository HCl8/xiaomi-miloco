# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""退出确认：退出要两帧否定 + 强制复核一窗，复核说还在就撤销退出。

单窗 omni 误判（低头/喝水/看别处）实测约 15%，一次就断会话的话「连续工作」永远
攒不满。所以：第一帧否定只记候选，第二帧否定才退出；退出那一刻向感知层要一窗
（绕过门禁），复核回来说「还在」就直接撤掉宽限、恢复计时。
"""

from __future__ import annotations

import asyncio

import pytest
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import Rule, RuleCondition, RuleMode


def _rule(rule_id: str = "r-ses", exit_debounce_seconds: float = 5) -> Rule:
    return Rule(
        id=rule_id,
        name=rule_id,
        task_id="t1",
        mode=RuleMode.STATE,
        condition=RuleCondition(perceive_device_ids=["cam1"], query="有人"),
        exit_debounce_seconds=exit_debounce_seconds,
    )


def _runner(monkeypatch, exit_debounce_seconds: float = 5) -> RuleRunner:
    monkeypatch.setattr(
        "miloco.task_record.service.TaskRecordService.__init__", lambda self: None
    )
    return RuleRunner(
        rules=[_rule(exit_debounce_seconds=exit_debounce_seconds)],
        miot_proxy=None, rule_log_repo=None, task_record_service=object(),
    )


async def _feed(runner, *values, **kw):
    for v in values:
        await runner.update_state("r-ses", "cam1", v, "", **kw)


@pytest.mark.asyncio
async def test_single_false_is_only_a_candidate(monkeypatch):
    """一帧否定不断会话 —— omni 单窗误判是常态。"""
    runner = _runner(monkeypatch)
    await _feed(runner, True)
    await _feed(runner, False)
    assert runner._state["r-ses"].exit_debounce_task is None


@pytest.mark.asyncio
async def test_exit_rechecks_halfway_through_the_grace(monkeypatch):
    """退出不是立刻要窗，而是宽限走一半时复核一次（第一次）。"""
    runner = _runner(monkeypatch, exit_debounce_seconds=1.0)
    calls: list[tuple] = []
    runner.set_recheck_requester(lambda did, rule_id, ttl: calls.append((did, rule_id)))

    await _feed(runner, True, True)
    assert calls == [], "进入时不该要窗"

    await _feed(runner, False, False)
    state = runner._state["r-ses"]
    assert state.exit_debounce_task is not None
    assert state.exit_recheck_task is not None, "中点复核要排上"
    assert calls == [], "刚判退出时还不该要窗"

    await asyncio.sleep(0.6)
    assert calls == [("cam1", "r-ses")], "宽限一半要一次复核（带 rule_id）"

    # 人确实走了：继续报假，没有新的边沿，不重复排程
    await _feed(runner, False)
    assert calls == [("cam1", "r-ses")]
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_forced_recheck_true_cancels_the_exit(monkeypatch):
    """复核窗说「还在」→ 撤掉宽限，按仍在处理，不重发 ENTERED。"""
    runner = _runner(monkeypatch)
    runner.set_recheck_requester(lambda *_a: None)

    await _feed(runner, True, True)
    await _feed(runner, False, False)
    assert runner._state["r-ses"].exit_debounce_task is not None

    await runner.update_state("r-ses", "cam1", True, "", confirm_single=True)

    state = runner._state["r-ses"]
    assert state.exit_debounce_task is None
    assert state.last_rule_state is True


@pytest.mark.asyncio
async def test_requester_failure_keeps_the_exit_path(monkeypatch):
    """要窗失败只是少一次复核，退出宽限照常开始。"""
    runner = _runner(monkeypatch)

    def boom(*_a) -> None:
        raise RuntimeError("perception down")

    runner.set_recheck_requester(boom)
    await _feed(runner, True, True)
    await _feed(runner, False, False)

    assert runner._state["r-ses"].exit_debounce_task is not None
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_confirm_single_outside_grace_changes_nothing(monkeypatch):
    """没有宽限在跑时，confirm_single 不该改变进入方向的语义（正常边沿照常发）。"""
    runner = _runner(monkeypatch)
    await _feed(runner, False)
    await runner.update_state("r-ses", "cam1", True, "", confirm_single=True)

    state = runner._state["r-ses"]
    assert state.sources["cam1"].last_bool is True
    assert state.last_rule_state is True, "没有宽限时应按普通 ENTERED 处理"
    assert state.exit_debounce_task is None


@pytest.mark.asyncio
async def test_cancel_clears_other_sources_pending_enter(monkeypatch):
    """撤销退出后，别的机位残留的一帧 True 不能被当成下一轮的「第二帧」。"""
    runner = _runner(monkeypatch)
    runner._rules["r-ses"].condition.perceive_device_ids = ["cam1", "cam2"]

    await _feed(runner, True, True)
    await runner.update_state("r-ses", "cam2", True, "")
    await runner.update_state("r-ses", "cam2", False, "")
    await runner.update_state("r-ses", "cam2", False, "")
    await _feed(runner, False, False)
    state = runner._state["r-ses"]
    assert state.exit_debounce_task is not None

    # 模拟另一个机位在宽限里留下的一帧 True
    state.sources.setdefault("cam2", type(state.sources["cam1"])()).pending_enter = True

    runner.set_recheck_requester(lambda *_a: None)
    await runner.update_state("r-ses", "cam1", True, "", confirm_single=True)

    assert state.exit_debounce_task is None
    assert all(not src.pending_enter for src in state.sources.values())
