"""Gate Layer — LLM 提炼「特殊放行」判定项。

默认问题只判「相对参考帧有没有变化」，判不出持续状态/缺席/身份相关/细微状态。
这里用 omni 模型（text-only）读本机位规则 + 家庭档案，产出覆盖不到的动作，
逐条交给 StartLux 做 yes/no；后台提炼、按机位缓存，失败只退回默认问题。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from miloco.perception.engine.config import OmniConfig, SpecialPassConfig
from miloco.perception.engine.types import RuleCondition, SpecialPassItem
from miloco.utils.paths import miloco_home

logger = logging.getLogger(__name__)

_PROMPT_VERSION = "v2"
_CACHE_VERSION = 1

_SYSTEM_PROMPT = (
    "你是家庭感知系统的门禁配置助手。门禁已经有一个默认问题，判断「画面相对参考帧"
    "有没有变化」。你的任务是结合这个机位的自定义任务和家庭档案，找出默认问题覆盖"
    "不到、但必须特殊放行的画面条件，写成可以直接做 yes/no 判断的动作或状态描述。"
    "只输出 JSON。"
)

_USER_TEMPLATE = """【机位】<<ROOM>>
【机位须知】<<CAMERA_PROMPT>>
【默认问题】<<DEFAULT_QUESTION>>

【本机位生效的自定义任务】
<<RULES>>

【家庭档案】
<<PROFILE>>

判断口径：
- 默认问题「能覆盖」的，不要输出：一次性的可见变化，例如有人进入/离开、有人摔倒、拿起/放下物品、明显换动作、有人经过。画面会变，默认问题会放行。
- 默认问题「覆盖不到」的，才输出：
  1. 持续状态/时长：同一姿势持续、久坐不起、门或水龙头长时间保持打开；
  2. 缺席/空位：某个位置应该有人/物但不在、房间长时间无人；
  3. 依赖具体身份或身体条件：要结合家庭档案才知道该关注的动作；
  4. 细微局部状态：物件位置、设备指示灯、门开合、宠物抓咬家具等；
  5. 档案里明确写出的健康/安全风险、禁忌或约定、要求关注/记录的动作、宠物异常或破坏行为。

