"""Gate Layer — 一级门禁窗口留存（mp4 + meta，供离线分析）。

只保存在**视觉门禁放行**的窗口：这类窗口才是进入语义门禁 / omni 的候选。
写容器内暂存目录，宿主归档由同步脚本负责；超过 max_disk_gb 停写并告警，
绝不让留存把容器盘写满，也绝不因为留存失败影响感知链路。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateDumpConfig
from miloco.perception.engine.types import GateTiming

logger = logging.getLogger(__name__)

_TOTAL_CACHE: dict[str, int] = {}
_LAST_FULL_LOG = 0.0


def _root(config: GateDumpConfig) -> Path:
    if config.dir:
        return Path(config.dir).expanduser()
    home = os.environ.get("MILOCO_HOME") or "~/.openclaw/miloco"
    return Path(home).expanduser() / "gate_dumps"


def _total(root: Path) -> int:
    key = str(root)
    if key not in _TOTAL_CACHE:
        total = 0
        if root.exists():
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    pass
        _TOTAL_CACHE[key] = total
    return _TOTAL_CACHE[key]


def _slug(s: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]", "_", s or "")
    if slug.startswith("."):
        slug = "_" + slug.lstrip(".")
    return slug or "_"


def _encode_clip(
    frames: list[NDArray[np.uint8]],
    audio_clip: NDArray[np.int16],
    sample_rate: int,
    fps: int,
    short_edge: int,
):
    # 复用 omni 的编码器：同一套 h264/aac 参数，留存字节可直接回放。
    from miloco.perception.engine.omni.prompt_builder import _encode_video_mp4

    b64, media = _encode_video_mp4(
        frames, audio_clip, sample_rate, fps=fps, short_edge=short_edge
    )
    if not b64:
        return None, None
    return base64.b64decode(b64), media


def dump_gate_window(
    *,
    did: str,
    room_name: str,
    start_ms: float,
    end_ms: float,
    frames: list[NDArray[np.uint8]],
    prev_raw_frame: NDArray[np.uint8] | None,
    audio_clip: NDArray[np.int16],
    sample_rate: int,
    fps: int,
    timing: GateTiming,
    config: GateDumpConfig,
) -> bool:
    """保存一个过了一级门禁的窗口；任何失败只告警，不影响感知链路。"""
    if not config.enabled or not frames:
        return False

    root = _root(config)
    before = _total(root)
    if before >= int(config.max_disk_gb * 1024**3):
        global _LAST_FULL_LOG
        now = time.monotonic()
        if now - _LAST_FULL_LOG > 60:
            _LAST_FULL_LOG = now
            logger.warning(
                "gate dump cap reached (%.1f GB), skip did=%s",
                config.max_disk_gb,
                did,
            )
        return False

    day = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    out = root / day / _slug(did) / str(int(start_ms))
    out.mkdir(parents=True, exist_ok=True)
    added = 0
    try:
        if prev_raw_frame is not None and prev_raw_frame.size:
            ok, buf = cv2.imencode(
                ".jpg", prev_raw_frame, [cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            if ok:
                data = buf.tobytes()
                (out / "ref.jpg").write_bytes(data)
                added += len(data)

        clip = audio_clip if config.include_audio else np.zeros(0, dtype=np.int16)
        clip_bytes, media = _encode_clip(
            frames, clip, sample_rate, fps, config.short_edge
        )
        if clip_bytes:
            (out / "clip.mp4").write_bytes(clip_bytes)
            added += len(clip_bytes)

        meta = {
            "did": did,
            "room": room_name,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "fps": fps,
            "frame_count": len(frames),
            "video_pass": timing.video_pass,
            "video_effective_pass": timing.video_effective_pass,
            "video_score": timing.video_score,
            "video_intra_score": timing.video_intra_score,
            "video_cross_score": timing.video_cross_score,
            "semantic_applied": timing.semantic_applied,
            "semantic_pass": timing.semantic_pass,
            "semantic_score": timing.semantic_score,
            "semantic_veto": timing.semantic_veto,
            "semantic_ms": timing.semantic_ms,
            "hold_pass": timing.hold_pass,
            "hold_opened_window": timing.hold_opened_window,
            "audio_pass": timing.audio_pass,
            "audio_energy": timing.audio_energy,
            "clip_bytes": len(clip_bytes) if clip_bytes else 0,
            "media": {
                "width": media.video_width,
                "height": media.video_height,
                "has_audio": media.has_audio,
            }
            if media
            else None,
        }
        data = json.dumps(meta, ensure_ascii=False, indent=1).encode()
        (out / "meta.json").write_bytes(data)
        added += len(data)
    except Exception as e:  # noqa: BLE001 — 留存失败不能影响感知
        logger.warning(
            "gate dump failed did=%s start_ms=%s: %s", did, int(start_ms), e
        )
        return False

    _TOTAL_CACHE[str(root)] = before + added
    return True
