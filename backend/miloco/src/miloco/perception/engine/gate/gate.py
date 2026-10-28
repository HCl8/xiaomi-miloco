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
from miloco.perception.engine.gate.semantic_gate import (
    SemanticEvalResult,
    evaluate_semantic,
)
from miloco.perception.engine.gate.speech_vad import evaluate_speech
from miloco.perception.engine.gate.visual_gate import evaluate_visual
from miloco.perception.engine.types import (
    GatePacket,
    GateTiming,
    GateTrigger,
    InputSlice,
    SemanticGateContext,
)

logger = logging.getLogger(__name__)


async def run_gate(
    input_slice: InputSlice,
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray[np.uint8] | None = None,
    last_visual_pass_ts: float | None = None,
    last_audio_pass_ts: float | None = None,
    prev_raw_frame: NDArray[np.uint8] | None = None,
    semantic_context: SemanticGateContext | None = None,
    force_pass: bool = False,
) -> tuple[
    GatePacket | None,
    GateTiming,
    NDArray[np.uint8] | None,
    float | None,
    float | None,
]:
    """Run Gate layer.

    Returns ``(packet | None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts)``。

    Hold 滞回:visual 不通过 + 距上次 visual 通过 <= ``config.hold_duration_sec`` + **本窗
    有帧** 时放行,打 ``trigger.hold=True`` 保 video 路由。``timing.hold_pass`` 是不看帧的
    原始判定,与前者取值可能不同 —— 三处 hold 取值的对照见
    ``knowledge/03-features/perception-pipeline.md`` 的 Gate 小节。

    on-demand 单次调用路径不传两 ts(默认 None),hold 自然关闭。

    ``prev_raw_frame`` 是上窗最后一帧的原始彩色图,只给二级语义门禁当参考帧;不传则本窗
    跳过语义判定(首窗/cold-start)。``semantic_context`` 带本机位提炼出的特殊放行项
    （自定义任务 + 家庭档案经 LLM 提炼），sidecar 一次请求里逐条判 yes/no,任一命中即放行。
    语义结果在 ``mode=enforce`` 且未过阈值时把 ``visual_effective`` 置假,``mode=shadow``
    只记分不改变路由。

    ``force_pass`` 是状态容器的定时复核：有帧就绕过视觉/语义门禁直接放行给 omni，
    用于确认「一直在工作」这类门禁永远判为「无变化」的持续状态。
    """
    t = time.monotonic()
    visual = evaluate_visual(
        input_slice.frames, config, input_fps, prev_frame=prev_frame,
    )
    visual_changed = visual.changed
    visual_score = visual.max_score
    last_checked = visual.last_checked
    video_ms = (time.monotonic() - t) * 1000

    # 状态容器到点复核：绕过视觉/语义门禁直接给 omni（有帧才放）。复核的目的
    # 就是让 omni 重新看当前状态，本地门禁不能把它拦掉。
    forced = force_pass and bool(input_slice.frames)

    # 二级语义门禁：只在视觉放行时判（视觉没放行的窗本来就不会调 omni）。
    # 失败/熔断时 applied=False，visual_effective 保持原样 = fail-open。
    semantic = SemanticEvalResult()
    if config.semantic.enabled and visual_changed and input_slice.frames and not forced:
        semantic = await evaluate_semantic(
            input_slice.frames, prev_raw_frame, config.semantic, semantic_context
        )
    semantic_veto = (
        visual_changed
        and semantic.applied
        and config.semantic.mode == "enforce"
        and not semantic.passed
    )
    visual_effective = forced or (visual_changed and not semantic_veto)

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
    any_pass = visual_effective or audio_active
    hold_active = (
        not visual_effective
        and last_visual_pass_ts is not None
        and config.hold_duration_sec > 0
        and (now - last_visual_pass_ts) <= config.hold_duration_sec
    )
    # 无帧时 hold 不放行:零帧包会一路走到 omni 却拼不出 video 块,模型只能照 schema 编场景。
    hold_candidate = hold_active and bool(input_slice.frames)

    # hold 窗也要过一遍二级语义：视觉没变化时上面的 semantic 没跑，这里补判。
    # 被语义否决的 hold 不再开窗（不调 omni、不落视频），只留下 hold_pass 原始标记。
    if (
        config.semantic.enabled
        and config.semantic.hold_semantic_gate
        and hold_candidate
        and not visual_changed
        and input_slice.frames
    ):
        semantic = await evaluate_semantic(
            input_slice.frames, prev_raw_frame, config.semantic, semantic_context
        )
    hold_semantic_veto = (
        config.semantic.enabled
        and config.semantic.hold_semantic_gate
        and hold_candidate
        and semantic.applied
        and not semantic.passed
    )
    hold_opens_window = hold_candidate and not hold_semantic_veto

    new_last_visual_pass_ts = now if visual_effective else last_visual_pass_ts
    new_last_audio_pass_ts = now if audio_active else last_audio_pass_ts

    # 命中项写成 "下标:动作" 便于在 traces.timing_detail 里直接看到是哪个特殊放行项。
    semantic_pass_hits = ""
    if semantic_context is not None and semantic.pass_hits:
        semantic_pass_hits = ",".join(
            f"{i}:{semantic_context.special_items[i].action[:30]}"
            for i in semantic.pass_hits
            if i < len(semantic_context.special_items)
        )[:300]

    timing = GateTiming(
        video_ms=video_ms, audio_ms=audio_ms,
        video_pass=visual_changed, audio_pass=audio_active,
        video_effective_pass=visual_effective,
        video_score=visual_score, audio_energy=audio_energy,
        vad_ms=vad_ms, speech_prob=speech_prob,
        hold_pass=hold_active,
        hold_opened_window=hold_opens_window,
        hold_semantic_veto=hold_semantic_veto,
        video_intra_score=visual.intra_max,
        video_cross_score=visual.cross_max,
        checked_frame_count=visual.checked_frame_count,

        semantic_ms=semantic.latency_ms,
        semantic_applied=semantic.applied,
        semantic_pass=semantic.passed,
        semantic_score=semantic.score,
        semantic_veto=semantic_veto,
        semantic_error=semantic.error,
        semantic_pass_score=semantic.pass_score,
        semantic_pass_hits=semantic_pass_hits,
        semantic_pass_count=semantic.pass_count,
        semantic_question_count=semantic.question_count,
        # 补问的分数/结论要进 timing，否则 traces/dump/看板永远显示 0
        # （路由用的是 semantic.passed，本身已经包含 end/physio 的 OR）。
        semantic_end_score=semantic.end_score,
        semantic_end_pass=semantic.end_passed,
        semantic_physio_score=semantic.physio_score,
        semantic_physio_pass=semantic.physio_passed,
        recheck=forced,
    )

    if not any_pass and not hold_opens_window:
        return None, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts

    packet = GatePacket(
        packet_id=str(uuid.uuid4()),
        room_name=input_slice.room_name,
        timestamp=input_slice.end_timestamp,
        trigger=GateTrigger(
            visual_changed=visual_effective,
            visual_change_score=visual_score,
            audio_active=audio_active,
            audio_energy_level=audio_energy,
            speech_active=speech_active,
            hold=hold_opens_window,
            recheck=forced,
        ),
        frames=input_slice.frames,
        audio_clip=input_slice.audio_clip,
        sample_rate=input_slice.sample_rate,
        fps=input_fps,
    )
    return packet, timing, last_checked, new_last_visual_pass_ts, new_last_audio_pass_ts