硬性要求：
- action 必须是画面可直接观察的动作/状态，用肯定句描述"要放行的画面"（如"厨房长时间无人"而不是"是否没人"）；8~40 个汉字；主体用档案/规则里的称呼，没有外观描述时用画面可判别的泛称（如"老人""小孩""猫"）；不写时间条件、概率、心理活动。
- 默认问题能覆盖的规则不要输出，放进 covered 说明原因；档案依据不需要 covered。
- 同一条件（同一条规则或同一档案依据）只输出一条；最多 <<MAX_ITEMS>> 条，按重要性排序；没有就输出空数组。
- source 必须写来源：规则来的填 rule:<rule_id>，档案来的填 profile。
- 每条规则都必须落到 items（默认问题覆盖不到）或 covered（默认问题能覆盖）里，不要遗漏；本机位没有自定义任务时 covered 输出空数组。
- 常规规则（例如"有人摔倒""有人进门""有人拿起杯子"）默认问题能覆盖，不要输出，在 covered 里说明。
- 只输出确实能从规则或档案推出的内容，不要编造。
<<EXTRA_BLOCK>>
输出 JSON（不要 markdown 代码块、不要解释）：
{"items":[{"action":"...","reason":"...","source":"rule:<rule_id> 或 profile"}],
 "covered":[{"rule_id":"...","reason":"默认问题能覆盖，原因..."}]}"""


@dataclass
class _CameraEntry:
    key: str
    generated_at: float
    model: str
    items: list[SpecialPassItem]
    covered: list[dict]


_ENTRIES: dict[str, _CameraEntry] = {}
_LOADED = False
_INFLIGHT: set[str] = set()
_RETRY_AT: dict[str, float] = {}
_TASKS: set[asyncio.Task] = set()
_PROFILE_CACHE: tuple[int, int, str] | None = None


# =============================================================================
# 对外入口
# =============================================================================


def prepare_camera(
    *,
    did: str,
    room_name: str | None,
    camera_prompt: str | None,
    rules: Sequence[RuleCondition],
    generic_instructions: str,
    config: SpecialPassConfig,
    omni_config: OmniConfig,
) -> list[SpecialPassItem]:
    """返回该机位当前可用的特殊放行项；需要重跑时只起后台任务、不阻塞本窗。"""
    if not config.enabled or config.max_items <= 0:
        return []
    try:
        profile = _profile_text()
        has_rules = any(r.query.strip() for r in rules)
        if not profile.strip() and not has_rules:
            return []
        live = _live_config(omni_config)
        if live is None:
            # 没配 omni key 时感知本身也跑不起来；这里静默跳过，不每窗刷 warning。
            return []
        _load_cache(config)
        key = _camera_key(
            profile=profile,
            room_name=room_name,
            camera_prompt=camera_prompt,
            rules=rules,
            generic_instructions=generic_instructions,
            model_name=live.model,
            max_items=config.max_items,
            max_action_chars=config.max_action_chars,
            extra_instructions=config.extra_instructions,
        )
        entry = _ENTRIES.get(did)
        now_wall = time.time()
        if entry is not None and entry.key == key:
            if now_wall - entry.generated_at >= config.refresh_after_hours * 3600:
                _spawn_refresh(
                    did=did,
                    key=key,
                    profile=profile,
                    room_name=room_name,
                    camera_prompt=camera_prompt,
                    rules=rules,
                    generic_instructions=generic_instructions,
                    config=config,
                    omni_config=omni_config,
                )
            return list(entry.items)
        # key 变化（档案/规则/机位改动）：旧项可能已对应不存在的规则，先不喂给门禁。
        _spawn_refresh(
            did=did,
            key=key,
            profile=profile,
            room_name=room_name,
            camera_prompt=camera_prompt,
            rules=rules,
            generic_instructions=generic_instructions,
            config=config,
            omni_config=omni_config,
        )
        return []
    except Exception:  # noqa: BLE001 — 提炼是增益项，任何异常都不能影响感知
        logger.warning("gate special-pass prepare failed did=%s", did, exc_info=True)
        return []


# =============================================================================
# 缓存
# =============================================================================


def _cache_path(config: SpecialPassConfig) -> str | os.PathLike:
    if config.cache_path:
        return os.path.expanduser(config.cache_path)
    return miloco_home() / "gate_special_pass.json"


def _load_cache(config: SpecialPassConfig) -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        raw = json.loads(Path(_cache_path(config)).read_text("utf-8"))
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 — 坏缓存按空处理，下次刷新会重写
        logger.warning(
            "gate special-pass cache unreadable; starting empty", exc_info=True
        )
        return
    cameras = raw.get("cameras") if isinstance(raw, dict) else None
    if not isinstance(cameras, dict):
        return
    for did, item in cameras.items():
        if not isinstance(item, dict):
            continue
        try:
            _ENTRIES[str(did)] = _CameraEntry(
                key=str(item.get("key") or ""),
                generated_at=float(item.get("generated_at") or 0),
                model=str(item.get("model") or ""),
                items=[
                    SpecialPassItem(
                        action=str(x["action"]),
                        reason=str(x.get("reason") or ""),
                        source=str(x.get("source") or ""),
                    )
                    for x in item.get("items") or []
                    if isinstance(x, dict) and x.get("action")
                ],
                covered=[x for x in item.get("covered") or [] if isinstance(x, dict)],
            )
        except Exception:  # noqa: BLE001 — 单条坏数据不废整份缓存
            logger.warning(
                "gate special-pass cache entry bad did=%s", did, exc_info=True
            )


def _write_cache(config: SpecialPassConfig) -> None:
    path = _cache_path(config)
    data = {
        "version": _CACHE_VERSION,
        "prompt_version": _PROMPT_VERSION,
        "cameras": {
            did: {
                "key": entry.key,
                "generated_at": entry.generated_at,
                "model": entry.model,
                "items": [
                    {"action": i.action, "reason": i.reason, "source": i.source}
                    for i in entry.items
                ],
                "covered": entry.covered,
            }
            for did, entry in _ENTRIES.items()
        },
    }
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — 缓存写失败不影响本轮结果
        logger.warning("gate special-pass cache write failed: %s", path, exc_info=True)


# =============================================================================
# 刷新（后台）
# =============================================================================


def _spawn_refresh(**kwargs) -> None:
    did = kwargs["did"]
    if did in _INFLIGHT:
        return
    if time.monotonic() < _RETRY_AT.get(did, 0.0):
        return
    try:
        task = asyncio.create_task(_refresh(**kwargs))
    except RuntimeError:  # 没有运行中的 loop（理论路径）：留给下一窗
        logger.debug("gate special-pass refresh skipped: no running loop")
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _refresh(
    *,
    did: str,
    key: str,
    profile: str,
    room_name: str | None,
    camera_prompt: str | None,
    rules: Sequence[RuleCondition],
    generic_instructions: str,
    config: SpecialPassConfig,
    omni_config: OmniConfig,
) -> None:
    _INFLIGHT.add(did)
    try:
        items, covered, model = await _extract(
            profile=profile,
            room_name=room_name,
            camera_prompt=camera_prompt,
            rules=rules,
            generic_instructions=generic_instructions,
            config=config,
            omni_config=omni_config,
        )
        _ENTRIES[did] = _CameraEntry(
            key=key,
            generated_at=time.time(),
            model=model,
            items=items,
            covered=covered,
        )
        _RETRY_AT.pop(did, None)
        _write_cache(config)
        logger.info(
            "event=gate_special_pass_refresh did=%s items=%d covered=%d model=%s",
            did,
            len(items),
            len(covered),
            model,
        )
        if covered:
            logger.info(
                "event=gate_special_pass_covered did=%s rule_ids=%s",
                did,
                ",".join(str(x.get("rule_id") or "") for x in covered),
            )
        for item in items:
            logger.info(
                "event=gate_special_pass_item did=%s source=%s action=%s",
                did,
                item.source,
                item.action,
            )
    except Exception as exc:  # noqa: BLE001
        _RETRY_AT[did] = time.monotonic() + config.retry_after_sec
        logger.warning(
            "gate special-pass refresh failed did=%s retry_in=%.0fs: %s: %s",
            did,
            config.retry_after_sec,
            type(exc).__name__,
            exc,
        )
    finally:
        _INFLIGHT.discard(did)


async def _extract(
    *,
    profile: str,
    room_name: str | None,
    camera_prompt: str | None,
    rules: Sequence[RuleCondition],
    generic_instructions: str,
    config: SpecialPassConfig,
    omni_config: OmniConfig,
) -> tuple[list[SpecialPassItem], list[dict], str]:
    from miloco.perception.engine.omni.omni_client import (
        call_omni,
        resolve_live_omni_config,
    )

    extra = config.extra_instructions.strip()
    extra_block = f"补充要求：\n{extra}\n\n" if extra else ""
    user_content = (
        _USER_TEMPLATE.replace("<<ROOM>>", (room_name or "（未知）").strip())
        .replace("<<CAMERA_PROMPT>>", (camera_prompt or "（无）").strip())
        .replace("<<DEFAULT_QUESTION>>", generic_instructions.strip())
        .replace("<<RULES>>", _render_rules(rules))
        .replace("<<EXTRA_BLOCK>>", extra_block)
        .replace("<<PROFILE>>", profile.strip() or "（无家庭档案）")
        .replace("<<MAX_ITEMS>>", str(max(1, config.max_items)))
    )
    live = resolve_live_omni_config(omni_config)
    # 全局约定：LLM 请求 max_tokens 一律给 10000；输出短，EOS 会提前停。
    live = replace(live, max_completion_tokens=10000, timeout=config.timeout_sec)
    raw = await call_omni(
        {"system_prompt": _SYSTEM_PROMPT, "user_content": user_content},
        live,
        # 感知循环驱动、非用户主动查询 → 用量归 realtime。
        type="realtime",
        # 辅助调用隔离：失败不打感知熔断，也不把提炼 prompt 塞进当前窗口 trace。
        use_circuit_breaker=False,
        record_trace=False,
    )
    content = _response_content(raw)
    if not content or not content.strip():
        raise ValueError("empty extraction response")
    items, covered = _parse_output(content, config)
    return items, covered, live.model


def _render_rules(rules: Sequence[RuleCondition]) -> str:
    lines: list[str] = []
    for rule in sorted(rules, key=lambda r: r.rule_id):
        query = _clean_text(rule.query)
        if not query:
            continue
        name = _clean_text(rule.rule_name)
        label = f"[{rule.rule_id}] {name}" if name else f"[{rule.rule_id}]"
        lines.append(f"- {label}：{query}")
    return "\n".join(lines) if lines else "（本机位没有自定义任务）"


def _response_content(raw: dict) -> str | None:
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 少数 provider 返回 content 数组
        return "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    return None


# =============================================================================
# 解析
# =============================================================================


def _parse_output(
    content: str, config: SpecialPassConfig
) -> tuple[list[SpecialPassItem], list[dict]]:
    obj = _loads_json(content)
    if isinstance(obj, list):
        raw_items, raw_covered = obj, []
    elif isinstance(obj, dict):
        raw_items = obj.get("items") or obj.get("actions") or []
        raw_covered = obj.get("covered") or []
    else:
        raise ValueError(f"extraction JSON has unexpected type {type(obj).__name__}")
    if not isinstance(raw_items, list):
        raise ValueError("items is not a list")

    items: list[SpecialPassItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        action = _clean_text(raw.get("action"))
        if not action:
            continue
        limit = max(1, config.max_action_chars)
        if len(action) > limit:
            cut = action[:limit]
            action = re.sub(r"[\s，。,.、；;：:]+$", "", cut) or cut.rstrip()
        norm = re.sub(r"[\s，。,.、；;：:！!？?]", "", action).casefold()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        source = _clean_text(raw.get("source"))
        if not (source.startswith("rule:") or source == "profile"):
            source = "profile"
        items.append(
            SpecialPassItem(
                action=action,
                reason=_clean_text(raw.get("reason"))[:200],
                source=source,
            )
        )
        if len(items) >= config.max_items:
            break

    covered = [
        {
            "rule_id": _clean_text(x.get("rule_id")),
            "reason": _clean_text(x.get("reason"))[:200],
        }
        for x in raw_covered
        if isinstance(x, dict)
    ]
    return items, covered


def _loads_json(content: str) -> object:
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    candidates = [text]
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError("extraction response is not valid JSON")


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


# =============================================================================
# key / profile
# =============================================================================


def _live_config(omni_config: OmniConfig) -> OmniConfig | None:
    """取当前生效的 omni 配置；没配 key 返回 None（提炼无法进行，静默跳过）。"""
    try:
        from miloco.perception.engine.omni.omni_client import (
            resolve_api_key,
            resolve_live_omni_config,
        )

        live = resolve_live_omni_config(omni_config)
        return live if resolve_api_key(live) else None
    except Exception:  # noqa: BLE001 — 读不到配置按不可用处理
        return None


def _camera_key(
    *,
    profile: str,
    room_name: str | None,
    camera_prompt: str | None,
    rules: Sequence[RuleCondition],
    generic_instructions: str,
    model_name: str,
    max_items: int,
    max_action_chars: int,
    extra_instructions: str,
) -> str:
    payload = {
        "prompt_version": _PROMPT_VERSION,
        "model": model_name,
        "profile": profile,
        "room": room_name or "",
        "camera_prompt": camera_prompt or "",
        "default_question": generic_instructions,
        "max_items": max_items,
        "max_action_chars": max_action_chars,
        "extra_instructions": extra_instructions,
        "rules": [
            [r.rule_id, r.rule_name, r.query]
            for r in sorted(rules, key=lambda r: r.rule_id)
        ],
    }
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _profile_text() -> str:
    """读一次 profile.md；mtime/size 不变则复用。读不到返回空串。"""
    global _PROFILE_CACHE
    from miloco.home_profile.store import profile_md_path

    try:
        stat = profile_md_path().stat()
    except OSError:
        _PROFILE_CACHE = None
        return ""
    if (
        _PROFILE_CACHE is not None
        and _PROFILE_CACHE[0] == stat.st_mtime_ns
        and _PROFILE_CACHE[1] == stat.st_size
    ):
        return _PROFILE_CACHE[2]
    from miloco.perception.engine.omni.home_profile_loader import (
        get_home_profile_prefix,
    )

    text = get_home_profile_prefix()
    _PROFILE_CACHE = (stat.st_mtime_ns, stat.st_size, text)
    return text
