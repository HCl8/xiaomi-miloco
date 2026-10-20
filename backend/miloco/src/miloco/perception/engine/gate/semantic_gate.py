"""Gate Layer — 二级语义门禁（本地 StartLux-Decision sidecar）。

视觉门禁（帧差）只回答"物理上有没有变"，答不了"变的是不是值得记录的新事件"。
这里把参考帧 + 本窗抽样帧发给本地决策模型，取 P(有变化) 作为语义分。

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

logger = logging.getLogger(__name__)

_QUESTION_KEY = "event"
_DECISION_PATH = "/v1/systemone"

# 后端单事件循环：按 base_url 缓存客户端/并发闸/熔断状态即可。
_CLIENTS: dict[str, httpx.AsyncClient] = {}
_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_FAILURE_COUNT: dict[str, int] = {}
_FAILURE_UNTIL: dict[str, float] = {}


@dataclass(frozen=True)
class SemanticEvalResult:
    applied: bool = False  # 模型真的跑过（False = 未启用/无参考帧/熔断/失败）
    passed: bool = True  # 模型判定"有变化"且过阈值
    score: float = 0.0
    latency_ms: float = 0.0
    error: str = ""


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


def build_payload(
    prev_frame: NDArray[np.uint8],
    frames: list[NDArray[np.uint8]],
    config: SemanticGateConfig,
) -> dict:
    images = encode_images(prev_frame, frames, config)
    state = config.state.replace("{n}", str(len(images)))
    return {
        "state": state,
        "questions": {
            _QUESTION_KEY: {
                "type": "noul",
                "instructions": config.instructions,
                "criteria": {
                    "true": config.true_label,
                    "false": config.false_label,
                },
            }
        },
        "images": images,
    }


async def evaluate_semantic(
    frames: list[NDArray[np.uint8]],
    prev_frame: NDArray[np.uint8] | None,
    config: SemanticGateConfig,
) -> SemanticEvalResult:
    """调 sidecar 判本窗；任何失败都返回 applied=False（上层按放行处理）。"""
    if not config.enabled:
        return SemanticEvalResult()
    if prev_frame is None or not frames:
        # 首窗/cold-start 没有参考帧，语义判据不成立；视觉门禁此时本来就会放行。
        return SemanticEvalResult()

    t0 = time.monotonic()
    base_url = config.base_url.rstrip("/")
    if _in_cooldown(base_url, t0):
        return SemanticEvalResult(error="cooldown")

    try:
        payload = await asyncio.to_thread(build_payload, prev_frame, frames, config)
        async with _semaphore(base_url, config.max_concurrency):
            resp = await _client(base_url, config.timeout_sec).post(
                base_url + _DECISION_PATH, json=payload
            )
        resp.raise_for_status()
        score = float(resp.json()["answers"][_QUESTION_KEY]["noul"])
    except Exception as e:  # noqa: BLE001 — 门禁失败不能中断感知
        _record_failure(base_url, config, t0)
        latency_ms = (time.monotonic() - t0) * 1000
        logger.warning("semantic gate failed (%s): %s", type(e).__name__, e)
        return SemanticEvalResult(latency_ms=latency_ms, error=type(e).__name__)

    _record_success(base_url)
    return SemanticEvalResult(
        applied=True,
        passed=score >= config.min_score,
        score=score,
        latency_ms=(time.monotonic() - t0) * 1000,
    )
