"""Tests for LLM 提炼特殊放行项（家庭档案 + 本机位自定义任务）。"""

from __future__ import annotations

import asyncio

import pytest
from miloco.perception.engine.config import OmniConfig, SpecialPassConfig
from miloco.perception.engine.gate import special_pass as sp
from miloco.perception.engine.types import RuleCondition, SpecialPassItem


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    sp._ENTRIES.clear()
    sp._LOADED = False
    sp._INFLIGHT.clear()
    sp._RETRY_AT.clear()
    sp._TASKS.clear()
    sp._PROFILE_CACHE = None
    yield
    sp._ENTRIES.clear()
    sp._LOADED = False
    sp._INFLIGHT.clear()
    sp._RETRY_AT.clear()
    sp._TASKS.clear()
    sp._PROFILE_CACHE = None


def _patch_llm(monkeypatch, content: str) -> dict:
    captured: dict = {}

    async def fake_call_omni(payload, cfg, type="realtime", **kwargs):  # noqa: A002
        captured["payload"] = payload
        captured["cfg"] = cfg
        captured["kwargs"] = kwargs
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni_client.call_omni", fake_call_omni
    )
    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni_client.resolve_live_omni_config",
        lambda c: c,
    )
    return captured


async def _drain_tasks() -> None:
    await asyncio.sleep(0)
    tasks = list(sp._TASKS)
    if tasks:
        await asyncio.gather(*tasks)


async def test_extract_includes_rules_profile_and_caps(monkeypatch):
    content = """```json
{"items":[
  {"action":"老人捂胸口或扶墙","reason":"爷爷有高血压","source":"profile"},
  {"action":"老人捂胸口或扶墙","reason":"重复项","source":"profile"},
  {"action":"宝宝在爬行垫上爬","reason":"宝宝学爬","source":"rule:r1"}
],"covered":[{"rule_id":"r2","reason":"常规变化事件，默认问题能覆盖"}]}
```"""
    captured = _patch_llm(monkeypatch, content)

    items, covered, model = await sp._extract(
        profile="# 家庭档案\n- 爷爷有高血压",
        room_name="客厅",
        camera_prompt="关注老人",
        rules=[
            RuleCondition(
                rule_id="r1", rule_name="[baby] 爬行", query="宝宝在爬行垫上爬"
            ),
            RuleCondition(rule_id="r2", rule_name="[fall] 摔倒", query="有人摔倒"),
        ],
        generic_instructions="有没有变化？",
        input_layout="画面共 5 张，第 1 张是参考帧，第 2 到第 5 张是本窗口。",
        config=SpecialPassConfig(max_items=2, extra_instructions="重点看宠物"),
        omni_config=OmniConfig(model="test-model"),
    )

    user = captured["payload"]["user_content"]
    assert "爷爷有高血压" in user
    assert "宝宝在爬行垫上爬" in user
    assert "有人摔倒" in user
    assert "关注老人" in user
    assert "画面共 5 张，第 1 张是参考帧" in user
    # 写 prompt 的规范必须强调「变化」、禁止静态状态，且常规活动要交给默认问题
    assert "变化时刻" in user
    assert "禁止输出静态或持续状态" in user
    assert "常规活动默认问题已经明确覆盖" in user
    assert "喝水/吃东西、打哈欠、看手机" in user
    assert "补充要求：\n重点看宠物" in user
    assert captured["cfg"].max_completion_tokens == 10000
    assert model == "test-model"

    assert [i.action for i in items] == ["老人捂胸口或扶墙", "宝宝在爬行垫上爬"]
    assert items[1].source == "rule:r1"
    assert covered == [{"rule_id": "r2", "reason": "常规变化事件，默认问题能覆盖"}]


async def test_extract_rejects_invalid_json(monkeypatch):
    _patch_llm(monkeypatch, "不是 JSON")
    with pytest.raises(ValueError):
        await sp._extract(
            profile="p",
            room_name=None,
            camera_prompt=None,
            rules=[],
            generic_instructions="q",
            config=SpecialPassConfig(),
            omni_config=OmniConfig(),
        )


