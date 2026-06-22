"""tests for miloco.miot.catalog_render."""

from __future__ import annotations

from miloco.miot.catalog_render import (
    SpecLine,
    _format_extra,
    _resolve_keys_for_device,
    _skeleton_signature,
    build_catalog,
)


def _empty_lru():
    return {"version": 1, "updated_at": None, "histories": {}}


def _light_device(did, name, model="x.light.y", online=True, room="客厅"):
    return {
        "did": did,
        "name": name,
        "room": room,
        "category": "light",
        "online": online,
        "model": model,
        "spec": {
            "prop.2.1": {
                "type_name": "on",
                "service_type_name": "light",
                "service_description": "Light",
                "format": "bool",
                "writeable": True,
                "readable": True,
            },
            "prop.2.2": {
                "type_name": "brightness",
                "service_type_name": "light",
                "service_description": "Light",
                "format": "uint8",
                "writeable": True,
                "readable": True,
                "value_range": [1, 100, 1],
                "unit": "%",
            },
        },
    }


# ─── SpecLine.render / _format_extra (regression: backend copy) ──────────────


def test_specline_prop_render_no_extra():
    sl = SpecLine(key="on", fmt="bool", wr="wr", extra="", unit="", is_action=False)
    assert sl.render() == "on|wr|bool"


def test_specline_action_render_with_params():
    sl = SpecLine(
        key="play-text", fmt="", wr="x", extra="text:string", unit="", is_action=True
    )
    assert sl.render() == "play-text|x|text:string"


def test_format_extra_range():
    assert _format_extra({"value_range": [0, 100, 1]}) == "[0,100;1]"


def test_format_extra_enum_preserves_order():
    extra = _format_extra(
        {
            "value_list": [
                {"name": "Cool", "value": 2},
                {"name": "Heat", "value": 5},
                {"name": "Auto", "value": 1},
            ]
        }
    )
    assert extra == "Cool=2,Heat=5,Auto=1"


# ─── 同设备同 type_name 消歧 ─────────────────────────────────────────────────


def test_resolve_keys_no_conflict():
    spec = {
        "prop.2.1": {"type_name": "on"},
        "prop.2.2": {"type_name": "brightness"},
    }
    assert _resolve_keys_for_device(spec) == {
        "prop.2.1": "on",
        "prop.2.2": "brightness",
    }


