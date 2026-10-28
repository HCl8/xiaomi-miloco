"""Gate Layer — LLM 提炼「特殊放行」判定项。

常规活动（喝水/打哈欠/看手机/起身/离开/回到/坐下工作）由默认问题直接召回；
这里用 omni 模型（text-only）读本机位规则 + 家庭档案，只提炼默认问题覆盖不到的
**变化时刻**（身份/身体条件、细微局部、宠物异常、健康安全禁忌等），逐条交给
StartLux 做 yes/no。
提炼时把门禁的公共输入布局一并给出，保证写出的描述都是「连续画面之间」可判的；
静态/持续状态不进特殊放行（否则同一状态会在每个窗都放行）。
后台提炼、按机位缓存，失败只退回默认问题。
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

_PROMPT_VERSION = "v8"
_CACHE_VERSION = 1

# 门禁公共输入布局的兜底文案；正常情况下由调用方把 config.state 渲染好后传入。
_FALLBACK_INPUT_LAYOUT = (
    "画面按时间先后排列，是一组连续画面（每秒 1 张）；任意两张画面之间的变化都算。"
)

_SYSTEM_PROMPT = (
    "你是家庭感知系统的门禁提示词助手。门禁的输入是一组连续的监控画面（每秒 1 张），"
    "默认问题判断这组画面里有没有发生变化。你的任务是结合机位任务和家庭档案，找出"
    "默认问题可能漏掉、但值得放行的**变化时刻**，写成可以直接做 yes/no 判断的"
    "变化描述（任意两张画面之间出现即可）。不要写静态场景或持续状态。只输出 JSON。"
)

_USER_TEMPLATE = """【输入数据】<<INPUT_LAYOUT>>
【默认问题】<<DEFAULT_QUESTION>>
【机位】<<ROOM>>
【机位须知】<<CAMERA_PROMPT>>

【本机位生效的自定义任务】
<<RULES>>

【家庭档案】
<<PROFILE>>

核心口径——只找「变化时刻」，不找「持续状态」：
- 每条 action 都必须是这组连续画面里能判定的变化（任意两张画面之间出现即可）：新出现、刚发生、刚开始、刚结束、刚切换、刚拿起/放下、刚进入/离开、位置/状态刚改变。
- 禁止输出静态或持续状态，例如"有人在用电脑/工作""有人拿着手机看""宝宝在爬行垫上""有人坐在沙发上"。这些状态可能在很多窗里都成立，会把门禁变成"只要状态在就放行"。
- 规则/档案关心的是持续状态时，只提炼它开始/结束/切换的那一刻。

常规活动默认问题已经明确覆盖，不要输出：
- 喝水/吃东西、打哈欠、看手机、起身/站立、离开/回到画面、坐下开始工作、进门/摔倒等，
  默认问题（活动类别变化）会召回；这些规则一律放 covered，不要写成特殊项。
- 规则里说的是"有人/人"时，用"主体"指代；只有规则/档案明确要求关注其他人/宠物/背景时才用对应泛称。
- 特殊项只留给默认问题覆盖不到的：依赖具体身份/身体条件、细微局部、宠物异常、健康/安全禁忌、档案约定等。
- 没有明显转折的持续状态不要硬编转折；确实没有变化就放 covered。

默认问题覆盖不到、但仍属于「变化」的也输出：
1. 非主体的人/物发生的变化：只有规则/档案明确要求关注其他人/宠物/背景时才输出，否则不要写背景路人；
2. 细微/局部变化：小物件刚被移动、门刚被打开、灯刚亮/灭、宠物刚开始抓咬；
3. 依赖具体身份/身体条件的转折时刻：结合档案才知道要关注的变化（如老人刚从站立变为扶墙）；
4. 档案写明的健康/安全风险、禁忌或要求关注的动作，取其发生/开始的瞬间。

示例：
- 好（变化）：「宝宝刚开始爬行」「老人刚从站立变为扶墙」「猫刚开始抓沙发」
- 坏（静态）：「有人在工作/用电脑」「有人拿着手机」「宝宝在爬行垫上」「客厅里有人」

硬性要求：
- action 用变化句式（刚…／开始…／从…变为…／新出现…／刚离开…），8~40 个汉字；主体用档案/规则里的称呼，没有外观描述时用画面可判别的泛称；不写时间条件、概率、心理活动。
- 每条 action 都必须能在这组连续画面之间判断（任意两张画面之间的变化）；只靠单帧静态内容就能成立的描述不合格。
- 每条 item 必须带 title：给看板展示用的短标题，不超过 8 个汉字，概括这条变化
  （例如「老人扶墙」「宝宝爬行」「宠物抓沙发」「拿起手机」），不要复述整句。
