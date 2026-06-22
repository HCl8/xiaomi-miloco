"""设备目录 TSV 渲染器（spec-injection-plan §2-§5）。

由 ``MiotService.catalog_for_session()`` 调用。输入：home_info dict + LRU state
+ whitelist —— 输出 plain text TSV 目录字符串。

后端在 control / status 成功路径自动写入 LRU（``LRUStore.touch``），
本模块只读 snapshot。纯函数，无副作用、无 HTTP 调用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# 包内 whitelist.json —— 与当前文件同行。
WHITELIST_PATH = Path(__file__).parent / "whitelist.json"


# ─── LRU 工具 ─────────────────────────────────────────────────────────────────

DEFAULT_CAPACITY = 7


def _empty_lru_state() -> dict:
    return {"version": 1, "updated_at": None, "histories": {}}


def cold_start_keys(
    spec_keys_in_order: Iterable[str],
    capacity: int = DEFAULT_CAPACITY,
) -> list[str]:
    """冷启动填充：按 spec 原序最多取 capacity 个 key。"""
    out: list[str] = []
    for k in spec_keys_in_order:
        if k in out:
            continue
        out.append(k)
        if len(out) >= capacity:
            break
    return out


def merged_keys(
    did: str,
    cold_start: list[str],
    capacity: int = DEFAULT_CAPACITY,
    *,
    state: dict | None = None,
    iid_to_key: dict[str, str] | None = None,
) -> list[str]:
    """目录构建用：返回翻译后的 type_name 列表（LRU 优先，cold_start 顶上）。

    LRU 里存 iid 形态；用 ``iid_to_key`` 翻译成 type_name 再合并 cold_start。
    翻不到的 iid（spec 改过、白名单缩过）静默丢弃。
    """
    if state is None:
        state = _empty_lru_state()
    if iid_to_key is None:
        iid_to_key = {}
    lru_iids = list(state["histories"].get(did, []))
    lru_keys = [iid_to_key[i] for i in lru_iids if i in iid_to_key]
    out: list[str] = []
    for k in lru_keys + cold_start:
        if k in out:
            continue
        out.append(k)
        if len(out) >= capacity:
            break
    return out


# ─── 数据结构 ─────────────────────────────────────────────────────────────────


@dataclass
class SpecLine:
    """一行 spec（prop 或 action）渲染所需的最小元数据。"""

    key: str
    fmt: str
    wr: str
    extra: str
    unit: str
    is_action: bool
    annotation: str = ""

    def render(self) -> str:
        if self.is_action:
            if self.extra:
                line = f"{self.key}|{self.wr}|{self.extra}"
            else:
                line = f"{self.key}|{self.wr}"
        else:
            parts = [self.key, self.wr, self.fmt]
            if self.extra or self.unit:
                parts.append(self.extra)
            if self.unit:
                parts.append(self.unit)
            line = "|".join(parts)
        if self.annotation:
            line = f"{line}  # {self.annotation}"
        return line


@dataclass
class DeviceCatalogEntry:
    did: str
    name: str
    room: str
    category: str
    online: bool
    model: str
    spec_lines: list[SpecLine] = field(default_factory=list)
    cold_spec_lines: list[SpecLine] = field(default_factory=list)


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

_DESC_FORBID_RE = re.compile(r"[\s|,:=@]+")


def normalize_desc(desc: str | None) -> str:
    if not desc:
        return ""
    s = _DESC_FORBID_RE.sub("_", desc.strip())
    return s.strip("_")


def _escape(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).replace("|", r"\|")


def _format_extra(entry: dict) -> str:
    value_range = entry.get("value_range")
    value_list = entry.get("value_list")
    in_params = entry.get("in_params")

    if value_range and isinstance(value_range, list) and len(value_range) >= 2:
        lo, hi = value_range[0], value_range[1]
        step = value_range[2] if len(value_range) >= 3 else None
        if step is None:
            return f"[{_num_str(lo)},{_num_str(hi)}]"
        return f"[{_num_str(lo)},{_num_str(hi)};{_num_str(step)}]"

    if value_list and isinstance(value_list, list):
        items = []
        for v in value_list:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name", "")).replace("|", "").replace(",", "").replace(":", "")
            val = v.get("value")
            items.append(f"{name}={_num_str(val) if isinstance(val, (int, float)) else val}")
        if items:
            return ",".join(items)

    if in_params and isinstance(in_params, list):
        items = []
        for p in in_params:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).replace("|", "").replace(",", "").replace(":", "").replace("=", "")
            fmt = str(p.get("format", "")).replace("|", "").replace(",", "")
            items.append(f"{name}:{fmt}")
        if items:
            return ",".join(items)

    return ""


def _num_str(n) -> str:
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if n.is_integer():
            return str(int(n))
        return repr(n).rstrip("0").rstrip(".") or "0"
    return str(n)


# ─── 白名单 ───────────────────────────────────────────────────────────────────


def load_whitelist(path: Path | str = WHITELIST_PATH) -> set[tuple[str, str, str]]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        (e["service_type_name"], e["kind"], e["type_name"])
        for e in data.get("entries", [])
        if isinstance(e, dict)
        and e.get("service_type_name")
        and e.get("kind")
        and e.get("type_name")
    }


def _is_whitelisted(
    entry: dict, kind: str, whitelist: set[tuple[str, str, str]]
) -> bool:
    if not whitelist:
        return True
    service_type = entry.get("service_type_name")
    type_name = entry.get("type_name")
    if not service_type or not type_name:
        return False
    return (service_type, kind, type_name) in whitelist


# ─── 设备 spec 准备 ───────────────────────────────────────────────────────────


def _resolve_keys_for_device(spec: dict) -> dict[str, str]:
    if not isinstance(spec, dict):
        return {}

    counts: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name")
        if type_name:
            counts[type_name] = counts.get(type_name, 0) + 1

    result: dict[str, str] = {}
    desc_used: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name")
        if not type_name:
            continue
        if counts.get(type_name, 0) <= 1:
            result[iid] = type_name
            continue
        normalized_desc = normalize_desc(entry.get("service_description"))
        if normalized_desc:
            key = f"{type_name}@{normalized_desc}"
            desc_used[key] = desc_used.get(key, 0) + 1
            result[iid] = key
        else:
            result[iid] = iid

    for iid, key in list(result.items()):
        if "@" in key and desc_used.get(key, 0) > 1:
            result[iid] = iid

    return result


def _is_iid_action(iid: str) -> bool:
    return iid.startswith("action.")


def _build_spec_line(iid: str, entry: dict, key: str) -> SpecLine:
    is_action = _is_iid_action(iid)
    annotation = _build_annotation(iid, entry, key)
    if is_action:
        extra = _format_extra(entry)
        return SpecLine(key=key, fmt="", wr="x", extra=extra, unit="", is_action=True, annotation=annotation)

    fmt = str(entry.get("format", "") or "")
    writeable = bool(entry.get("writeable"))
    readable = bool(entry.get("readable"))
    if writeable and readable:
        wr = "wr"
    elif writeable:
        wr = "w"
    else:
        wr = "r"
    extra = _format_extra(entry)
    unit = ""
    if entry.get("unit"):
        unit = str(entry["unit"]).replace("|", "").replace(",", "")
    return SpecLine(key=key, fmt=fmt, wr=wr, extra=extra, unit=unit, is_action=False, annotation=annotation)


def _build_annotation(iid: str, entry: dict, key: str) -> str:
    desc = str(entry.get("description") or "")
    svc_desc = str(entry.get("service_description") or "")

    if key == iid:
        return _clean_annotation(desc) if desc else ""

    if "@" in key:
        type_name = key.split("@")[0]
    else:
        type_name = key

    prop_desc_en = str(entry.get("prop_description") or "")
    normalized_en = prop_desc_en.strip().lower().replace(" ", "-").replace("_", "-")
    if normalized_en == type_name:
        return ""

    if svc_desc and desc.startswith(svc_desc):
        type_desc = desc[len(svc_desc):].strip()
        if type_desc:
            return _clean_annotation(type_desc)

    return ""


_MAX_ANNOTATION_LEN = 20


def _clean_annotation(s: str) -> str:
    s = s.replace("|", "").replace("(", "").replace(")", "")
    if len(s) > _MAX_ANNOTATION_LEN:
        s = s[:_MAX_ANNOTATION_LEN] + "…"
    return s


def _device_filtered_keys_in_order(
    spec: dict,
    whitelist: set[tuple[str, str, str]],
    iid_to_key: dict[str, str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        kind = "action" if _is_iid_action(iid) else "prop"
        if kind == "prop":
            if not entry.get("writeable") and not entry.get("readable"):
                continue
        if not _is_whitelisted(entry, kind, whitelist):
            continue
        key = iid_to_key.get(iid)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _build_lines_for_device(
    spec: dict,
    keys: list[str],
    iid_to_key: dict[str, str],
) -> list[SpecLine]:
    key_to_iid = {v: k for k, v in iid_to_key.items()}
    out: list[SpecLine] = []
    for k in sorted(keys):  # 字典序——LRU 影响集合，不影响顺序
        iid = key_to_iid.get(k)
        if not iid:
            continue
        entry = spec.get(iid)
        if not isinstance(entry, dict):
            continue
        out.append(_build_spec_line(iid, entry, k))
    return out


# ─── 设备选 50 ────────────────────────────────────────────────────────────────


def _device_priority_key(
    device: dict, whitelist_categories: set[str]
) -> tuple:
    category = device.get("category") or ""
    return (
        not bool(device.get("online")),
        not bool(device.get("name")),
        category not in whitelist_categories,
    )


def select_devices(
    devices: list[dict], whitelist: set[tuple[str, str, str]], cap: int = 50
) -> tuple[list[dict], list[dict]]:
    whitelist_categories = {service_type for service_type, _, _ in whitelist}
    sorted_devs = sorted(devices, key=lambda d: _device_priority_key(d, whitelist_categories))
    return sorted_devs[:cap], sorted_devs[cap:]


# ─── 骨架签名 + 旁挂 ──────────────────────────────────────────────────────────


def _skeleton_signature(lines: list[SpecLine]) -> str:
    triples = sorted({(line.key, line.fmt, line.wr) for line in lines})
    return "\n".join(f"{key}|{fmt}|{wr}" for key, fmt, wr in triples)


def _pretty_render_group(
    group_devices: list[DeviceCatalogEntry],
    *,
    sharing_threshold: float = 0.8,
    _allow_degrade: bool = True,
) -> list[str]:
    if not group_devices:
        return []
    all_keys = sorted({line.key for device in group_devices for line in device.spec_lines})

    device_renders: list[dict[str, str]] = [
        {line.key: line.render() for line in device.spec_lines}
        for device in group_devices
    ]

    shared_keys: list[str] = []
    sidehang_keys: set[str] = set()
    for key in all_keys:
        renders = {dr.get(key) for dr in device_renders}
        if len(renders) == 1 and None not in renders:
            shared_keys.append(key)
        else:
            sidehang_keys.add(key)

    avg_rows = sum(len(d.spec_lines) for d in group_devices) / len(group_devices)
    if (
        _allow_degrade
        and avg_rows > 0
        and len(shared_keys) / avg_rows < sharing_threshold
    ):
        subgroups: dict[str, list[DeviceCatalogEntry]] = {}
        for d in group_devices:
            text = "\n".join(sorted(line.render() for line in d.spec_lines))
            subgroups.setdefault(text, []).append(d)
        out: list[str] = []
        for sub in sorted(subgroups.values(), key=lambda g: -len(g)):
            if out:
                out.extend(["", ""])
            out.extend(_pretty_render_group(sub, _allow_degrade=False))
        return out

    out_lines: list[str] = []
    for device in sorted(group_devices, key=lambda d: (not d.online, d.did)):
        out_lines.append(
            "|".join([
                _escape(device.did),
                _escape(device.name),
                _escape(device.room),
                _escape(device.category),
                "online" if device.online else "offline",
            ])
        )
        for line in sorted(device.spec_lines, key=lambda l: l.key):
            if line.key in sidehang_keys:
                out_lines.append(f"  + {line.render()}")

    if shared_keys:
        out_lines.append("---")
        first_device_lines = {line.key: line for line in group_devices[0].spec_lines}
        for key in sorted(shared_keys):
            out_lines.append(first_device_lines[key].render())

    return out_lines


# ─── 顶层入口 ─────────────────────────────────────────────────────────────────


@dataclass
class CatalogResult:
    text: str
    selected_count: int
    overflow_count: int
    empty_count: int
    capacity: int
    estimated_tokens: int


_TOKEN_BUDGET = 5000

_FORMAT_LEGEND = [
    "# 数据格式：",
    "#   did|device_name|room|category|status     // 设备信息行",
    "#     + prop/action                          // 当前设备独有的 prop/action，以 '  + ' 前缀表示",
    "#   ...",
    "#   ---                                      // 分隔线，下方是整组共享属性",
    "#   spec_name|access|format|constraint|unit  // prop 行",
    "#   spec_name|access|in_params               // action 行",
    "#   ...",
    "#   (空两行)                                 // 组分隔：组与组之间空两行；拥有共享属性的设备归为一组，空行下方是新组，重复以上结构",
    "#",
    "# 字段解释：",
    "#   category：设备类别，如 light / air-conditioner",
    "#   status：设备状态，只有 online / offline 两种",
    "#   spec_name：prop / action 的名字，作为 miloco-cli device (control / props / action) 第二个参数；",
    "#     形如 on / brightness / play-text；同名冲突时自动带 @<子设备描述> 后缀消歧，如 on@左键",
    "#   行尾 ``  # 注释``（如有）：人类可读的中文说明，传入 cli 时忽略",
    "#   access：权限，必选；只能取 wr=读写 / w=只写 / r=只读（不能 control）/ x=可执行（仅 action）四值",
    "#   format：值的数据类型，可选；取值如 bool / uint8 / int8 / float 等",
    "#   constraint：数值约束；格式 1：范围 [min,max;step]；格式 2：枚举 Cool=2,Heat=5",
    "#   in_params：动作入参类型说明（name:format,..），CLI 调用时只按顺序传值，不传参数名",
    "#     例：play-text|x|text-content:string → miloco-cli device action <did> play-text \"文本\"",
    "#         start-cook|x|cook-mode:uint8   → miloco-cli device action <did> start-cook 1",
    "#   unit：物理单位，可选；如 celsius / percentage / kelvin 等",
]

_TOKEN_PUNCT = frozenset("|()=,:;[]{}.@#\"'<>!?+-*/\\")


def _estimate_tokens(text: str) -> int:
    punct_chars = sum(1 for c in text if c in _TOKEN_PUNCT)
    cjk_chars = sum(1 for c in text if not c.isascii())
    rest_chars = len(text) - punct_chars - cjk_chars
    return max(1, int(punct_chars + rest_chars / 3.2 + cjk_chars / 0.9))


def _build_with_capacity(
    devices: list[dict],
    *,
    whitelist: set[tuple[str, str, str]],
    capacity: int,
    lru_state: dict,
) -> tuple[list[DeviceCatalogEntry], list[DeviceCatalogEntry]]:
    populated: list[DeviceCatalogEntry] = []
    empty: list[DeviceCatalogEntry] = []
    for device in devices:
        spec = device.get("spec") or {}
        iid_to_key = _resolve_keys_for_device(spec)
        cold_keys = _device_filtered_keys_in_order(spec, whitelist, iid_to_key)[:capacity]
        cold_spec_lines = _build_lines_for_device(spec, cold_keys, iid_to_key)
        merged = merged_keys(
            device.get("did", ""),
            cold_keys,
            capacity=capacity,
            state=lru_state,
            iid_to_key=iid_to_key,
        )
        available_keys = set(iid_to_key.values())
        valid_keys = [key for key in merged if key in available_keys]
        spec_lines = _build_lines_for_device(spec, valid_keys, iid_to_key)
        catalog_entry = DeviceCatalogEntry(
            did=device.get("did", ""),
            name=device.get("name", "") or "",
            room=device.get("room") or "",
            category=device.get("category") or "",
            online=bool(device.get("online")),
            model=device.get("model") or "",
            spec_lines=spec_lines,
            cold_spec_lines=cold_spec_lines,
        )
        if spec_lines:
            populated.append(catalog_entry)
        else:
            empty.append(catalog_entry)
    return populated, empty


def _render_catalog(
    populated: list[DeviceCatalogEntry],
    *,
    sharing_threshold: float,
) -> str:
    groups: dict[str, list[DeviceCatalogEntry]] = {}
    for e in populated:
        sig = _skeleton_signature(e.cold_spec_lines or e.spec_lines)
        groups.setdefault(sig, []).append(e)
    group_list = sorted(groups.values(), key=lambda g: -len(g))

    parts: list[str] = ["# devices catalog"]
    parts.append(
        "# 本目录是高频子集 + 生成时刻快照，随时可能过时、且未必收全——"
        "目标数量不定（复数语义、可能多台）的查询或控制，必须先 device list 拉最新全量再逐台处理；"
        "看得见的同类设备也未必齐全，看见 ≠ 全部，绝不可当全量"
    )
    parts.extend(_FORMAT_LEGEND)
    for i, g in enumerate(group_list):
        if i > 0:
            parts.extend(["", ""])
        parts.extend(_pretty_render_group(g, sharing_threshold=sharing_threshold))
    return "\n".join(parts) + "\n"


def build_catalog(
    info: dict,
    *,
    whitelist: set[tuple[str, str, str]] | None = None,
    cap: int = 50,
    capacity: int = DEFAULT_CAPACITY,
    sharing_threshold: float = 0.8,
    token_budget: int = _TOKEN_BUDGET,
    lru_state: dict,
) -> CatalogResult:
    """从 home_info + LRU + whitelist 构造目录 TSV。

    lru_state 必须由调用方传递（后端直接调用 ``LRUStore.load()``）。
    超过 token_budget 时按序列降级：capacity 7→5 → 设备数 50→30。
    """
    if whitelist is None:
        whitelist = load_whitelist()

    devices = info.get("devices", []) or []
    selected, overflow = select_devices(devices, whitelist, cap=cap)

    cur_capacity = capacity
    cur_cap = cap

    populated: list[DeviceCatalogEntry] = []
    empty: list[DeviceCatalogEntry] = []
    text = ""

    while True:
        populated, empty = _build_with_capacity(
            selected,
            whitelist=whitelist,
            capacity=cur_capacity,
            lru_state=lru_state,
        )
        text = _render_catalog(
            populated,
            sharing_threshold=sharing_threshold,
        )
        tokens = _estimate_tokens(text)
        if tokens <= token_budget:
            break
        if cur_capacity > 5:
            cur_capacity = 5
            continue
        if cur_cap > 30:
            cur_cap = 30
            selected, overflow = select_devices(devices, whitelist, cap=cur_cap)
            continue
        break

    return CatalogResult(
        text=text,
        selected_count=len(selected),
        overflow_count=len(overflow),
        empty_count=len(empty),
        capacity=cur_capacity,
        estimated_tokens=_estimate_tokens(text),
    )