async def test_extract_rejects_empty_content(monkeypatch):
    _patch_llm(monkeypatch, "")
    with pytest.raises(ValueError):
        await sp._extract(
            profile="p",
            room_name=None,
            camera_prompt=None,
            rules=[],
            generic_instructions="q",
            config=SpecialPassConfig(),
            omni_config=OmniConfig(),
        )


async def test_prepare_caches_per_camera_and_skips_llm(monkeypatch):
    monkeypatch.setattr(sp, "_profile_text", lambda: "# 家庭档案\n- 爷爷有高血压")
    monkeypatch.setattr(sp, "_live_config", lambda c: OmniConfig(model="test-model"))
    calls: list[str] = []

    async def fake_extract(**kwargs):
        calls.append(kwargs["room_name"])
        return (
            [SpecialPassItem(action=f"{kwargs['room_name']}老人捂胸口")],
            [],
            "test-model",
        )

    monkeypatch.setattr(sp, "_extract", fake_extract)
    cfg = SpecialPassConfig()
    omni = OmniConfig(model="test-model")

    def prepare(did: str, room: str) -> list[SpecialPassItem]:
        return sp.prepare_camera(
            did=did,
            room_name=room,
            camera_prompt=None,
            rules=[],
            generic_instructions="q",
            config=cfg,
            omni_config=omni,
        )

    # 首窗没有缓存：返回空，后台刷新
    assert prepare("cam-a", "客厅") == []
    await _drain_tasks()
    assert [i.action for i in prepare("cam-a", "客厅")] == ["客厅老人捂胸口"]
    assert calls == ["客厅"]

    # 第二台相机单独提炼
    assert prepare("cam-b", "厨房") == []
    await _drain_tasks()
    assert [i.action for i in prepare("cam-b", "厨房")] == ["厨房老人捂胸口"]
    assert calls == ["客厅", "厨房"]

    # 同一机位 key 未变：直接用缓存，不再调 LLM
    prepare("cam-a", "客厅")
    assert calls == ["客厅", "厨房"]


async def test_prepare_drops_stale_items_when_rules_change(monkeypatch):
    monkeypatch.setattr(sp, "_profile_text", lambda: "# 家庭档案\n- 爷爷有高血压")
    monkeypatch.setattr(sp, "_live_config", lambda c: OmniConfig(model="test-model"))
    calls: list[str] = []

    async def fake_extract(**kwargs):
        calls.append(kwargs["rules"][0].rule_id if kwargs["rules"] else "none")
        return ([SpecialPassItem(action="某动作")], [], "test-model")

    monkeypatch.setattr(sp, "_extract", fake_extract)
    cfg = SpecialPassConfig()
    omni = OmniConfig(model="test-model")
    rule_a = RuleCondition(rule_id="a", rule_name="A", query="条件 A")

    sp.prepare_camera(
        did="cam",
        room_name="客厅",
        camera_prompt=None,
        rules=[rule_a],
        generic_instructions="q",
        config=cfg,
        omni_config=omni,
    )
    await _drain_tasks()

    # 规则变了：旧项立即不再喂给门禁，等新提炼完成
    assert (
        sp.prepare_camera(
            did="cam",
            room_name="客厅",
            camera_prompt=None,
            rules=[RuleCondition(rule_id="b", rule_name="B", query="条件 B")],
            generic_instructions="q",
            config=cfg,
            omni_config=omni,
        )
        == []
    )
    await _drain_tasks()
    assert calls == ["a", "b"]


