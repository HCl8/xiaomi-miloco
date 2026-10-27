"""Gate Layer — 二级语义门禁（本地 StartLux-Decision sidecar）。

视觉门禁（帧差）只回答"物理上有没有变"，答不了"变的是不是值得记录的新事件"。
这里把参考帧 + 本窗抽样帧发给本地决策模型，取 P(有变化) 作为语义分。

除了默认的「相对参考帧有没有变化」问题，还会带上本机位经 LLM 提炼的「特殊放行」
问题（自定义任务 + 家庭档案）。特殊放行项也只描述**变化**（新出现/刚开始/刚切换），
和默认问题一样对比第 1 张参考帧；静态持续状态不参与，避免状态在就整段放行。
一次请求多问、任一过阈值即放行。

sidecar 是独立 MLX 服务，本模块只依赖 HTTP；调用失败一律 fail-open，由上层决定
shadow（只记分）还是 enforce（真否决）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import cv2
import httpx
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import SemanticGateConfig
from miloco.perception.engine.types import SemanticGateContext

logger = logging.getLogger(__name__)

_QUESTION_KEY = "event"
_END_KEY = "event_end"
_PHYSIO_KEY = "event_physio"
_PASS_KEY_PREFIX = "pass:"
_DECISION_PATH = "/v1/systemone"
_PASS_INSTRUCTIONS_SUFFIX = (
    "（只判断这个变化；描述里点名了主体/某人/宠物时，只看对应对象；"
    "相对第 1 张参考帧没有出现这个变化就选否。）"
)

# 后端单事件循环：按 base_url 缓存客户端/并发闸/熔断状态即可。
_CLIENTS: dict[str, httpx.AsyncClient] = {}
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_FAILURE_COUNT: dict[str, int] = {}
_FAILURE_UNTIL: dict[str, float] = {}


@dataclass(frozen=True)
class SemanticEvalResult:
    applied: bool = False  # 模型真的跑过（False = 未启用/无参考帧/熔断/失败）
    passed: bool = True  # 默认问题或任一特殊放行问题过阈值
    score: float = 0.0  # 默认问题（相对参考帧有没有变化）的分数
    latency_ms: float = 0.0
    error: str = ""
    # 「结束/离开」补问的分数与是否命中（默认问题对消失/结束不敏感）。
    end_score: float = 0.0
    end_passed: bool = False
    # 生理关怀补问（喝水/吃东西/打哈欠等）：过阈值时状态容器不跳 omni。
    physio_score: float = 0.0
    physio_passed: bool = False
    # 特殊放行问题（规则/档案经 LLM 提炼）：最高分、命中下标、问了几条、总题数。
    pass_score: float = 0.0
    pass_hits: tuple[int, ...] = ()
    pass_count: int = 0
    question_count: int = 0


def select_frames(frames: list[NDArray[np.uint8]], n: int) -> list[NDArray[np.uint8]]:
    """从本窗等距抽 n 帧。含首帧，近似实验里的 1fps 采样。"""
    if not frames or n <= 0:
        return []
    if len(frames) <= n:
        return list(frames)
    step = len(frames) / n
    return [frames[min(len(frames) - 1, int(i * step))] for i in range(n)]


def _resize_short_edge(frame: NDArray[np.uint8], short_edge: int) -> NDArray[np.uint8]:
    if short_edge <= 0 or frame.size == 0:
        return frame
    h, w = frame.shape[:2]
    if min(h, w) <= short_edge:
        return frame
    scale = short_edge / min(h, w)
    return cv2.resize(
        frame,
        (max(1, round(w * scale)), max(1, round(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def prepare_reference_frame(
    frame: NDArray[np.uint8] | None, short_edge: int
) -> NDArray[np.uint8] | None:
    """跨窗基准帧：提前缩放，避免每设备长期占一份全分辨率帧。"""
    if frame is None or frame.size == 0:
        return None
    return _resize_short_edge(frame, short_edge).copy()


def encode_images(
    prev_frame: NDArray[np.uint8],
    frames: list[NDArray[np.uint8]],
    config: SemanticGateConfig,
) -> list[str]:
    images: list[str] = [prev_frame]
    images.extend(select_frames(frames, config.frames))
    out: list[str] = []
    for img in images:
        img = _resize_short_edge(img, config.image_short_edge)
        ok, buf = cv2.imencode(
            ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(config.jpeg_quality)]
        )
        if not ok:
            raise ValueError("jpeg encode failed")
        out.append(
            "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        )
    return out


def _client(base_url: str, timeout_sec: float) -> httpx.AsyncClient:
    client = _CLIENTS.get(base_url)
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec))
        _CLIENTS[base_url] = client
    return client


def _semaphore(base_url: str, limit: int) -> asyncio.Semaphore:
    sem = _SEMAPHORES.get(base_url)
    if sem is None:
        sem = asyncio.Semaphore(max(1, limit))
        _SEMAPHORES[base_url] = sem
    return sem


def _in_cooldown(base_url: str, now: float) -> bool:
    return _FAILURE_UNTIL.get(base_url, 0.0) > now


def _record_failure(base_url: str, config: SemanticGateConfig, now: float) -> None:
    count = _FAILURE_COUNT.get(base_url, 0) + 1
    if count >= max(1, config.failure_threshold):
        _FAILURE_UNTIL[base_url] = now + config.failure_cooldown_sec
        _FAILURE_COUNT[base_url] = 0
        logger.warning(
            "semantic gate circuit open for %.0fs after %d failures: %s",
            config.failure_cooldown_sec,
            count,
            base_url,
        )
    else:
        _FAILURE_COUNT[base_url] = count


def _record_success(base_url: str) -> None:
    _FAILURE_COUNT.pop(base_url, None)
    _FAILURE_UNTIL.pop(base_url, None)


def _render_context_block(context: SemanticGateContext | None) -> str:
    """机位/时间/机位须知。家庭档案原文不在这里——它只喂给 LLM 提炼特殊放行项。"""
    if context is None:
        return ""
    parts: list[str] = []
    head: list[str] = []
    if context.room_name:
        head.append(f"【机位】{context.room_name}")
    if context.current_time:
        head.append(f"当前时间：{context.current_time}")
    if head:
        parts.append("；".join(head))
    if context.camera_prompt:
        parts.append(f"【机位须知】{context.camera_prompt.strip()}")
    if context.special_items:
        parts.append(
            "（特殊放行问题同样以第 1 张为参考帧；只判相对参考帧的变化；"
            "描述里点名了对象时只看该对象。）"
        )
    return "\n".join(parts)


def _special_questions(
    context: SemanticGateContext | None,
) -> dict[str, dict]:
    if context is None:
        return {}
    questions: dict[str, dict] = {}
    for i, item in enumerate(context.special_items):
        action = item.action.strip()
        if not action:
            continue
        questions[f"{_PASS_KEY_PREFIX}{i}"] = {
            "type": "noul",
            "instructions": (
                "第 1 张是参考帧，第 2 张起是本窗口。"
                f"相对参考帧，本窗口里是否出现了以下变化：{action}？"
                f"{_PASS_INSTRUCTIONS_SUFFIX}"
            ),
            "criteria": {"true": "出现了这个变化", "false": "没有出现这个变化"},
        }
    return questions


def build_payload(
    prev_frame: NDArray[np.uint8],
    frames: list[NDArray[np.uint8]],
    config: SemanticGateConfig,
    context: SemanticGateContext | None = None,
) -> dict:
    images = encode_images(prev_frame, frames, config)
    state = config.state.replace("{n}", str(len(images)))
    context_block = _render_context_block(context)
    if context_block:
        state = f"{state}\n\n{context_block}"
    questions = {
        _QUESTION_KEY: {
            "type": "noul",
            "instructions": config.instructions,
            "criteria": {
                "true": config.true_label,
                "false": config.false_label,
            },
        }
    }
    if config.end_enabled:
        questions[_END_KEY] = {
            "type": "noul",
            "instructions": config.end_instructions,
            "criteria": {"true": "消失/结束/离开", "false": "没有"},
        }
    if config.physio_enabled:
        questions[_PHYSIO_KEY] = {
            "type": "noul",
            "instructions": config.physio_instructions,
            "criteria": {"true": "有生理动作", "false": "没有"},
        }
    questions.update(_special_questions(context))
    return {"state": state, "questions": questions, "images": images}


async def evaluate_semantic(
    frames: list[NDArray[np.uint8]],
    prev_frame: NDArray[np.uint8] | None,
    config: SemanticGateConfig,
    context: SemanticGateContext | None = None,
) -> SemanticEvalResult:
    """调 sidecar 判本窗；任何失败都返回 applied=False（上层按放行处理）。

    一次请求里同时问：默认问题（相对参考帧有没有变化）+ 若干特殊放行问题
    （规则/档案经 LLM 提炼）。任一过阈值即 passed，特殊放行项救了被默认问题误拦的窗。
    """
    if not config.enabled:
        return SemanticEvalResult()
    if prev_frame is None or not frames:
        # 首窗/cold-start 没有参考帧，语义判据不成立；视觉门禁此时本来就会放行。
        return SemanticEvalResult()

    t0 = time.monotonic()
    base_url = config.base_url.rstrip("/")
    if _in_cooldown(base_url, t0):
        return SemanticEvalResult(error="cooldown")

    special_items = context.special_items if context is not None else ()
    question_count = (
        1
        + int(config.end_enabled)
        + int(config.physio_enabled)
        + len(special_items)
    )

    try:
        payload = await asyncio.to_thread(
            build_payload, prev_frame, frames, config, context
        )
        async with _semaphore(base_url, config.max_concurrency):
            resp = await _client(base_url, config.timeout_sec).post(
                base_url + _DECISION_PATH, json=payload
            )
        resp.raise_for_status()
        answers = resp.json()["answers"]
        score = float(answers[_QUESTION_KEY]["noul"])
        end_score = 0.0
        if config.end_enabled:
            end_answer = answers.get(_END_KEY)
            if isinstance(end_answer, dict) and "noul" in end_answer:
                end_score = float(end_answer["noul"])
        end_passed = config.end_enabled and end_score >= config.end_min_score
        physio_score = 0.0
        if config.physio_enabled:
            physio_answer = answers.get(_PHYSIO_KEY)
            if isinstance(physio_answer, dict) and "noul" in physio_answer:
                physio_score = float(physio_answer["noul"])
        physio_passed = (
            config.physio_enabled and physio_score >= config.physio_min_score
        )
        pass_scores: list[float] = []
        pass_hits: list[int] = []
        for i in range(len(special_items)):
            answer = answers.get(f"{_PASS_KEY_PREFIX}{i}")
            if not isinstance(answer, dict) or "noul" not in answer:
                continue
            value = float(answer["noul"])
            pass_scores.append(value)
            if value >= config.extra_min_score:
                pass_hits.append(i)
    except Exception as e:  # noqa: BLE001 — 门禁失败不能中断感知
        _record_failure(base_url, config, t0)
        latency_ms = (time.monotonic() - t0) * 1000
        logger.warning("semantic gate failed (%s): %s", type(e).__name__, e)
        return SemanticEvalResult(
            latency_ms=latency_ms,
            error=type(e).__name__,
            pass_count=len(special_items),
            question_count=question_count,
        )

    _record_success(base_url)
    return SemanticEvalResult(
        applied=True,
        passed=(
            score >= config.min_score
            or end_passed
            or physio_passed
            or bool(pass_hits)
        ),
        score=score,
        latency_ms=(time.monotonic() - t0) * 1000,
        end_score=end_score,
        end_passed=end_passed,
        physio_score=physio_score,
        physio_passed=physio_passed,
        pass_score=max(pass_scores) if pass_scores else 0.0,
        pass_hits=tuple(pass_hits),
        pass_count=len(special_items),
        question_count=question_count,
    )
