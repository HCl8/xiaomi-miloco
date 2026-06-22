"""Spec utility functions shared by CLI device commands.

catalog build/render 逻辑已迁移到 backend ``miloco.miot.catalog_render``。
本文件仅保留 CLI ``device list`` / ``device spec`` / ``device control`` 等
命令共用的工具函数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ─── 数据结构 ─────────────────────────────────────────────────────────────────


@dataclass
class SpecLine:
    """一行 spec（prop 或 action）渲染所需的最小元数据。"""

    key: str  # 含可能的 @ 后缀
    fmt: str  # prop: bool/uint8/.. 等值类型；action: 空串（无值类型）
    wr: str  # access 字段，prop ∈ {wr, w, r}；action 恒为 ``x``（execute）
    extra: str  # prop: constraint（范围/枚举）；action: in_params 入参列表；无则空
    unit: str  # 仅 prop 行；无单位为空
    is_action: bool
    annotation: str = ""  # 可选展示注释，渲染为行尾 ``  # annotation``，与 key 物理隔离

    def render(self) -> str:
        """渲染为单行字符串。

        prop 行：   name|access|format|constraint|unit  [# 注释]
        action 行： name|access|in_params               [# 注释]   （access 恒为 'x'）
        """
        if self.is_action:
            if self.extra:
                line = f"{self.key}|{self.wr}|{self.extra}"
            else:
                line = f"{self.key}|{self.wr}"
        else:
            # prop: access 在前，format 在后，与 action 的 access 列对齐
            parts = [self.key, self.wr, self.fmt]
            if self.extra or self.unit:
                parts.append(self.extra)  # 无约束时为空串，产出 ||unit
            if self.unit:
                parts.append(self.unit)
            line = "|".join(parts)
        if self.annotation:
            line = f"{line}  # {self.annotation}"
        return line


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


# 仅替换 TSV / catalog 解析层会冲突的字符；中文 / 其它 Unicode 字符直接保留，
# 这样米家中文 description（"左键" / "中键" / "指示灯"）能进 ``@desc`` 后缀。
# 替换为 ``_`` 而不是 strip，保留单词边界（"Switch 1" → "Switch_1"，与原行为兼容）。
_DESC_FORBID_RE = re.compile(r"[\s|,:=@]+")


def normalize_desc(desc: str | None) -> str:
    if not desc:
        return ""
    s = _DESC_FORBID_RE.sub("_", desc.strip())
    return s.strip("_")


def _escape(value: str | None) -> str:
    """转义设备行字段中的 ``|``（避免破坏 TSV）。"""
    if value is None:
        return ""
    return str(value).replace("|", r"\|")


def _format_extra(entry: dict) -> str:
    """value_range / value_list / in_params → extra 字符串。

    - 数值范围：``[min,max;step]``，无 step 时退化为 ``[min,max]``
    - 枚举：``Name1=Val1,Name2=Val2,..``（保留 spec 原序）
    - action 入参：``name1:fmt,name2:fmt,..``
    其它情况返回空字符串。
    """
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
    """整数不带小数点；浮点保留原值；其它原样转字符串。"""
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if n.is_integer():
            return str(int(n))
        return repr(n).rstrip("0").rstrip(".") or "0"
    return str(n)


# ─── 设备 spec 消歧 / key 解析 ───────────────────────────────────────────────


def _resolve_keys_for_device(spec: dict) -> dict[str, str]:
    """同设备内同 type_name 冲突 → 按 §2.1 优先级生成消歧后的 key。

    返回 ``{iid: key}``。
    """
    if not isinstance(spec, dict):
        return {}

    # 第一遍：统计 type_name 出现次数
    counts: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name")
        if type_name:
            counts[type_name] = counts.get(type_name, 0) + 1

    # 第二遍：分配 key，同时按完整 ``type_name@desc`` 计数
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

    # 第三遍：``type_name@desc`` 在设备内仍冲突（多 entry 归一化后同 desc）→ 退化为 raw iid
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
    """为 catalog / device spec 输出生成 ``(注释)`` 后缀。

    - 裸 type_name（无冲突）：不加注释，type_name 自解释
    - type_name@service_desc：加 ``(type_desc)``，即属性自身描述（去掉
      service_desc 前缀，如"开关"）——agent 截断到 ``@`` 之前的 ``(`` 即可
    - raw iid（desc 冲突退化）：加 ``(service_desc type_desc)`` 完整描述，
      因为 iid 本身不带语义
    """
    desc = str(entry.get("description") or "")
    svc_desc = str(entry.get("service_description") or "")

    if key == iid:
        # raw iid 退化：完整描述（iid 本身不透明，必须注释）
        return _clean_annotation(desc) if desc else ""

    if "@" in key:
        type_name = key.split("@")[0]
    else:
        type_name = key

    # 用英文 prop_description 跟 type_name 比较：
    # 一致（如 mode == "Mode"）→ 不加注释；不一致 → 加中文 desc 辅助理解。
    prop_desc_en = str(entry.get("prop_description") or "")
    normalized_en = prop_desc_en.strip().lower().replace(" ", "-").replace("_", "-")
    if normalized_en == type_name:
        return ""

    # 英文不一致 → 尝试抠中文注释
    if svc_desc and desc.startswith(svc_desc):
        type_desc = desc[len(svc_desc):].strip()
        if type_desc:
            return _clean_annotation(type_desc)

    # 中文 type_desc 为空（desc == svc_desc 或子设备自定义名覆盖）→ 跳过
    return ""


_MAX_ANNOTATION_LEN = 20


def _clean_annotation(s: str) -> str:
    s = s.replace("|", "").replace("(", "").replace(")", "")
    if len(s) > _MAX_ANNOTATION_LEN:
        s = s[:_MAX_ANNOTATION_LEN] + "…"
    return s