def test_prepare_without_api_key_does_not_spawn(monkeypatch):
    monkeypatch.setattr(sp, "_profile_text", lambda: "# 家庭档案\n- 爷爷有高血压")
    monkeypatch.setattr(sp, "_live_config", lambda c: None)
    spawned: list[dict] = []
    monkeypatch.setattr(sp, "_spawn_refresh", lambda **kw: spawned.append(kw))

    assert (
        sp.prepare_camera(
            did="cam",
            room_name="客厅",
            camera_prompt=None,
            rules=[],
            generic_instructions="q",
            config=SpecialPassConfig(),
            omni_config=OmniConfig(),
        )
        == []
    )
    assert spawned == []


def test_prepare_with_empty_profile_but_rules_still_refines(monkeypatch):
    # 自定义任务不依赖家庭档案：档案为空也要走提炼，否则规则问题进不了 prompt。
    monkeypatch.setattr(sp, "_profile_text", lambda: "")
    monkeypatch.setattr(sp, "_live_config", lambda c: OmniConfig(model="m"))
    spawned: list[dict] = []
    monkeypatch.setattr(sp, "_spawn_refresh", lambda **kw: spawned.append(kw))

    assert (
        sp.prepare_camera(
            did="cam",
            room_name="客厅",
            camera_prompt=None,
            rules=[RuleCondition(rule_id="r1", rule_name="R", query="有人摔倒")],
            generic_instructions="q",
            config=SpecialPassConfig(),
            omni_config=OmniConfig(),
        )
        == []
    )
    assert len(spawned) == 1


def test_prepare_without_profile_and_rules_does_nothing(monkeypatch):
    monkeypatch.setattr(sp, "_profile_text", lambda: "")
    monkeypatch.setattr(sp, "_live_config", lambda c: OmniConfig(model="m"))
    spawned: list[dict] = []
    monkeypatch.setattr(sp, "_spawn_refresh", lambda **kw: spawned.append(kw))

    assert (
        sp.prepare_camera(
            did="cam",
            room_name="客厅",
            camera_prompt=None,
            rules=[],
            generic_instructions="q",
            config=SpecialPassConfig(),
            omni_config=OmniConfig(),
        )
        == []
    )
    assert spawned == []


def test_camera_key_changes_with_rules_profile_and_room():
    base = dict(
        profile="p",
        room_name="客厅",
        camera_prompt=None,
        generic_instructions="q",
        input_layout="第 1 张是参考帧，其余是本窗口。",
        model_name="m",
        max_items=4,
        max_action_chars=60,
        extra_instructions="",
    )
    rule_a = RuleCondition(rule_id="1", rule_name="n", query="q")
    rule_b = RuleCondition(rule_id="2", rule_name="n", query="q")

    k0 = sp._camera_key(**base, rules=[rule_a])
    assert k0 == sp._camera_key(**base, rules=[rule_a])
    assert k0 != sp._camera_key(**base, rules=[rule_b])
    assert k0 != sp._camera_key(**{**base, "profile": "p2"}, rules=[rule_a])
    assert k0 != sp._camera_key(**{**base, "room_name": "厨房"}, rules=[rule_a])
    assert k0 != sp._camera_key(**{**base, "max_items": 2}, rules=[rule_a])
    # 公共输入布局改了（参考帧/本窗口描述），提炼结果必须重跑
    assert k0 != sp._camera_key(
        **{**base, "input_layout": "玩法不同的布局"}, rules=[rule_a]
    )


def test_cache_round_trip(tmp_path):
    cfg = SpecialPassConfig()
    sp._ENTRIES["cam"] = sp._CameraEntry(
        key="k",
        generated_at=123.0,
        model="m",
        items=[SpecialPassItem(action="老人捂胸口", reason="r", source="profile")],
        covered=[{"rule_id": "r1", "reason": "默认能覆盖"}],
    )
    sp._write_cache(cfg)

    sp._ENTRIES.clear()
    sp._LOADED = False
    sp._load_cache(cfg)

    entry = sp._ENTRIES["cam"]
    assert entry.key == "k"
    assert entry.items[0].action == "老人捂胸口"
    assert entry.covered[0]["rule_id"] == "r1"
