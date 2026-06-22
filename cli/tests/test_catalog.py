"""tests for miloco_cli.catalog (utility functions)."""

from __future__ import annotations

from miloco_cli.catalog import (
    SpecLine,
    _format_extra,
    _resolve_keys_for_device,
)

# ─── SpecLine.render / _format_extra ──────────────────────────────────────────


def test_specline_prop_render_no_extra():
    sl = SpecLine(key="on", fmt="bool", wr="wr", extra="", unit="", is_action=False)
    assert sl.render() == "on|wr|bool"


def test_specline_prop_render_with_extra_and_unit():
    sl = SpecLine(
        key="brightness",
        fmt="uint8",
        wr="wr",
        extra="[1,100;1]",
        unit="%",
        is_action=False,
    )
    assert sl.render() == "brightness|wr|uint8|[1,100;1]|%"


def test_specline_action_render_no_params():
    sl = SpecLine(key="turn-on", fmt="", wr="x", extra="", unit="", is_action=True)
    assert sl.render() == "turn-on|x"


def test_specline_action_render_with_params():
    sl = SpecLine(
        key="play-text", fmt="", wr="x", extra="text:string", unit="", is_action=True
    )
    assert sl.render() == "play-text|x|text:string"


def test_format_extra_range():
    assert _format_extra({"value_range": [0, 100, 1]}) == "[0,100;1]"


def test_format_extra_range_no_step():
    assert _format_extra({"value_range": [0, 100]}) == "[0,100]"


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


def test_format_extra_action_params():
    extra = _format_extra(
        {"in_params": [{"name": "text", "format": "string"}]}
    )
    assert extra == "text:string"


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


def test_resolve_keys_chinese_description():
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "左键"},
        "prop.3.1": {"type_name": "on", "service_description": "中键"},
        "prop.4.1": {"type_name": "on", "service_description": "右键"},
        "prop.10.1": {"type_name": "on", "service_description": "指示灯"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "on@左键"
    assert keys["prop.3.1"] == "on@中键"
    assert keys["prop.4.1"] == "on@右键"
    assert keys["prop.10.1"] == "on@指示灯"


def test_resolve_keys_fallback_to_raw_iid():
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "Main"},
        "prop.3.1": {"type_name": "on", "service_description": "Main"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "prop.2.1"
    assert keys["prop.3.1"] == "prop.3.1"