def test_resolve_keys_with_desc_disambiguation():
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "Switch 1"},
        "prop.3.1": {"type_name": "on", "service_description": "Switch 2"},
        "prop.4.1": {"type_name": "on", "service_description": "Switch 3"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "on@Switch_1"
    assert keys["prop.3.1"] == "on@Switch_2"
    assert keys["prop.4.1"] == "on@Switch_3"


# ─── 骨架签名 ────────────────────────────────────────────────────────────────


def test_skeleton_signature_includes_at_suffix():
    a = [
        SpecLine("on@Switch_1", "bool", "wr", "", "", False),
        SpecLine("on@Switch_2", "bool", "wr", "", "", False),
    ]
    b = [
        SpecLine("on@Key_1", "bool", "wr", "", "", False),
        SpecLine("on@Key_2", "bool", "wr", "", "", False),
    ]
    assert _skeleton_signature(a) != _skeleton_signature(b)


def test_skeleton_signature_same_when_extras_differ():
    a = [
        SpecLine("on", "bool", "wr", "", "", False),
        SpecLine("mode", "uint8", "wr", "Cool=2,Heat=5", "", False),
    ]
    b = [
        SpecLine("on", "bool", "wr", "", "", False),
        SpecLine("mode", "uint8", "wr", "Cool=2,Heat=5,Auto=1", "", False),
    ]
    assert _skeleton_signature(a) == _skeleton_signature(b)


# ─── build_catalog 端到端 ────────────────────────────────────────────────────


def test_build_catalog_merges_same_skeleton_devices():
    info = {
        "devices": [
            _light_device("lamp_001", "客厅主灯", "yeelink.light.color4"),
            _light_device("lamp_002", "餐厅吊灯", "mijia.light.bulb3", room="餐厅"),
        ],
    }
    r = build_catalog(info, whitelist=set(), lru_state=_empty_lru())
    text = r.text
    assert "lamp_001" in text and "lamp_002" in text
    sep_lines = [ln for ln in text.splitlines() if ln.strip() == "---"]
    assert len(sep_lines) == 1
    assert "on|wr|bool" in text
    assert "brightness|wr|uint8|[1,100;1]|%" in text
    assert "# 数据格式：" in text
    assert "did|device_name|room|category|status" in text
    assert "# models" not in text


def test_build_catalog_groups_separated_by_blank_lines():
    info = {
        "devices": [
            _light_device("lamp_001", "客厅主灯"),
            {
                "did": "fan_001",
                "name": "落地扇",
                "room": "卧室",
                "category": "fan",
                "online": True,
                "model": "x.fan.y",
                "spec": {
                    "prop.2.1": {
                        "type_name": "on",
                        "service_type_name": "fan",
                        "service_description": "Fan",
                        "format": "bool",
                        "writeable": True,
                        "readable": True,
                    },
                },
            },
        ],
    }
    text = build_catalog(info, whitelist=set(), lru_state=_empty_lru()).text
    assert "lamp_001" in text and "fan_001" in text
    assert "\n\n\n" in text


def test_build_catalog_empty_spec_devices_excluded_from_render():
    info = {
        "devices": [
            _light_device("lamp_001", "客厅主灯"),
            {
                "did": "sensor_001",
                "name": "温湿度",
                "room": "卧室",
                "category": "sensor",
                "online": False,
                "spec": {},
            },
        ],
    }
    r = build_catalog(info, lru_state=_empty_lru())
    assert "# 以下设备无可控属性" not in r.text
    assert "sensor_001" not in r.text
    assert "lamp_001" in r.text
    assert r.empty_count == 1


def test_build_catalog_token_budget_degrades_capacity():
    devs = []
    for i in range(50):
        d = {
            "did": f"dev_{i:03d}",
            "name": f"设备{i}",
            "room": "客厅",
            "category": f"cat-{i}",
            "online": True,
            "model": f"unique.model.{i}",
            "spec": {
                f"prop.2.{j}": {
                    "type_name": f"p{i}-{j}",
                    "service_type_name": f"svc-{i}",
                    "service_description": "S",
                    "format": "uint8",
                    "writeable": True,
                    "readable": True,
                    "value_range": [0, 100, 1],
                }
                for j in range(12)
            },
        }
        devs.append(d)
    info = {"devices": devs}
    r = build_catalog(info, whitelist=set(), token_budget=50, lru_state=_empty_lru())
    assert r.capacity == 5

# ─── LRU 合并 ────────────────────────────────────────────────────────────────

from miloco.miot.catalog_render import cold_start_keys, merged_keys


def test_cold_start_dedup_and_cap():
    keys = cold_start_keys(
        ["a", "b", "a", "c", "d", "e", "f", "g", "h"], capacity=5
    )
    assert keys == ["a", "b", "c", "d", "e"]


def test_merged_keys_translates_iid_to_type_name():
    state = {"histories": {"dev1": ["prop.2.2", "prop.2.1"]}}
    iid_to_key = {
        "prop.2.1": "brightness",
        "prop.2.2": "color_temp",
    }
    merged = merged_keys(
        "dev1",
        cold_start=["on", "brightness", "battery"],
        capacity=5,
        state=state,
        iid_to_key=iid_to_key,
    )
    assert merged == ["color_temp", "brightness", "on", "battery"]


def test_merged_keys_drops_unknown_iids():
    state = {"histories": {"dev1": ["prop.99.99", "prop.2.1"]}}
    iid_to_key = {"prop.2.1": "brightness"}
    merged = merged_keys(
        "dev1",
        cold_start=["on"],
        capacity=5,
        state=state,
        iid_to_key=iid_to_key,
    )
    assert merged == ["brightness", "on"]


def test_merged_keys_with_explicit_state():
    state = {"histories": {"dev1": ["prop.2.1", "prop.2.2"]}}
    iid_to_key = {"prop.2.1": "alpha", "prop.2.2": "beta"}
    merged = merged_keys(
        "dev1",
        cold_start=["x", "y"],
        capacity=5,
        state=state,
        iid_to_key=iid_to_key,
    )
    assert merged == ["alpha", "beta", "x", "y"]