- 常规活动（喝水/打哈欠/看手机/起身/离开/回到/坐下工作）和大幅变化放 covered 说明原因；只有真正覆盖不到的变化才落到 items。
- 同一条件（同一条规则或同一档案依据）只输出一条；最多 <<MAX_ITEMS>> 条，按重要性排序；没有就输出空数组。
- source 必须写来源：规则来的填 rule:<rule_id>，档案来的填 profile。
- 每条规则都必须落到 items（动作/细微变化）或 covered（大幅/持续状态）里，不要遗漏；本机位没有自定义任务时 covered 输出空数组。
- 只输出确实能从规则或档案推出的内容，不要编造。
<<EXTRA_BLOCK>>
输出 JSON（不要 markdown 代码块、不要解释）：
{"items":[{"title":"...","action":"...","reason":"...","source":"rule:<rule_id> 或 profile"}],
 "covered":[{"rule_id":"...","reason":"默认问题能覆盖，或只是持续状态..."}]}"""


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


def _auto_title(action: str) -> str:
    """从动作描述里取一个短标题（≤8 字）：去掉「主体」和标点后的首个短语。"""
    text = re.sub(r"^主体", "", action.strip())
    text = re.split(r"[，。；,;：:（(]", text, maxsplit=1)[0].strip()
    return text[:8] or action[:8]


def _physical_did(did: str) -> str:
    """合成通道 did → 物理 did（``cam1:ch0`` → ``cam1``）。"""
    return did.rsplit(":ch", 1)[0] if ":ch" in did else did


def _forced_items(config: SpecialPassConfig, did: str = "") -> list[SpecialPassItem]:
    """配置里写死的特殊放行项；不依赖 LLM，顺序在提炼项之前。

    按机位配置同时按合成通道 did 和物理 did 查：配置里写的是相机 did，而感知侧
    传进来可能是 ``did:ch0``，只查一种会让整机位的强制项静默失效。
    """
    out: list[SpecialPassItem] = []
    by_cam = config.force_items_by_camera or {}
    per_cam: list = []
    for key in {did, _physical_did(did)}:
        if key:
            per_cam.extend(by_cam.get(key, ()))
    raw_items = list(config.force_items or ())
    raw_items += per_cam
    for raw in raw_items:
        text = (raw or "").strip()
        if not text:
            continue
        # 支持 "标题=描述"：标题给看板，描述给 StartLux；没有 "=" 就自动取标题。
        head, sep, tail = text.partition("=")
        title = head.strip() if sep else ""
        action = (tail if sep else head).strip()[: config.max_action_chars]
        if not action:
            continue
        out.append(
            SpecialPassItem(
                action=action,
                title=(title or _auto_title(action))[:12],
                reason="config.force_items",
            )
        )
    return out[: config.max_items]


def _merge_forced(
    items: list[SpecialPassItem],
    forced: list[SpecialPassItem],
    config: SpecialPassConfig,
) -> list[SpecialPassItem]:
    if not forced:
        return items[: config.max_items]
    seen = {i.action for i in forced}
    return (forced + [i for i in items if i.action not in seen])[: config.max_items]


def prepare_camera(
    *,
    did: str,
    room_name: str | None,
    camera_prompt: str | None,
    rules: Sequence[RuleCondition],
    generic_instructions: str,
    config: SpecialPassConfig,
    omni_config: OmniConfig,
    input_layout: str = "",
) -> list[SpecialPassItem]:
    """返回该机位当前可用的特殊放行项；需要重跑时只起后台任务、不阻塞本窗。

    ``input_layout`` 是门禁的公共输入说明（参考帧 + 本窗口各帧），会随提炼提示词
    一起给 LLM，确保它写出的是「相对参考帧」可判的变化；也进缓存 key，改了会重提炼。
    """
    input_layout = (input_layout or "").strip() or _FALLBACK_INPUT_LAYOUT
    if not config.enabled or config.max_items <= 0:
        return []
    forced = _forced_items(config, did)
    try:
        profile = _profile_text()
        has_rules = any(r.query.strip() for r in rules)
        if not profile.strip() and not has_rules:
            # 没有规则/档案时 LLM 提炼无事可做，但配置写死的项仍然生效。
            return list(forced)
        live = _live_config(omni_config)
        if live is None:
            # 没配 omni key 时感知本身也跑不起来；这里静默跳过，不每窗刷 warning。
            return list(forced)
        _load_cache(config)
        key = _camera_key(
            profile=profile,
            room_name=room_name,
            camera_prompt=camera_prompt,
            rules=rules,
            generic_instructions=generic_instructions,
            input_layout=input_layout,
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
                    input_layout=input_layout,
                    config=config,
                    omni_config=omni_config,
                )
            return _merge_forced(list(entry.items), forced, config)
        # key 变化（档案/规则/机位改动）：旧项可能已对应不存在的规则，先不喂给门禁。
        _spawn_refresh(
            did=did,
            key=key,
            profile=profile,
            room_name=room_name,
            camera_prompt=camera_prompt,
            rules=rules,
            generic_instructions=generic_instructions,
            input_layout=input_layout,
            config=config,
            omni_config=omni_config,
        )
        return list(forced)
    except Exception:  # noqa: BLE001 — 提炼是增益项，任何异常都不能影响感知
        logger.warning("gate special-pass prepare failed did=%s", did, exc_info=True)
        return list(forced)


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
                        title=str(x.get("title") or ""),
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
                    {
                        "action": i.action,
                        "title": i.title,
                        "reason": i.reason,
                        "source": i.source,
                    }
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
    input_layout: str,
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
            input_layout=input_layout,
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
    input_layout: str = "",
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
        .replace(
            "<<INPUT_LAYOUT>>",
            (input_layout or "").strip() or _FALLBACK_INPUT_LAYOUT,
        )
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
        title = _clean_text(raw.get("title"))[:12] or _auto_title(action)
        items.append(
            SpecialPassItem(
                action=action,
                title=title,
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
    input_layout: str = "",
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
        "input_layout": input_layout,
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
