"""Gate Layer — Orchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.audio_gate import evaluate_audio
from miloco.perception.engine.gate.speech_vad import evaluate_speech
from miloco.perception.engine.gate.visual_gate import evaluate_visual
from miloco.perception.engine.types import (
    GatePacket,
    GateTiming,
    GateTrigger,
    InputSlice,
)

logger = logging.getLogger(__name__)


async def run_gate(
    input_slice: InputSlice,
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray[np.uint8] | None = None,
    last_visual_pass_ts: float | None = None,
    last_audio_pass_ts: float | None = None,
) -> tuple[
    GatePacket | None,
    GateTiming,
    NDArray[np.uint8] | None,
    float | None,
    float | None,
]:
    """Run Gate layer.

    Returns ``(packet | None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts)``。

    Hold 滞回:hold 资格只依赖 ``last_visual_pass_ts``。本窗 visual 不通过 + 距上次 visual
    通过 <= ``config.hold_duration_sec`` 时,即使 audio 也不通过也会生成 packet 并打
    ``trigger.hold=True``,下游 ``_is_audio_only`` 短路保 video 路由。

    on-demand 单次调用路径不传两 ts(默认 None),hold 自然关闭。
    """
    # 抽帧交给 DeviceSnapshot.get_frames_at_fps:它按**逐帧时间戳 + 真实 duration_ms**
    # 重采样(与 pipeline 给 omni 下采样用的是同一个抽象)。
    #
    # ⚠️ 不要退回「传 config.input.fps 让 evaluate_visual 自己算 interval」:那条路假设
    # `len(frames) ≈ input_fps × 窗长`,而生产上不成立——实测每 8s 窗有 160 帧(≈20fps),
    # 配置声明的 input.fps 却是 3,于是 interval=round(3/2)=2、抽出 80 帧 79 个帧对,而
    # check_fps=2 的本意是每 0.5s 一次(修复后实测正是 16 帧 16 对)。
    #
    # 真正的危害是**抽检间隔只有 0.05s**:相邻抽检帧几乎相同,慢速移动的人在一对帧之间只移动
    # 一两个像素、响应压根到不了阈值,窗内所有帧对都在做无用功——与「提高 check_fps 采样短
    # 事件」的意图正好相反。
    #
    # ⚠️ 修复**不会**变便宜,反而更贵(实测 95→125ms):过采样出来的近似相同帧对响应为 0,在
    # _evaluate_pair 的 no_structure 短路处就返回了,跳过 PCT/形态学/连通域;间隔拉回 0.5s 后
    # 每对都有真实变化、都要跑完整流程。同理它对误报的影响也不确定——近似相同的帧对产生不了
    # 紧凑斑块,所以「帧对多 5 倍 = 误报机会多 5 倍」这个推断是错的。修它的理由是**语义正确
    # 与对慢速运动的灵敏度**,不是省钱或降误报。
    #
    # 重采样后帧率**就是** check_fps,故 input_fps 传 check_fps —— evaluate_visual 内部
    # interval 算出 1、不再二次抽帧,其「frames 按 input_fps 到达」的契约这才成立。
    frames = input_slice.get_frames_at_fps(config.check_fps)

    # 丢进线程:结构判据(多尺度 PCT + 梯度响应 + 连通域)是几十毫秒量级的 CPU 活,
    # 原裸帧差只有几毫秒。留在事件循环里会阻塞同 batch 其他 device 的 IO——VAD 早先
    # 正是因为这个原因走了 to_thread(见下)。
    t = time.monotonic()
    visual = await asyncio.to_thread(
        evaluate_visual, frames, config, config.check_fps, prev_frame,
    )
    visual_changed = visual.changed
    visual_score = visual.max_score
    last_checked = visual.last_checked
    video_ms = (time.monotonic() - t) * 1000

    t = time.monotonic()
    audio_active, audio_energy = evaluate_audio(input_slice.audio_clip, config)
    audio_ms = (time.monotonic() - t) * 1000

    # 仅在音频过能量 gate 时跑 VAD：没过 gate 音频本就不喂、speeches 已被剥，无需判人声。
    # VAD 单独计时（vad_ms），不混进 audio_ms——否则 gate_audio_ms 会从亚毫秒跳到 ~数十 ms
    # 触发既有监控阈值。
    t = time.monotonic()
    speech_active, speech_prob = (
        await asyncio.to_thread(evaluate_speech, input_slice.audio_clip, config)
        if audio_active
        else (False, 0.0)
    )
    vad_ms = (time.monotonic() - t) * 1000

    now = time.monotonic()
    any_pass = visual_changed or audio_active
    hold_active = (
        not visual_changed
        and last_visual_pass_ts is not None
        and config.hold_duration_sec > 0
        and (now - last_visual_pass_ts) <= config.hold_duration_sec
    )

    new_last_visual_pass_ts = now if visual_changed else last_visual_pass_ts
    new_last_audio_pass_ts = now if audio_active else last_audio_pass_ts

    timing = GateTiming(
        video_ms=video_ms, audio_ms=audio_ms,
        video_pass=visual_changed, audio_pass=audio_active,
        video_score=visual_score, audio_energy=audio_energy,
        vad_ms=vad_ms, speech_prob=speech_prob,
        hold_pass=hold_active,
        video_intra_score=visual.intra_max,
        video_cross_score=visual.cross_max,
        video_verdict=visual.diag.verdict,
        video_mode=visual.diag.mode,
        video_diag={
            # 阈值基准 + 实际生效的响应阈:所有相对阈值都以 diff_scale 为尺,标定第一步
            # 就是看它的昼夜分布。noise_sigma 并列落库仅作诊断(它与 diff_scale 的差距就是
            # 首版参数被真机证伪的根因)。
            "diff_scale": visual.diag.diff_scale,
            "raw_scale": visual.diag.raw_scale,
            "struct_threshold": visual.diag.struct_threshold,
            "resp_p90": visual.diag.resp_p90,
            "resp_p99": visual.diag.resp_p99,
            "checked_frames": float(visual.diag.checked_frames),
            "pair_count": float(visual.diag.pair_count),
            "blk_x": float(visual.diag.max_block_x),
            "blk_y": float(visual.diag.max_block_y),
            "blk_w": float(visual.diag.max_block_w),
            "blk_h": float(visual.diag.max_block_h),
            "noise_sigma": visual.diag.noise_sigma,
            "pct_alpha": visual.diag.pct_alpha,
            "pct_beta": visual.diag.pct_beta,
            # 绝对灰度级量:阈值系数只能从这些的真实分布上定
            "pct_resid_abs": visual.diag.pct_resid_abs,
            "pct_texture_abs": visual.diag.pct_texture_abs,
            "pct_residual": visual.diag.pct_residual,
            "pct_ncc": visual.diag.pct_ncc,
            "pct_texture": visual.diag.pct_texture,
            "struct_ratio": visual.diag.struct_ratio,
            "light_block_ratio": visual.diag.light_block_ratio,
            "env_block_ratio": visual.diag.env_block_ratio,
            "env_duty_max": visual.diag.env_duty_max,
            "env_cov_max": visual.diag.env_cov_max,
            "block_count": float(visual.diag.block_count),
            "max_block_ratio": visual.diag.max_block_ratio,
            "max_block_fill": visual.diag.max_block_fill,
            "legacy_diff_score": visual.diag.legacy_diff_score,
        },
    )

    if not any_pass and not hold_active:
        return None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts

    packet = GatePacket(
        packet_id=str(uuid.uuid4()),
        room_name=input_slice.room_name,
        timestamp=input_slice.end_timestamp,
        trigger=GateTrigger(
            visual_changed=visual_changed,
            visual_change_score=visual_score,
            audio_active=audio_active,
            audio_energy_level=audio_energy,
            speech_active=speech_active,
            hold=hold_active,
        ),
        frames=input_slice.frames,
        audio_clip=input_slice.audio_clip,
        sample_rate=input_slice.sample_rate,
        fps=input_fps,
    )
    return packet, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts
