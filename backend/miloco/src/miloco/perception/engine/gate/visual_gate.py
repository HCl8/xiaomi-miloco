"""Gate Layer — Visual Gate（结构判据 + 多尺度光度一致性测试）。

判「画面有没有真实变动」。**不是**「变了多少像素」——裸帧差把光照变化当变动（误报）、
把小面积动作漏掉（漏报),根因是只看变化量、不看变化的空间/结构分布,也不看是否物理真实。

本模块实现方案 v1 阶段(Tolaria ``miloco/gate-视觉门禁改造方案-真实变动-vs-光变``):

    帧对 → PCT@全帧(滤漫射光变) → 结构响应(梯度方向/幅值,光照不变量)
         → PCT@分块(滤半房照亮等局部光变) → 连通域 + 紧凑度 + 最小面积 → 候选块
    非空候选 = 触发

**一套判据、三个尺度**:光变判据统一为 PCT(见 ``photometric.py``),跑在全帧 / 分块 /
候选区。初稿把它拆成「全帧漂移门 + 候选区阴影鉴别器」两套,中间尺度成了盲区(半房间被
照亮),且漂移门单独使用了一个被判定为不可靠、必须由纹理判据当门槛的判据——却放在
「判错即丢整帧」这个代价最高的位置上。统一后两个问题一起消失。v1 只到分块尺度;候选区
尺度(移动阴影/光束)是 v2,有实验门 E2,未实现。

**昼夜双档**:成本结构是分时段的。白天「优先不误拦、可以误放」→ 阈值取放行侧;夜间 IR
下 PCT 与结构响应同时退化、空窗占绝大多数、误报量级压不住 → **反过来**提高阈值拦住大
多数窗口,接受漏报上升。故参数按 ``day`` / ``night`` 两档组织,由 ``sat_mean`` + 滞回判定。

**跨窗帧对(cross)无需特殊处理**:它的时间差 ≈ 一整个窗口(数秒),而 PCT 的乘性测试在
秒级间隔上恰恰是 AGC/IR 长周期漂移的主场,方向 NCC 对静态场景同样成立。只有光流(v1 不
含)才依赖小位移。

**成本**:每帧 ~5 个 cv2/numpy 数组运算,每帧对 ~8 个,448×448 上单窗合计数十毫秒量级。
调用方(``gate.run_gate``)已把本函数丢进线程,不占事件循环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple

import cv2
import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate.photometric import (
    FrameFeat,
    PctResult,
    PctThresholds,
    additive_offset_test,
    frame_features,
    frame_moments,
    noise_sigma,
    orientation_agreement,
    pct_blocks,
    pct_frame,
    response,
    saturation_mean,
    temporal_scale,
)

logger = logging.getLogger(__name__)

_DIFF_SIZE = (448, 448)
# 调试落盘计数(进程内),受 GateConfig.gate_debug_dump_max 限制。
_dump_count = 0
_MASK_TOTAL_PX = _DIFF_SIZE[0] * _DIFF_SIZE[1]

MODE_DAY = "day"
MODE_NIGHT = "night"

# 本窗最终被哪一层/哪个尺度拦下。落 trace,是误报/漏报成因分解(实验 E1)的主键——
# 没有这个字段就无法回答「误报里漫射光变占多少、局部光变占多少」,也就无法判断
# v2/v3 该不该做。
VERDICT_NO_FRAMES = "no_frames"  # 窗内无可用帧(解码异常)
VERDICT_COLD_START = "cold_start"  # 无跨窗基准,强制放行以建立基准
VERDICT_MODE_SWITCH = "mode_switch"  # 昼夜模式翻转,吞掉本窗并重建基准
VERDICT_NO_STRUCTURE = "no_structure"  # 亮度响应无超阈像素 → 真静止
VERDICT_LIGHT_FRAME = "light_frame"  # PCT@全帧 判漫射光变(开灯/AGC/IR 切换/色温)
# PCT@分块 把所有响应块都判成了局部光变(半房照亮/窗帘拉开/云过/隔壁开灯)。与
# light_frame 分开记桶,E1 的误报成因分解才能把「漫射」与「局部结构化」两类光变分别数
# 出来——它们各自的占比直接决定方案 v2/v3 该不该做。
VERDICT_LIGHT_BLOCKS = "light_blocks"
# 响应全部落在「持续变化源」块里(OSD 水印/摆动线缆/风扇/屏幕)。与 blocks_rejected 分桶:
# 前者说明抑制层真的在干活,后者只说明块不合格——分不开就无法判断这一层的收益。
# 候选块的变化是一次**纯加性光度偏移**(真机主要误报源=H.264 宏块带状伪影)。与 light_* 分桶:
# 前者是 PCT 在网格上判的,后者是直接在候选块自己的响应像素上判的——真机上恰恰是后者才抓得住。
VERDICT_OFFSET_ONLY = "offset_only"
VERDICT_ENV_SUPPRESSED = "env_suppressed"
VERDICT_BLOCKS_REJECTED = "blocks_rejected"  # 有响应、非光变,但无合格块(面积/紧凑度)
VERDICT_TRIGGER = "trigger"  # 放行
VERDICT_LEGACY_DIFF = "legacy_diff"  # visual_algo="diff" 回退路径

# 同一窗多个帧对各有判定时,报**响应最大**的那一对(见 evaluate_visual 里的 best 逻辑),
# 响应相同才用这个偏序打平。顺序不能反过来:一个静止对因几个噪点走到 blocks_rejected,
# 排名会盖过真正发生了全局光变的那一对,成因分解就把光变窗记成"块被拒"。
# 判 count==0 时归因给「光变」还是「块不合格」的分界:被光变抑制掉的响应占原响应的比例。
_SUPPRESSED_ATTRIBUTION = 0.5

_VERDICT_RANK = {
    VERDICT_NO_STRUCTURE: 1,
    VERDICT_LIGHT_FRAME: 2,
    VERDICT_LIGHT_BLOCKS: 3,
    VERDICT_OFFSET_ONLY: 4,
    VERDICT_ENV_SUPPRESSED: 5,
    VERDICT_BLOCKS_REJECTED: 6,
}


class _OffsetParams(NamedTuple):
    """加性偏移判据的参数组(昼夜共用)。"""

    enabled: bool
    coh_min: float
    cv_max: float
    std_delta_k: float
    std_a_max: float
    min_px: int


def _offset_params(config: GateConfig) -> _OffsetParams:
    return _OffsetParams(
        enabled=config.offset_test_enabled,
        coh_min=config.offset_coh_min,
        cv_max=config.offset_cv_max,
        std_delta_k=config.offset_std_delta_k,
        std_a_max=config.offset_std_a_max,
        min_px=config.offset_min_px,
    )


class _EnvParams(NamedTuple):
    """持续变化源抑制的参数组(昼夜共用,故不进 _Band 的分档部分)。"""

    enabled: bool
    duty_alpha: float
    duty_thresh: float
    active_cov: float
    salience_k: float
    area_max_ratio: float


def _env_params(config: GateConfig) -> _EnvParams:
    return _EnvParams(
        enabled=config.env_mask_enabled,
        duty_alpha=config.env_duty_alpha,
        duty_thresh=config.env_duty_thresh,
        active_cov=config.env_active_cov,
        salience_k=config.env_salience_k,
        area_max_ratio=config.env_area_max_ratio,
    )


def _block_stats(
    mask: NDArray[np.uint8], resp: NDArray[np.uint8], grid: int
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """逐块的 (响应覆盖率, 响应幅度均值)。INTER_AREA 求的正是区域均值。

    幅度只在响应像素上取:``sum(resp·mask)/sum(mask)``——否则大片未响应像素会把均值稀释到
    与覆盖率强相关,失去「幅度」这个独立维度(显著性解除全靠它)。
    """
    m = mask.astype(np.float32)
    cov = cv2.resize(m, (grid, grid), interpolation=cv2.INTER_AREA)
    weighted = cv2.resize(
        resp.astype(np.float32) * m, (grid, grid), interpolation=cv2.INTER_AREA
    )
    amp = np.where(cov > 1e-6, weighted / np.maximum(cov, 1e-6), 0.0)
    return cov.astype(np.float32), amp.astype(np.float32)


def _env_suppress(
    mask: NDArray[np.uint8],
    cov: NDArray[np.float32],
    amp: NDArray[np.float32],
    baseline: VisualBaseline | None,
    env: _EnvParams,
    grid: int,
) -> tuple[NDArray[np.uint8], float]:
    """把「持续变化源」块从响应掩码里剔除。返回 (新掩码, 被抑制块占比)。

    抑制条件 = ``占空比 ≥ duty_thresh`` **且** ``本窗幅度 ≤ amp_base × salience_k``。

    后半条是**解除**条件(方案 §7.4):固定源的幅度稳定,人/物挡在它前面会让该处幅度显著跳高
    → 解除抑制、照常触发。绝不能改成「占空比越高压得越狠」——人站在水印/风扇前恰好会把该处
    占空比推得更高,那样正好压掉要抓的东西。

    统计(duty/amp_base)由 :func:`_env_update` 在**抑制之前**的量上更新(方案 §7.2),本函数只读。
    """
    if not env.enabled or baseline is None or baseline.duty is None:
        return mask, 0.0
    duty = baseline.duty
    base = baseline.amp_base if baseline.amp_base is not None else np.zeros_like(duty)
    persistent = duty >= env.duty_thresh
    salient = amp > np.maximum(base, 1e-6) * env.salience_k
    suppress = persistent & ~salient
    ratio = float(np.mean(suppress))
    # 面积上限:大半画面被判成持续源必然是判错了(相机被移动/整体光变)→ 整体不抑制。
    if ratio <= 0.0 or ratio > env.area_max_ratio:
        return mask, 0.0
    keep = cv2.resize(
        (~suppress).astype(np.uint8), _DIFF_SIZE, interpolation=cv2.INTER_NEAREST
    )
    return mask * keep, ratio


def _env_update(
    baseline: VisualBaseline | None,
    cov: NDArray[np.float32],
    amp: NDArray[np.float32],
    env: _EnvParams,
    grid: int,
) -> tuple[
    NDArray[np.float32], NDArray[np.float32], NDArray[np.float32], NDArray[np.float32]
]:
    """按**抑制之前**的逐块响应更新占空比与幅度基线(方案 §7.2 定死的顺序)。

    顺序反了会自我拆台:一旦开始抑制,被抑制块的响应归零 → 占空比掉下去 → 抑制解除 → 又开始
    误报,来回振荡。统计只观测原始响应,与消费(抑制)是两条并行通路。

    amp_base 只在该块**活跃**时更新——不活跃时的幅度是 0,会把基线拉到 0、使显著性判据永远
    成立、抑制永不生效。
    """
    prev_duty = (
        baseline.duty
        if baseline is not None and baseline.duty is not None
        else np.zeros((grid, grid), np.float32)
    )
    prev_amp = (
        baseline.amp_base
        if baseline is not None and baseline.amp_base is not None
        else np.zeros((grid, grid), np.float32)
    )
    if prev_duty.shape != (grid, grid):  # grid 配置变了 → 重建,别用错形状的历史
        prev_duty = np.zeros((grid, grid), np.float32)
        prev_amp = np.zeros((grid, grid), np.float32)

    prev_active = (
        baseline.prev_active
        if baseline is not None and baseline.prev_active is not None
        and baseline.prev_active.shape == (grid, grid)
        else np.zeros((grid, grid), np.float32)
    )
    prev_blink = (
        baseline.blink
        if baseline is not None and baseline.blink is not None
        and baseline.blink.shape == (grid, grid)
        else np.zeros((grid, grid), np.float32)
    )

    active = (cov >= env.active_cov).astype(np.float32)
    a = env.duty_alpha
    duty = (1.0 - a) * prev_duty + a * active
    amp_base = np.where(active > 0, (1.0 - a) * prev_amp + a * amp, prev_amp)
    # blink:活跃状态的翻转率。用 |active - prev_active| 而非 duty 的一阶差分——后者会被
    # EMA 平滑掉,恰好抹掉「高频闪烁」这个唯一的信号。
    blink = (1.0 - a) * prev_blink + a * np.abs(active - prev_active)
    return (duty.astype(np.float32), amp_base.astype(np.float32),
            blink.astype(np.float32), active)


class _Band(NamedTuple):
    """一档(昼/夜)的全部承重参数。由 :func:`_band_for` 从 GateConfig 摊平得到。"""

    check_fps: int
    blur_ksize: int
    # 响应阈 = max(struct_abs_floor, struct_k·σ)。绝对下限防「σ 估得过小时阈值塌到
    # 量化噪声量级」,相对项让阈值随夜间噪声自动抬高。
    struct_k: float
    struct_abs_floor: float
    morph_ksize: int
    min_fill_ratio: float
    min_block_ratio: float
    pct: PctThresholds
    block_grid: int
    env: _EnvParams
    offset: _OffsetParams


def _band_for(config: GateConfig, mode: str) -> _Band:
    if mode == MODE_NIGHT:
        return _Band(
            check_fps=config.night_check_fps,
            blur_ksize=config.night_blur_ksize,
            struct_k=config.night_struct_k,
            struct_abs_floor=config.night_struct_abs_floor,
            morph_ksize=config.morph_ksize,
            min_fill_ratio=config.min_fill_ratio,
            min_block_ratio=config.night_min_block_ratio,
            pct=PctThresholds(
                residual_k=config.night_pct_residual_k,
                residual_rel=config.pct_residual_rel,
                ncc_tau=config.night_pct_ncc_tau,
                texture_k=config.night_pct_texture_k,
                neighbor_k=config.night_pct_neighbor_k,
                light_coverage_min=config.pct_light_coverage_min,
                ncc_evidence_k=config.pct_ncc_evidence_k,
            ),
            block_grid=config.pct_block_grid,
            env=_env_params(config),
            offset=_offset_params(config),
        )
    return _Band(
        check_fps=config.check_fps,
        blur_ksize=config.struct_blur_ksize,
        struct_k=config.struct_k,
        struct_abs_floor=config.struct_abs_floor,
        morph_ksize=config.morph_ksize,
        min_fill_ratio=config.min_fill_ratio,
        min_block_ratio=config.min_block_ratio,
        pct=PctThresholds(
            residual_k=config.pct_residual_k,
            residual_rel=config.pct_residual_rel,
            ncc_tau=config.pct_ncc_tau,
            texture_k=config.pct_texture_k,
            neighbor_k=config.pct_neighbor_k,
            light_coverage_min=config.pct_light_coverage_min,
            ncc_evidence_k=config.pct_ncc_evidence_k,
        ),
        block_grid=config.pct_block_grid,
        env=_env_params(config),
        offset=_offset_params(config),
    )


@dataclass(frozen=True)
class VisualBaseline:
    """visual gate 的跨窗状态。调用方按 device 存,原样回传下一窗。

    比裸基准帧多带昼夜模式 + 待切换计数,是因为模式滞回本身需要记忆:没有「上一窗是哪
    档」就既做不了 N 窗确认(黄昏时会在两档之间反复翻),也识别不出「模式刚翻转」这个
    需要吞掉一窗重建基准的时刻。v3(占空比统计 + 长期基线)的状态也会挂在这里。
    """

    frame: NDArray[np.uint8]
    mode: str = MODE_DAY
    # 候选档与当前档不一致的连续窗数;达到 night_confirm_windows 才真的切换。
    pending_switch: int = 0
    # 跨窗最小值跟踪的帧间差尺度(灰度级),0 = 未知。**必须跨窗**,不能逐帧对估:
    # 大面积变化(全局光变、人贴镜头、相机被移动)时当前帧对的任何统计量都被污染,尺度暴涨
    # → 阈值暴涨 → 变化反而变得不可见。实测「人贴镜头占>50%」在逐对估法下直接漏报。
    # 噪声底本是相机的慢变属性,最小值跟踪(快降慢升)取的就是「安静基线」。
    diff_scale: float = 0.0
    # 持续变化源统计,grid×grid(与 PCT@分块 同网格)。None = 尚未建立。
    # duty:该块「本窗活跃」的 EMA 占空比;amp_base:该块活跃时的响应幅度基线(灰度级)。
    # 位置固定且几乎每窗都在变的源(OSD 水印/摆动线缆/风扇/屏幕)占空比趋 1;抑制的解除靠
    # 「本窗幅度显著超过 amp_base」,而不是靠占空比本身(方案 §7.4:后者方向是反的)。
    duty: NDArray[np.float32] | None = None
    amp_base: NDArray[np.float32] | None = None
    # blink:该块「活跃状态发生翻转」的 EMA 频率(SuBSENSE 的 blinking-pixel / dynamic-entropy
    # 同构)。**与 duty 数的是不同的东西,方向也相反**:duty 数「是不是活跃」,人站着不动会把它
    # 顶到最高(实测 0.87)而编码伪影只有 0.28 —— 方向反了,这正是 duty 层被真机证伪的原因;
    # blink 数「活跃↔不活跃切换了多少次」,人长驻切换少(实测 0.025),伪影闪烁切换多(0.438)。
    # 现阶段**只测不用**:落 trace 供标定,不参与任何抑制决策。
    blink: NDArray[np.float32] | None = None
    # 上窗的逐块活跃布尔(float 0/1),blink 需要它才能算「翻转」。
    prev_active: NDArray[np.float32] | None = None


@dataclass(frozen=True)
class VisualDiag:
    """承重中间量,全部落 trace。7 个阈值靠这些分布标定(方案 §3.5)。

    取值来自「本窗最有代表性的那个帧对」——按 (verdict rank, struct_ratio) 取最大,
    即触发时取触发的那一对、未触发时取最靠后被拦的那一对。
    """

    verdict: str = VERDICT_NO_FRAMES
    mode: str = MODE_DAY
    # PCT@全帧 三判据原始值
    pct_alpha: float = 1.0
    pct_beta: float = 0.0
    pct_residual: float = 0.0
    pct_ncc: float = 0.0
    pct_texture: float = 0.0
    # 绝对灰度级量(标定阈值系数只能靠这些的真实分布,相对值一旦标尺坏了就无从反推)
    pct_resid_abs: float = 0.0
    pct_texture_abs: float = 0.0
    diff_scale: float = 0.0
    # 本窗未经跨窗跟踪的原始候选值。与 diff_scale 并列落库:跟踪器一旦跑偏(首版最小值跟踪
    # 就棘轮到了极值),只看跟踪值是看不出来的,必须能对比原始候选。
    raw_scale: float = 0.0
    struct_threshold: float = 0.0
    # 响应幅度分位(灰度级):选阈值的直接依据,见 _evaluate_pair 里的说明。
    resp_p90: float = 0.0
    resp_p99: float = 0.0
    # 本窗实际抽检的帧数与帧对数。判定是所有帧对的 OR,帧对数直接决定误报机会,而抽帧受
    # 「配置声明 fps vs 实际解码 fps」影响极易偏离预期——落库才能证实而不是靠推算。
    checked_frames: int = 0
    pair_count: int = 0
    # 最大候选块的 bbox(448 网格,x/y/w/h)。「变化发生在画面哪个位置」是查误报的第一问——
    # OSD 时间水印固定在角上、周期源固定在某处,只看面积/紧凑度分不出来,看位置一眼就分出来。
    max_block_x: int = 0
    max_block_y: int = 0
    max_block_w: int = 0
    max_block_h: int = 0
    # 结构响应超阈像素占比(新的 video_score 语义)
    struct_ratio: float = 0.0
    # PCT@分块 判为光变的块占比(半房照亮这类局部光变的体量)
    light_block_ratio: float = 0.0
    # 被「持续变化源」抑制掉的块占比。这一层的收益直接看它 + env_suppressed 桶的计数。
    env_block_ratio: float = 0.0
    # 加性偏移判据在「最大候选块」上的四个原始量(阈值靠它们的分布标定)+ 被它剔掉的块数。
    offset_sign_coh: float = 0.0
    offset_shift_cv: float = 0.0
    offset_std_delta: float = 0.0
    offset_std_a: float = 0.0
    offset_rejected: int = 0
    # 最热块的占空比与响应覆盖率。抑制层不生效时,只看 env_block_ratio=0 分不出是「占空比没
    # 爬上来」还是「爬上来了但被显著性解除」——必须同时看这两个量。
    env_duty_max: float = 0.0
    env_cov_max: float = 0.0
    # blink 统计(只测不用,见 VisualBaseline.blink)。``env_blink_at_block`` 是**承重的那个**:
    # 全图 max 会被任意一个闪烁块占满,标定需要的是「触发的那一块历史上闪不闪」。
    env_blink_max: float = 0.0
    env_blink_at_block: float = 0.0
    env_duty_at_block: float = 0.0
    # 候选块统计
    block_count: int = 0
    max_block_ratio: float = 0.0
    max_block_fill: float = 0.0
    # 旧裸帧差打分,同窗并行计算只为 A/B 对照(E1 要拿新旧判定对齐着看)。不参与判定。
    legacy_diff_score: float = 0.0
    # 单帧空间高频噪声 σ(灰度级)。**已不是阈值基准**(基准是 diff_scale),仅并列诊断:
    # 两者的差距就是首版被真机证伪的根因,留着好定位类似问题。
    noise_sigma: float = 0.0


@dataclass(frozen=True)
class VisualEvalResult:
    """visual gate 评估输出。

    intra_max / cross_max 分开返回供诊断:跨窗对的时间差 ≈ window_duration,远大于窗内
    邻帧;若 cross_max 持续 >> intra_max,说明 ISP 长周期漂移(AGC/IR/AWB)主导。改用结构
    判据后这两个字段的语义从「变化像素占比」变为「结构响应超阈像素占比」,量纲仍是 0-1。
    """

    changed: bool
    max_score: float
    intra_max: float
    cross_max: float
    last_checked: VisualBaseline | None
    diag: VisualDiag = field(default_factory=VisualDiag)


def evaluate_visual(
    frames: list[NDArray[np.uint8]],
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray[np.uint8] | VisualBaseline | None = None,
    dump_tag: str = "",
) -> VisualEvalResult:
    """判本窗画面有没有真实变动。

    prev_frame: 上一窗口返回的 ``last_checked``(``VisualBaseline``)——把跨窗口边界的变化
    也纳入检测,避免"变化恰好发生在两个窗口之间"被漏掉。为兼容旧调用方,也接受裸的预处理
    灰度帧(等价于 ``VisualBaseline(frame=...)``,昼夜模式从本窗重新判定)。

    last_checked 里的 frame 是本窗口末次被检帧的预处理结果,调用方存下来作为下窗基准。
    基准取"末次被检帧"而非"窗口末帧"(否则末检点之后的变化会两窗都漏)。frames 为空时
    返回 None,调用方应保留旧基准。
    """
    baseline = _as_baseline(prev_frame)

    if not frames:
        return VisualEvalResult(False, 0.0, 0.0, 0.0, None)

    # 昼夜模式要先定,因为 check_fps 本身是分档的(夜间可降采样省算力)。用首帧的饱和度
    # 判——多一次 resize,亚毫秒;而首帧必然在抽检序列里(range 从 0 起),预处理结果复用。
    first_gray, first_sat = _preprocess_with_sat(frames[0]) if frames[0].size > 0 else (None, 0.0)
    mode, pending, switched = _resolve_mode(config, baseline, first_sat)
    band = _band_for(config, mode)

    interval = max(1, round(input_fps / band.check_fps))
    check_indices = list(range(0, len(frames), interval))
    if len(check_indices) < 2 and len(frames) >= 2:
        check_indices.append(len(frames) - 1)

    # 每帧只预处理一次;跳过空帧(解码异常兜底,cv2.resize 对空数组会抛错)
    grays: list[NDArray[np.uint8]] = []
    for i in check_indices:
        if frames[i].size == 0:
            continue
        grays.append(first_gray if (i == 0 and first_gray is not None) else _preprocess(frames[i]))
    if not grays:
        return VisualEvalResult(False, 0.0, 0.0, 0.0, None)


    # 模式刚翻转:IR 切换本身是一次大幅光变,PCT@全帧 大概率能吃掉,但基准帧来自另一种
    # 成像模式、光度关系不可比 → 吞掉本窗并重建基准,不做判定。代价是每天日出/日落各丢
    # 一窗,而夜间档本就偏向拦截(D4),方向一致。
    if switched:
        # 模式翻转时连尺度一起重置(diff_scale=0):IR 切换后成像特性变了,旧噪声底不再适用。
        return VisualEvalResult(
            False, 0.0, 0.0, 0.0,
            VisualBaseline(frame=grays[-1], mode=mode, pending_switch=pending),
            VisualDiag(verdict=VERDICT_MODE_SWITCH, mode=mode),
        )

    if config.visual_algo == "diff":
        return _evaluate_legacy_diff(
            grays, config, baseline,
            VisualBaseline(
                frame=grays[-1], mode=mode, pending_switch=pending,
                diff_scale=baseline.diff_scale if baseline is not None else 0.0,
            ),
            mode,
        )

    # 阈值基准:先在所有帧对上取**最小**的候选尺度(最安静的那一对最能代表噪声底),再与跨窗
    # 跟踪值做「快降慢升」的最小值跟踪。全窗共用一个尺度,判定在窗内自洽。
    raw_scale = _raw_scale(baseline, grays)
    scale = _track_scale(baseline, raw_scale, config)

    # 帧对 = 跨窗对(上窗末帧 ↔ 本窗首帧) + 窗内相邻对。逐帧特征算一次、被相邻两对复用。
    feats = [frame_features(g, blur_ksize=band.blur_ksize) for g in grays]
    base_feat = (
        frame_features(baseline.frame, blur_ksize=band.blur_ksize)
        if baseline is not None else None
    )

    pairs: list[tuple[FrameFeat, FrameFeat, bool]] = []  # (a, b, is_cross)
    if base_feat is not None:
        pairs.append((base_feat, feats[0], True))
    pairs.extend((a, b, False) for a, b in zip(feats, feats[1:]))

    intra_max = 0.0
    cross_max = 0.0
    changed = False
    best: tuple[bool, float, int] = (False, -1.0, -1)
    outcomes: list[_PairOutcome] = []
    best_diag = VisualDiag(verdict=VERDICT_NO_STRUCTURE, mode=mode)

    for a, b, is_cross in pairs:
        outcome = _evaluate_pair(a, b, band, scale, baseline)
        outcomes.append(outcome)
        if is_cross:
            cross_max = max(cross_max, outcome.diag.struct_ratio)
        else:
            intra_max = max(intra_max, outcome.diag.struct_ratio)
        changed = changed or outcome.triggered
        key = (
            outcome.triggered,
            outcome.diag.struct_ratio,
            _VERDICT_RANK.get(outcome.diag.verdict, 0),
        )
        if key > best:
            best, best_diag = key, outcome.diag

    max_score = max(intra_max, cross_max)

    # 占空比统计取窗内**各块的最大**覆盖率/幅度(抑制之前的量):窗内任一帧对该块活跃就算活跃,
    # 否则 16 个帧对里只有一半跨过水印跳变时,占空比会被没跳变的那些对稀释到阈值以下。
    band_env = _env_params(config)
    grid = band.block_grid
    covs = [o.blk_cov for o in outcomes if o.blk_cov is not None]
    amps = [o.blk_amp for o in outcomes if o.blk_amp is not None]
    win_cov = (
        np.max(np.stack(covs), axis=0)
        if covs else np.zeros((grid, grid), np.float32)
    )
    win_amp = (
        np.max(np.stack(amps), axis=0)
        if amps else np.zeros((grid, grid), np.float32)
    )
    duty, amp_base, blink, active = _env_update(
        baseline, win_cov, win_amp, band_env, grid
    )
    duty_max = float(duty.max()) if duty.size else 0.0
    cov_max = float(win_cov.max()) if win_cov.size else 0.0
    blink_max = float(blink.max()) if blink.size else 0.0
    # 合格斑块**覆盖到的所有网格块**上的 duty/blink 最大值。标定要的是「这一处历史上闪不闪」,
    # 不是全图 max —— 人横穿画面时路径上每块都翻转,全图 max 立刻顶满(实测长驻用例下
    # blink_max == duty_max == 0.97),分不开任何东西。
    # ⚠️ 也不能只取 bbox **左上角**所在的那一块:斑块跨若干块,而角点所在块通常是覆盖最少的
    # 那个,常常根本没到 active_cov(实测活跃块是 (5,4)(6,3)(6,4)(7,4) 而角点落在 (5,3),
    # 读出来恒为 0)。
    blink_at = duty_at = 0.0
    if best_diag.max_block_w > 0 and blink.size:
        bs = _DIFF_SIZE[0] / grid
        r0 = int(min(grid - 1, best_diag.max_block_y // bs))
        c0 = int(min(grid - 1, best_diag.max_block_x // bs))
        r1 = int(min(grid, (best_diag.max_block_y + best_diag.max_block_h) // bs + 1))
        c1 = int(min(grid, (best_diag.max_block_x + best_diag.max_block_w) // bs + 1))
        blink_at = float(blink[r0:r1, c0:c1].max())
        duty_at = float(duty[r0:r1, c0:c1].max())
    new_baseline = VisualBaseline(
        frame=grays[-1], mode=mode, pending_switch=pending, diff_scale=scale,
        duty=duty, amp_base=amp_base, blink=blink, prev_active=active,
    )

    # cold-start:无跨窗基准时静止画面所有判据都归零,会被当无变化丢掉,导致开机已存在的
    # 场景永不被感知。把首个窗视作变化放行以建立基准(空帧窗已在上面提前 return)。
    if baseline is None:
        changed = True
        verdict = VERDICT_COLD_START
    else:
        verdict = VERDICT_TRIGGER if changed else best_diag.verdict

    result = VisualEvalResult(
        changed=changed,
        max_score=max_score,
        intra_max=intra_max,
        cross_max=cross_max,
        last_checked=new_baseline,
        diag=_replace_diag(
            best_diag, verdict=verdict, mode=mode, raw_scale=raw_scale,
            checked_frames=len(grays), pair_count=len(pairs),
            env_duty_max=duty_max, env_cov_max=cov_max,
            env_blink_max=blink_max, env_blink_at_block=blink_at,
            env_duty_at_block=duty_at,
        ),
    )
    _maybe_dump(config, grays, baseline, result, frames[0] if frames else None, dump_tag)
    return result


class _PairOutcome(NamedTuple):
    triggered: bool
    diag: VisualDiag
    # 抑制**之前**的逐块响应覆盖率/幅度,窗级据此更新占空比统计(方案 §7.2)。
    blk_cov: NDArray[np.float32] | None = None
    blk_amp: NDArray[np.float32] | None = None


def _evaluate_pair(
    a: FrameFeat,
    b: FrameFeat,
    band: _Band,
    scale: float,
    baseline: VisualBaseline | None,
) -> _PairOutcome:
    """单帧对:亮度响应 → PCT@全帧 → PCT@分块 → 连通域/紧凑度/面积 → 候选块。

    **顺序是承重的**:先算响应(一次 absdiff),没有超阈像素就立刻返回。理由有两条——
    ①生产上绝大多数窗是静止的,这一步就短路掉全部 PCT / 形态学 / 连通域开销;②静止画面
    天然「仿射一致 + 纹理保持」,若先判 PCT 会被记成 ``light_frame``,把 E1 的漫射光变
    误报桶灌满静止窗,成因分解就失真了。
    """
    thresh = max(band.struct_abs_floor, band.struct_k * scale)
    resp = response(a, b)
    # 响应幅度的尾部分位:阈值只能从它的真实分布上选,不能靠「若干倍稳健尺度」推——实测
    # H.264 流的 |帧间差| 是**重尾**的(静止场景 4.2% 像素超过 3.7 倍稳健尺度,高斯只该有
    # 0.02%,尾部重 200 倍),任何「k×尺度」的取法都不可能干净分开编码伪影与低对比物体。
    p90, p99 = (
        float(v) for v in np.percentile(resp[::4, ::4], (90.0, 99.0))
    )
    mask = (resp > thresh).astype(np.uint8)
    struct_ratio = float(np.count_nonzero(mask)) / _MASK_TOTAL_PX
    legacy = _diff_processed(a.gray, b.gray)
    diag = VisualDiag(
        struct_ratio=struct_ratio,
        legacy_diff_score=legacy,
        diff_scale=scale,
        struct_threshold=thresh,
        resp_p90=p90,
        resp_p99=p99,
        # 单帧空间高频噪声,仅并列诊断用(与 diff_scale 的差距就是首版被证伪的根因)
        noise_sigma=max(noise_sigma(a.f32), noise_sigma(b.f32)),
    )
    if struct_ratio == 0.0:
        zeros = np.zeros((band.block_grid, band.block_grid), np.float32)
        return _PairOutcome(
            False, _replace_diag(diag, verdict=VERDICT_NO_STRUCTURE), zeros, zeros
        )

    cos2d, w = orientation_agreement(a, b)

    # ① PCT@全帧:**只作诊断与分桶,不否决**。
    #
    # 这是实现期发现的一个架构级问题(方案原文把全帧尺度写成了「判光变 → 整帧丢弃」):
    # 小面积真实变化在全帧统计上是**不可见**的。一只占画面 0.9% 的手,对全帧仿射残差的
    # 贡献被 99% 未变区域淹没,残差落在噪声量级 → 整帧被判光变 → 否决 → **正是方案要修的
    # 小面积动作漏报**。「同时有人走进来 + 开灯」更糟:光变主导了统计,人被一起否掉。
    #
    # 根本原因:「整幅经历了一次光照变换」这个假设,只有在整幅确实都变了时才**可检验**;
    # 响应只覆盖 1% 时,拟合由那 99% 没变的像素决定,它的低残差对那 1% 什么也没说。
    # 所以否决权全部交给分块尺度——那里每个块只用自己的像素作证,小物体的块自己说话。
    # 按 D2「优先不误拦」这是唯一安全的取法;代价只是全局光变窗多跑一次分块 PCT(~1ms)。
    frame_pct = pct_frame(
        frame_moments(a, b), cos2d, w, scale, _frame_thresholds(band.pct)
    )
    diag = _replace_diag(
        diag,
        pct_alpha=frame_pct.alpha, pct_beta=frame_pct.beta,
        pct_residual=frame_pct.residual, pct_resid_abs=frame_pct.resid_abs,
        pct_ncc=frame_pct.ncc,
        pct_texture=frame_pct.texture, pct_texture_abs=frame_pct.texture_abs,
    )

    # ② PCT@分块:唯一的抑制者。逐块复判,把光变块从响应掩码里剔除。
    light_blk, _res_blk, _ncc_blk = pct_blocks(
        a, b, cos2d, w, scale, band.pct, band.block_grid, mask
    )
    diag = _replace_diag(diag, light_block_ratio=float(np.mean(light_blk)))
    suppressed_frac = 0.0
    if light_blk.any():
        keep = cv2.resize(
            (~light_blk).astype(np.uint8), _DIFF_SIZE, interpolation=cv2.INTER_NEAREST
        )
        mask = mask * keep
        remain = float(np.count_nonzero(mask)) / _MASK_TOTAL_PX
        suppressed_frac = 1.0 - remain / struct_ratio
        if not mask.any():
            return _PairOutcome(False, _replace_diag(diag, verdict=_light_verdict(frame_pct)))

    # ③ 持续变化源抑制:先按**抑制前**的响应算逐块统计(顺序见 _env_update),再据历史统计抑制。
    blk_cov, blk_amp = _block_stats(mask, resp, band.block_grid)
    before_env = float(np.count_nonzero(mask))
    mask, env_ratio = _env_suppress(
        mask, blk_cov, blk_amp, baseline, band.env, band.block_grid
    )
    env_removed_frac = (
        1.0 - float(np.count_nonzero(mask)) / before_env if before_env > 0 else 0.0
    )
    diag = _replace_diag(diag, env_block_ratio=env_ratio)
    if not mask.any():
        return _PairOutcome(
            False, _replace_diag(diag, verdict=VERDICT_ENV_SUPPRESSED), blk_cov, blk_amp
        )

    # ④ 形态学去噪 + 连通域 + 紧凑度/最小面积
    if band.morph_ksize > 1:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (band.morph_ksize, band.morph_ksize)
        )
        # CLOSE 先于 OPEN:低对比物体的响应是「实心但带噪声空洞」——Δ=8 灰度配 σ_diff≈1.7
        # 的差分噪声,阈值处会有 ~13% 的像素掉到阈下。先 OPEN(crop_enhance 的顺序,那里
        # 输入是高对比全分辨率帧)会顺着这些空洞把实心块打碎成一堆过不了面积的碎片 → 漏报。
        # 先 CLOSE 补洞再 OPEN 去散点,对「实心块 + 散碎噪点」这组输入才是对的顺序。
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    n_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    count = 0
    max_ratio = 0.0
    max_fill = 0.0
    max_box = (0, 0, 0, 0)
    offset_rejected = 0
    off_diag = None
    for i in range(1, n_labels):  # 0 是背景
        area = int(stats[i, cv2.CC_STAT_AREA])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        ratio = area / _MASK_TOTAL_PX
        fill = area / (bw * bh) if bw * bh > 0 else 0.0
        if ratio < band.min_block_ratio or fill < band.min_fill_ratio:
            continue
        # 加性偏移判据:直接在这个块自己的响应像素上问「是不是一次纯光度偏移」。
        # 放在连通域**之后**是关键——它避免了 PCT@分块 的网格对不齐问题(变化区比网格单元窄时,
        # 单元内混了变了/没变两群像素,单一仿射映射必然拟合不了)。这里的作用域正是候选块本身。
        off = additive_offset_test(
            a.gray, b.gray, (labels == i).astype(np.uint8), scale=scale,
            coh_min=band.offset.coh_min, cv_max=band.offset.cv_max,
            std_delta_k=band.offset.std_delta_k, std_a_max=band.offset.std_a_max,
            min_px=band.offset.min_px,
        )
        if off_diag is None or ratio > max_ratio:
            off_diag = off
        if off.is_offset and band.offset.enabled:
            offset_rejected += 1
            continue
        count += 1
        if ratio > max_ratio:
            max_ratio, max_fill = ratio, fill
            max_box = (
                int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]), bw, bh
            )

    diag = _replace_diag(
        diag, block_count=count, max_block_ratio=max_ratio, max_block_fill=max_fill,
        max_block_x=max_box[0], max_block_y=max_box[1],
        max_block_w=max_box[2], max_block_h=max_box[3],
        offset_rejected=offset_rejected,
        offset_sign_coh=off_diag.sign_coherence if off_diag else 0.0,
        offset_shift_cv=off_diag.shift_cv if off_diag else 0.0,
        offset_std_delta=off_diag.std_delta if off_diag else 0.0,
        offset_std_a=off_diag.std_a if off_diag else 0.0,
    )
    if count == 0 and offset_rejected > 0:
        # 合格块全被加性偏移判据剔掉 → 归因给它,而不是笼统的「块不合格」。
        return _PairOutcome(
            False, _replace_diag(diag, verdict=VERDICT_OFFSET_ONLY), blk_cov, blk_amp
        )
    if count == 0:
        # 无合格块。若响应的大部分本就是被某个抑制层拿掉的,就归因给那一层而不是「块不合格」——
        # 否则「持续源被压掉 + 别处几个残留噪点」会被记进噪点桶,成因分解看不出抑制层的收益。
        # 两层都过阈时取移除更多的那层。
        if max(suppressed_frac, env_removed_frac) < _SUPPRESSED_ATTRIBUTION:
            verdict = VERDICT_BLOCKS_REJECTED
        elif env_removed_frac >= suppressed_frac:
            verdict = VERDICT_ENV_SUPPRESSED
        else:
            verdict = _light_verdict(frame_pct)
        return _PairOutcome(
            False, _replace_diag(diag, verdict=verdict), blk_cov, blk_amp
        )
    return _PairOutcome(
        True, _replace_diag(diag, verdict=VERDICT_TRIGGER), blk_cov, blk_amp
    )


def _light_verdict(frame_pct: PctResult) -> str:
    """给「响应被光变抑制」的窗命名桶。

    整幅能用同一个光照变换解释 → 漫射光变(开灯/AGC/IR 切换);否则 → 局部结构化光变
    (半房照亮/窗帘/云过/隔壁开灯)。E1 的误报成因分解要把这两类分别数出来,它们各自的
    占比直接决定方案 v2/v3 该不该做。
    """
    return VERDICT_LIGHT_FRAME if frame_pct.is_light else VERDICT_LIGHT_BLOCKS


def _frame_thresholds(block_thr: PctThresholds) -> PctThresholds:
    """全帧尺度的 PCT 阈值 = 分块阈值,但**纹理下限护栏归零**。

    护栏③在分块尺度必须有(完全落在均匀物体内部的块与「平坦墙被照亮」同签名),但在全帧
    尺度它反而制造误报:一间平坦白墙的房间整体变亮,残差为 0——「每个像素都被同一个仿射
    变换解释」在整帧尺度上已是很强的证据(有人走进来就会破坏它:那部分像素不跟随,残差
    立刻升到 1),不需要再要求纹理。留着③只会让「平坦房间开灯」变成误报。
    """
    return PctThresholds(
        residual_k=block_thr.residual_k,
        residual_rel=block_thr.residual_rel,
        ncc_tau=block_thr.ncc_tau,
        texture_k=0.0,
        neighbor_k=block_thr.neighbor_k,
        light_coverage_min=block_thr.light_coverage_min,
        ncc_evidence_k=block_thr.ncc_evidence_k,
    )


def _maybe_dump(
    config: GateConfig,
    grays: list[NDArray[np.uint8]],
    baseline: VisualBaseline | None,
    result: VisualEvalResult,
    ref_bgr: NDArray[np.uint8] | None,
    dump_tag: str,
) -> None:
    """把触发窗的**输入**落盘,供离线逐阶段复现(见 config.gate_debug_dump_dir)。

    只存输入(抽检灰度帧 + 上窗基准 + 全分辨率参考帧 + diag),不存中间结果:各阶段在本地用
    同一份代码重跑,改可视化不必重新部署,复现路径也与线上严格一致。

    ``gate_debug_dump_on_disagree`` 打开时只落**新旧判定不一致**的窗(见 :func:`_is_disagreement`)。

    **落全部窗,不只落触发窗**:只落触发窗的样本是有偏的——看不到「本判据漏了而别的判据能抓到」
    的窗,也量不出误报的真实下降,在那种样本上调阈值等于自欺。dump_tag 带 device 与窗口结束
    时间,离线才能把 omni 的描述对齐成标签(用真看过视频的 VLM 当真值,而不是拿旧算法当参照)。
    """
    global _dump_count
    if not config.gate_debug_dump_dir:
        return
    if _dump_count >= config.gate_debug_dump_max:
        return
    if config.gate_debug_dump_on_disagree and not (
        _is_disagreement(config, result)
        # offset 判据单独落盘:被它压掉的窗旧算法通常也不触发 → 不构成分歧 → 默认
        # 落不下来。而它压掉的恰是「首遇漏报」的最可能来源(纯色物体贴平滑墙),必须
        # 留证人工复核——样本量小(实测 ~3% 分歧窗),不会写爆磁盘。
        or result.diag.verdict == VERDICT_OFFSET_ONLY
    ):
        return
    try:
        import json
        import time as _time
        from dataclasses import asdict
        from pathlib import Path

        out = Path(config.gate_debug_dump_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        stamp = dump_tag or f"{int(_time.time() * 1000)}"
        np.savez_compressed(
            out / f"{stamp}.npz",
            grays=np.stack(grays),
            prev=(baseline.frame if baseline is not None else np.zeros((0, 0), np.uint8)),
        )
        payload = asdict(result.diag)
        payload["changed"] = bool(result.changed)
        (out / f"{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1)
        )
        if ref_bgr is not None and ref_bgr.size > 0:
            cv2.imwrite(str(out / f"{stamp}_ref.jpg"), ref_bgr)
        _dump_count += 1
    except Exception:  # 调试路径绝不能影响主流程
        logger.debug("gate debug dump failed", exc_info=True)


def _is_disagreement(config: GateConfig, result: VisualEvalResult) -> bool:
    """新判据与旧裸帧差是否给出了不同结论。

    落盘取样用:需要人工核实的恰恰是分歧窗——新拦旧放=新增漏报,新放旧拦=新增触发。

    ``cold_start`` / ``mode_switch`` 不是真正的判定(前者无条件放行以建立基准,后者吞掉一窗
    重建基准),把它们算成分歧会白占配额。
    """
    if result.diag.verdict in (VERDICT_COLD_START, VERDICT_MODE_SWITCH):
        return False
    return result.changed != (result.diag.legacy_diff_score >= config.change_threshold)


def _raw_scale(
    baseline: VisualBaseline | None, grays: list[NDArray[np.uint8]]
) -> float:
    """本窗所有帧对里**最小**的帧间差尺度候选值。

    取最小而非平均:窗内多个帧对中最安静的那一对最能代表噪声底,有真实运动的帧对候选值偏高、
    不该把基准带高(否则一有人活动阈值就升,反而更容易漏)。

    返回 0.0 = 本窗没有可用候选(全是重复帧),由 :func:`_track_scale` 沿用历史值。
    """
    pairs = list(zip(grays, grays[1:]))
    if baseline is not None:
        pairs.append((baseline.frame, grays[0]))
    cands = [
        c
        for a, b in pairs
        if (c := temporal_scale(_ScaleFrame(a), _ScaleFrame(b))) > 0.0
    ]
    return min(cands) if cands else 0.0


class _ScaleFrame(NamedTuple):
    """temporal_scale 只读 .gray,构造完整 FrameFeat(含 Sobel)是浪费。"""

    gray: NDArray[np.uint8]


def _track_scale(
    baseline: VisualBaseline | None, raw: float, config: GateConfig
) -> float:
    """跨窗**双向限速**跟踪帧间差尺度。

    每窗只允许基准变动 ±``scale_leak``(默认 5%),无论本窗候选值高低。

    # 为什么不是「最小值跟踪(快降慢升)」——踩过的坑

    首版用最小值跟踪:候选更低立刻采纳、更高只许缓升。方向错了——它会一路**棘轮**到「史上
    最安静的那一刻」,而判据需要的是「典型安静水平」。真机实测 53 窗后基准已被压到 1.02
    (不少窗顶到下限 0.5),而由静止场景响应占比反解的真实尺度约 2~3;后果是 `struct_k·scale`
    低于绝对下限、自适应完全没生效,PCT 的残差判据也因基准偏小而永不成立。

    双向限速则跟着 raw 的**中位趋势**走:被大面积变化污染的窗最多把基准顶高 5%,随后的安静窗
    每窗拉回 5%,不会棘轮到极值;真实上升(入夜噪声变大)约 20 窗(~3 分钟)追上。

    raw ≤ 0 表示本窗无可用候选(全是重复帧)→ 沿用历史值,不要把基准打到 0。
    候选值先被 ``scale_max`` 钉住上界,再进限速——否则窗内每个帧对都含大变化时(冷启动后第一窗
    赶上开灯/相机被移动)会采纳一个荒谬的基准,把那个变化自己变得不可见。
    """
    prev = baseline.diff_scale if baseline is not None else 0.0
    if raw <= 0.0:
        return prev
    # 物理上界:噪声底不可能有这么大,超了就是被大面积运动污染的候选值(见 config.scale_max)。
    capped = min(raw, config.scale_max)
    if prev <= 0.0:
        return capped
    leak = max(1.0 + 1e-6, config.scale_leak)
    return float(min(max(capped, prev / leak), prev * leak))


def _resolve_mode(
    config: GateConfig, baseline: VisualBaseline | None, sat_mean: float
) -> tuple[str, int, bool]:
    """昼夜模式判定 + 滞回。返回 (本窗模式, 新的待切换计数, 本窗是否刚翻转)。

    双阈滞回(进入夜间用低阈、退出用高阈)+ N 窗确认,防黄昏在两档之间反复翻转导致参数档
    抖动。相机若上报 IR 标志位,应优先接那个信号——饱和度均值只是没有标志位时的替代。
    """
    if sat_mean < config.night_sat_enter:
        candidate = MODE_NIGHT
    elif sat_mean > config.night_sat_exit:
        candidate = MODE_DAY
    else:
        candidate = baseline.mode if baseline is not None else MODE_DAY

    if baseline is None:
        return candidate, 0, False
    if candidate == baseline.mode:
        return baseline.mode, 0, False

    pending = baseline.pending_switch + 1
    if pending >= max(1, config.night_confirm_windows):
        return candidate, 0, True
    return baseline.mode, pending, False


def _evaluate_legacy_diff(
    grays: list[NDArray[np.uint8]],
    config: GateConfig,
    baseline: VisualBaseline | None,
    new_baseline: VisualBaseline,
    mode: str,
) -> VisualEvalResult:
    """``visual_algo="diff"`` 回退路径:原裸帧差 + 单阈值。

    保留它是为了出问题时不改代码即可回滚,以及给实验 E1 一个同口径的对照基线。
    """
    cross_max = (
        _diff_processed(baseline.frame, grays[0]) if baseline is not None else 0.0
    )
    intra_max = max(
        (_diff_processed(a, b) for a, b in zip(grays, grays[1:])), default=0.0
    )
    max_score = max(cross_max, intra_max)
    changed = bool(baseline is None or max_score >= config.change_threshold)
    return VisualEvalResult(
        changed=changed,
        max_score=max_score,
        intra_max=intra_max,
        cross_max=cross_max,
        last_checked=new_baseline,
        diag=VisualDiag(
            verdict=VERDICT_COLD_START if baseline is None else VERDICT_LEGACY_DIFF,
            mode=mode,
            struct_ratio=max_score,
            legacy_diff_score=max_score,
        ),
    )


def _as_baseline(
    prev: NDArray[np.uint8] | VisualBaseline | None,
) -> VisualBaseline | None:
    if prev is None or isinstance(prev, VisualBaseline):
        return prev
    return VisualBaseline(frame=prev)


def _replace_diag(diag: VisualDiag, **kw: object) -> VisualDiag:
    from dataclasses import replace

    return replace(diag, **kw)  # type: ignore[arg-type]


def _preprocess(frame: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Downscale to 448x448 grayscale for differencing.

    Downscale first (cheaper than cvtColor on 4K). INTER_AREA averages
    source-pixel regions, suppressing high-frequency aliasing/noise that
    INTER_LINEAR would propagate into the diff.
    """
    small = cv2.resize(frame, _DIFF_SIZE, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small


def _preprocess_with_sat(
    frame: NDArray[np.uint8],
) -> tuple[NDArray[np.uint8], float]:
    """同 :func:`_preprocess`,并顺带返回缩放后 BGR 的平均饱和度(昼夜模式判定用)。

    只对首帧调用:饱和度是全局量,窗内不会突变;而 resize 只做一次两处用,零额外成本。
    """
    small = cv2.resize(frame, _DIFF_SIZE, interpolation=cv2.INTER_AREA)
    sat = saturation_mean(small)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    return gray, sat


def _diff_processed(
    gray_a: NDArray[np.uint8], gray_b: NDArray[np.uint8], pixel_threshold: int = 25
) -> float:
    """Ratio of changed pixels between two preprocessed (448x448 gray) frames."""
    diff = cv2.absdiff(gray_a, gray_b)
    changed_pixels = np.count_nonzero(diff > pixel_threshold)
    return changed_pixels / diff.size


def compute_frame_diff(
    frame_a: NDArray[np.uint8], frame_b: NDArray[np.uint8], pixel_threshold: int = 25
) -> float:
    """Compute ratio of changed pixels between two raw frames.

    Frames are resized to 448x448 before comparison to avoid
    unnecessary computation on high-resolution inputs.
    """
    if frame_a.size == 0 or frame_b.size == 0:
        return 0.0
    return _diff_processed(_preprocess(frame_a), _preprocess(frame_b), pixel_threshold)
