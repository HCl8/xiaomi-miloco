"""Gate Layer — 光度一致性测试（PCT）与结构响应（纯 CV,无状态）。

判「画面变化」的两个正交问题各由本模块的一个函数回答:

- **是不是结构变化** → :func:`pair_maps` 的逐像素结构响应
- **这块变化是不是光变** → :func:`pct` 的区域级三判据（可跑在任意尺度）

两者共用同一套逐帧量（:func:`frame_features`）:梯度幅值 + 倍角方向向量。算一次、两处消费。

设计依据（方案 §2.1 / §四）: Tolaria ``miloco/gate-视觉门禁改造方案-真实变动-vs-光变``。
文献: 梯度**方向**是光照不变量而幅值不是（Huerta / Agrawal / LBSP）;纹理保持判据取
Sanin & Lovell 的 Large-Region texture 法（arXiv:1304.1233 评为「最准、抗去饱和」）。

# 职责划分:响应管「什么变了」,PCT 管「是不是光变」

把光照不变性做进**响应**里是个陷阱(本实现踩过一次):``|∇I_b − α·∇I_a|`` 这类梯度域判据
对「均匀色块盖住均匀背景」完全失明——色块**内部**改的是亮度、不改梯度,只有轮廓一圈有
响应,细到过不了紧凑度/面积过滤。而「一个人穿着单色上衣走过墙前」正是这个形状。

所以分工是:

- **响应** = 仿射补偿后的**亮度**残差 ``|I_b − (α̂·I_a + β̂)|``,在亮度域,对一切真实变化
  灵敏;全局光变被 α̂/β̂ 吸收后自然趋零。
- **PCT** = 不变性判据,回答「这个区域的变化能不能用一次光照变换解释」。

# 为什么光度模型是仿射(α, β)而不是纯乘性(α)

真实阴影是**加性**的:``I = 环境光 + α·直射光``——室内多有窗光+灯,环境光占比高,使
「变化前后亮度比恒定」这个假设本就不成立(方案 §10.2 边界3 自己承认了这点);相机 gamma
与 AGC 又把关系进一步弄成非线性。两参数仿射把「乘性增益」与「加性环境光/黑电平漂移」
一起纳入,是对这个已知缺陷的直接修补,而不是实现上的花招。

仿射最小二乘有闭式解,且残差只取决于两帧亮度的 **Pearson 相关系数**:

    α = Cov(a,b)/Var(a),  β = mean(b) − α·mean(a)
    min Σ(I_b − αI_a − β)² / N = Var(b)·(1 − r²)

故归一化残差取 ``sqrt(1 − r²)`` ——量纲无关、对比度无关、天然落在 [0,1],比「残差除以
均值」好标定得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import cv2
import numpy as np
from numpy.typing import NDArray

# 除零护栏。梯度/亮度都在 0-255 量级,1e-6 不影响任何判据的有效位。
_EPS = 1e-6
# Immerkær 噪声估计用的二阶差分核。对平滑图像响应为零、只对噪声与强边缘响应,配合中位数
# (对边缘鲁棒)给出 σ 的稳健估计。
_NOISE_KERNEL = np.array(
    [[1.0, -2.0, 1.0], [-2.0, 4.0, -2.0], [1.0, -2.0, 1.0]], dtype=np.float32
)
# 该核对独立高斯噪声的增益 = sqrt(36) = 6;1.4826 是 MAD→σ 的换算常数。
_NOISE_GAIN = 6.0
# Sobel 3×3 对独立噪声的幅值增益:std(gx)=σ·sqrt(12),两方向合成后 E[mag²]=24σ²,
# 故 w=mag_a·mag_b 在纯噪声区的期望约 24σ²。判据②要求证据量显著高于这个底噪才作数。
_NOISE_W_PER_SIGMA2 = 24.0
_MAD_TO_SIGMA = 1.4826
# 尺度下限:8bit 量化本身就带 ~0.29 级的均匀量化噪声,再小的尺度会让相对阈值退化成 0。
_MIN_SIGMA = 0.5
# E[|X|] = σ·√(2/π) = 0.7979σ for X~N(0,σ):由绝对差的均值反解标准差。
_ABSMEAN_TO_SIGMA = 1.0 / 0.7979
# 截断倍数:先用中位数定一个上限,再对截断后的绝对差取均值。4×中位数 ≈ 2.7σ,纯高斯下被截掉
# 的尾部只有 0.7%,偏差可忽略且是固定的(能被 struct_k 吸收)。
_TRIM_FACTOR = 4.0
_TRIM_MIN = 2.0
# 帧间差的方差 = 两帧各自方差之和,故「单帧尺度」= 差分尺度 / √2。
_SQRT2 = 1.4142135623730951

# 「这个区域算不算平坦」的方差下限(灰度级²)。0.25 = 标准差 0.5 级,远低于任何相机的
# 读出噪声,只用来挡真正的数学退化(全同值区域),不承担纹理判定——那是 texture_floor 的活。
_VAR_EPS = 0.25


@dataclass(frozen=True)
class FrameFeat:
    """单帧的光度/结构特征。逐帧算一次,供本帧参与的所有帧对复用。

    ``c2`` / ``s2`` 是**倍角**方向向量 ``(cos2θ, sin2θ)``——用倍角而非 θ 本身,是为了
    对「边缘对比度反转」（梯度方向翻 180°,同一条边）不敏感:那种翻转下 θ 变 π 而 2θ 变
    2π ≡ 0。无梯度处 ``mag≈0`` 使 c2/s2≈0（非单位向量),此时方向内积自然趋 0,再由
    权重 w 压掉,不需要额外分支。
    """

    gray: NDArray[np.uint8]
    f32: NDArray[np.float32]
    mag: NDArray[np.float32]
    c2: NDArray[np.float32]
    s2: NDArray[np.float32]


def frame_features(gray: NDArray[np.uint8], *, blur_ksize: int = 3) -> FrameFeat:
    """灰度帧 → 梯度幅值 + 倍角方向向量。

    blur_ksize > 1 时先做高斯模糊:Sobel 放大噪声,夜间 IR 低信噪比下不压一下,
    结构响应的噪声底会顶到 edge_threshold 附近。blur_ksize<=1 关闭。
    """
    src = gray
    if blur_ksize > 1:
        k = blur_ksize | 1  # 强制奇数,cv2 要求
        src = cv2.GaussianBlur(gray, (k, k), 0)

    f32 = src.astype(np.float32)
    gx = cv2.Sobel(f32, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(f32, cv2.CV_32F, 0, 1, ksize=3)
    gx2 = gx * gx
    gy2 = gy * gy
    den = gx2 + gy2 + _EPS
    return FrameFeat(
        gray=gray,
        f32=gray.astype(np.float32),  # 亮度判据用原图,不用模糊图
        mag=cv2.sqrt(den),
        c2=(gx2 - gy2) / den,
        s2=(2.0 * gx * gy) / den,
    )


class Affine(NamedTuple):
    """仿射光度拟合 ``I_b ≈ α·I_a + β`` 的结果 + 判据①的归一化残差。"""

    alpha: float
    beta: float
    # sqrt(1 − r²):b 的方差中**不能**被 a 的仿射变换解释的比例。0 = 完美光变。
    residual: float


def temporal_scale(
    a: FrameFeat, b: FrameFeat, *, stride: int = 4
) -> float:
    """帧间差的稳健尺度(灰度级)——**所有相对阈值的基准**。

    定义 = 「**截断后**的绝对差均值 / 0.7979」,即「什么都没发生时,同一像素帧间会差多少」的
    稳健标准差估计。

    为什么是「截断均值」而不是直接取中位数:两帧 uint8 之差是**整数**,中位数只能取 0/1/2/…,
    反解出的尺度被量化成 1.48/2.97/4.45… 的台阶(实测 σ₀=2 与 σ₀=4 的合成场景给出同一个
    1.48),而我们正好工作在尺度 2~3 这个区间,阈值会以 ~6 灰度级为步长跳。均值是分数、平滑;
    再用「4×中位数」做上限截断,把真实运动像素的影响钉住(实测繁忙场景也只有 ~19% 像素超阈,
    截断后仍由安静基线主导)。stride 下采样只为省时间。

    # 为什么必须是「帧间差」而不是「单帧空间高频噪声」(真机教训)

    首版用 Immerkær 二阶差分核测单帧空间高频噪声,真机上被证伪:估出 σ=0.5~1.0(一台顶到
    下限),而由「静止场景 4.1% 像素超过 6 灰度级」反解出的真实帧间尺度约 **2.9 灰度级**——
    低估 4 倍。后果是相对阈值体系两头同时坏:响应阈退化成常数(偏松→静止场景也全触发)、
    光变判据阈退化成 ~2 灰度级(偏严→PCT 永不成立、一次都没抑制过)。

    根因:帧在进入判据前经历 ①H.264 解码(编码器自带去噪/量化)②``INTER_AREA`` 720p→448
    降采样(高频被平均掉)。**到判据这一层高频传感器噪声已基本不存在**,残留的「帧间不可约
    变化」主要是编码块效应 + 微小真实运动——这两样二阶差分核看不见。而所有阈值真正要问的
    正是后者。两个量在原始传感器数据上接近,在解码+降采样后差了一个量级。
    """
    d = cv2.absdiff(b.gray, a.gray)[::stride, ::stride]
    cap = max(_TRIM_MIN, _TRIM_FACTOR * float(np.median(d)))
    trimmed = float(np.mean(np.minimum(d, cap)))
    est = trimmed * _ABSMEAN_TO_SIGMA
    # 逐字节几乎相同的帧对(解码器重发同一帧)不携带噪声信息 → 返回 0 = 「无信息」,由调用方
    # 从候选集里剔除。否则取最小值时这种帧对会把基准钉在下限、阈值塌到绝对下限。
    return est if est >= _MIN_SIGMA else 0.0


def noise_sigma(f32: NDArray[np.float32]) -> float:
    """单帧空间高频噪声 σ(灰度级)。**仅作诊断,不再当阈值基准**——见
    :func:`temporal_scale` 的说明(它测的量与判据需要的量差一个数量级)。保留是因为
    「传感器噪声」与「编码+微动底噪」分开看对定位问题有用。

    **为什么整套阈值必须相对 σ 而不是绝对灰度级**:仿射拟合的归一化残差 ``sqrt(1−r²)``
    会把「噪声」和「真实结构变化」混为一谈——两者都降低相关性。真机上每帧噪声是**独立**
    的,故一块纹理区在纯光变下的 r² 也只有 ``SNR/(SNR+1)``:std 30 的纹理配 σ=6 的噪声
    就已经给出 0.2 的残差,而「有人走进画面」实测也在 0.26 量级——用绝对阈值这两者根本
    分不开。改成「拟合残差 ≤ k·σ」后判据变成「残差是否已被噪声完全解释」,量纲自洽,且
    昼夜 SNR 差异自动被吸收(夜间 σ 大 → 阈值自动放宽),不必为此再手调一遍参数。

    做法 = Immerkær 二阶差分核 + 中位数绝对偏差。核对平滑区响应为零,中位数使强边缘不把
    估计带高。
    """
    lap = cv2.filter2D(f32, cv2.CV_32F, _NOISE_KERNEL)
    mad = float(np.median(np.abs(lap)))
    return max(_MIN_SIGMA, _MAD_TO_SIGMA * mad / _NOISE_GAIN)


def affine_fit(
    m_a: float, m_b: float, m_aa: float, m_ab: float, m_bb: float,
    *, lo: float = 0.2, hi: float = 5.0,
) -> Affine:
    """由五个一二阶矩求仿射拟合。区域尺度无关(全帧 / 分块 / 候选区共用)。

    用最小二乘而非鲁棒估计:LS 对「区域内有真实前景」不鲁棒 → 拟合被带偏 → 残差变大 →
    **不判光变 → 放行**。鲁棒估计反而会稳稳拟合背景、把前景留成离群点而仍可能判光变。
    在「优先不误拦」(D2)下,LS 的这个不鲁棒性正是我们想要的方向。

    退化处理(承重,别简化掉):

    - ``Var(a) ≈ 0``(变化前是平坦区):α 无从估计 → 固定 α=1、只拟合偏移 β,这样「平坦
      场景整体变亮」仍被完整吸收、响应趋零。
    - 一边有对比度另一边没有(物体盖住了整片纹理,或反之):**无法**用仿射解释 → 残差
      取 1.0(满值),坚决不判光变。
    - 两边都平坦:没有需要解释的东西 → 残差 0.0,是否判光变交给纹理下限护栏③。
    """
    var_a = max(0.0, m_aa - m_a * m_a)
    var_b = max(0.0, m_bb - m_b * m_b)
    cov = m_ab - m_a * m_b

    if var_a <= _VAR_EPS and var_b <= _VAR_EPS:
        return Affine(1.0, m_b - m_a, 0.0)
    if var_a <= _VAR_EPS or var_b <= _VAR_EPS:
        alpha = 1.0 if var_a <= _VAR_EPS else float(np.clip(cov / var_a, lo, hi))
        return Affine(alpha, m_b - alpha * m_a, 1.0)

    alpha = float(np.clip(cov / var_a, lo, hi))
    r2 = (cov * cov) / (var_a * var_b)
    return Affine(alpha, m_b - alpha * m_a, float(np.sqrt(max(0.0, 1.0 - r2))))


def frame_moments(
    a: FrameFeat, b: FrameFeat
) -> tuple[float, float, float, float, float]:
    """全帧的 (m_a, m_b, m_aa, m_ab, m_bb)。"""
    af, bf = a.f32, b.f32
    n = float(af.size)
    return (
        float(np.mean(af)),
        float(np.mean(bf)),
        float(np.dot(af.ravel(), af.ravel()) / n),
        float(np.dot(af.ravel(), bf.ravel()) / n),
        float(np.dot(bf.ravel(), bf.ravel()) / n),
    )


def orientation_agreement(
    a: FrameFeat, b: FrameFeat
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """帧对 → (倍角方向内积 cos2d, 方向权重 w)。判据②(纹理保持)的逐像素分子/分母。

    ``w = mag_a·mag_b``:梯度幅值**乘积**。用乘积(对边缘强度是二次的)而非 ``min``:全帧
    的噪声级梯度数量远多于真实边缘,线性权重下它们的随机方向会把区域 NCC 整体拖低,导致
    连纯光变都过不了判据②。乘积权重让强边缘按平方主导。

    幅值本身随光照缩放,但 NCC 是 ``Σw·cos / Σw`` 的比值,权重的整体缩放会约掉,故不需要
    对 w 做 α 补偿。
    """
    cos2d = a.c2 * b.c2 + a.s2 * b.s2
    w = a.mag * b.mag
    return cos2d, w


def response(
    a: FrameFeat, b: FrameFeat
) -> NDArray[np.uint8]:
    """结构响应 = 未补偿的逐像素亮度差 ``|I_b − I_a|``(灰度级)。

    **不在这里做光照补偿**,这是一次踩过的坑:用全局仿射参数补偿后再取残差,在「半房间被
    照亮」这类局部光变下,全局拟合是两半的折中,于是**连没变的那一半也被补出巨大假响应**,
    整帧超阈。

    正确的分工是:仿射补偿只是一个**假设**,只出现在 PCT 的判据里;响应如实呈现「哪里变
    了」,由 PCT 在全帧/分块两个尺度上判定哪些变化能被光照解释并剔除。这样未变区域恒零,
    多尺度判定才有干净的作用对象。
    """
    return cv2.absdiff(b.gray, a.gray)


@dataclass(frozen=True)
class PctResult:
    """PCT 判定结果 + 三个判据的原始值（全部落 trace,阈值靠它们标定）。"""

    is_light: bool
    alpha: float
    beta: float
    # 残差 / 纹理都同时给「相对基准的倍数」与「绝对灰度级」:相对值用于判定,绝对值用于标定
    # (阈值系数只能从绝对量的真实分布上定,首版只落相对值导致标尺一坏就无从反推)。
    residual: float
    resid_abs: float
    ncc: float
    texture: float
    texture_abs: float


@dataclass(frozen=True)
class PctThresholds:
    """PCT 三判据阈值。同一组在不同尺度上取值可以不同,故显式传入而非读全局。

    ``residual_k`` / ``texture_k`` 是**噪声 σ 的倍数**而非绝对灰度级,见
    :func:`noise_sigma` 的说明;``ncc_tau`` 是相关系数,本身已无量纲。
    """

    residual_k: float
    ncc_tau: float
    texture_k: float
    neighbor_k: float
    # 允许按光变抑制一个块的最小响应覆盖率。响应铺满整块 = 光变的形状;只占一角 = 物体的
    # 形状。见 :func:`pct_blocks` 的说明。
    light_coverage_min: float = 0.7
    # 判据②生效所需的最小证据量(× 纯噪声区的期望权重)。低于此值说明区域内没有可信边缘,
    # ncc 是随机数 → 取 1.0 让②弃权。
    ncc_evidence_k: float = 2.0


def pct_from_moments(
    *,
    m_a: float,
    m_b: float,
    m_aa: float,
    m_ab: float,
    m_bb: float,
    m_w: float,
    m_wcos: float,
    scale: float,
    thr: PctThresholds,
) -> PctResult:
    """由区域内的矩算 PCT。尺度无关——全帧、分块、候选区都走这里。

    判为光变 = ①∧②∧③:

    - ① **仿射一致**:``I_b ≈ α·I_a + β`` 两参数拟合的残差 RMS ≤ 期望噪声残差,即「残差已被
      噪声完全解释」。期望值 = ``residual_k · scale · sqrt((1+α²)/2)``:两帧噪声独立、拟合把
      a 帧噪声乘了 α,而 ``scale`` 量的是两帧之差(方差为单帧的 2 倍),故除以 2 再乘 (1+α²)。
      不做这个 α 修正,变亮(α>1)会系统性比变暗更易被判成真实变化。基准量见
      :func:`temporal_scale`。
    - ② **纹理保持**:梯度方向 NCC > ncc_tau。**必须门槛**——①单独会把「均匀亮度且比背景
      暗」的单色物体(灰夹克的人 / 灰调宠物 / 单色上衣)判成阴影而漏报,这正是「宁误不漏」
      要防的(方案 §十二 12.2 问题1)。
    - ③ **纹理下限护栏(双侧)**:变化前**与变化后**的亮度标准差都 ≥ ``texture_k · scale/√2``
      (单帧尺度)。低
      SNR 区(光滑墙/地板)上 ② 是**空洞断言**、① 也无从判别 → 禁止据此判光变 → 保守放行
      (符合 D2)。
      **必须双侧**,这是一个真实漏报机制:均匀物体盖住一个纹理块时,变化后 ``std_b ≈ σ``
      (结构没了),而残差 ``std_b·sqrt(1−r²)`` 被这个极小的 std_b 缩掉 → 拟合用 α→0 加一个
      大 β「把整块压成常数」就能通过①,相关性 r²≈0 这个事实被完全掩盖 → 人身上的块被判成
      光变而漏报。要求变化后也有结构,才算「纹理保持」这件事可验证。
      ⚠️ **夜间档故意把 texture_k 压到 0**(方案 §8.2)——夜间目标是拦住大多数窗口,
      要弱化这道保不漏的护栏。这是全参数表里唯一昼夜方向相反的一项。

    m_w 趋 0(区域内两帧都几乎无边缘)时 ncc 无意义,取 1.0 = 「视作纹理保持」,即倾向判
    光变。白天由 ③ 挡住不会走到这里;夜间③已放开,此时倾向拦截正是 D4 要的方向。
    """
    aff = affine_fit(m_a, m_b, m_aa, m_ab, m_bb)
    var_b = max(0.0, m_bb - m_b * m_b)
    resid_rms = float(np.sqrt(max(0.0, var_b * aff.residual * aff.residual)))
    # 纯光变下残差的期望不是 σ 而是 sqrt(1+α²)·σ:两帧噪声独立,而拟合把 a 帧的噪声乘了
    # α。不做这个归一化,变亮(α>1)会系统性地比变暗更容易被判成真实变化——实测 α=1.6 的
    # 全局变亮残差 2.2σ、α=0.55 的变暗只有 1.2σ,同一个阈值必然一边漏一边误。
    frame_scale = scale / _SQRT2
    noise_scale = scale * float(np.sqrt((1.0 + aff.alpha * aff.alpha) / 2.0))
    # 判据②的证据量门控:平坦区的梯度全是噪声,方向内积是随机数。此时 ncc 不该有发言权,
    # 否则「平坦房间整体开灯」会被②否掉而误报。证据不足 → 取 1.0(视作纹理保持,把裁决
    # 权交给①③)。
    ncc = (
        (m_wcos / m_w)
        if m_w > thr.ncc_evidence_k * frame_scale * frame_scale * _NOISE_W_PER_SIGMA2
        else 1.0
    )
    texture = float(np.sqrt(max(0.0, m_aa - m_a * m_a)))
    texture_b = float(np.sqrt(var_b))
    floor = thr.texture_k * frame_scale
    is_light = (
        resid_rms <= thr.residual_k * noise_scale
        and ncc > thr.ncc_tau
        and texture >= floor
        and texture_b >= floor
    )
    return PctResult(
        is_light=is_light,
        alpha=aff.alpha,
        beta=aff.beta,
        residual=resid_rms / max(noise_scale, _MIN_SIGMA),
        resid_abs=resid_rms,
        ncc=float(ncc),
        texture=texture / max(frame_scale, _MIN_SIGMA),
        texture_abs=texture,
    )


def pct_frame(
    moments: tuple[float, float, float, float, float],
    cos2d: NDArray[np.float32],
    w: NDArray[np.float32],
    scale: float,
    thr: PctThresholds,
) -> PctResult:
    """PCT @ 全帧尺度(S1)。判光变 → 本帧对整帧丢弃、不产生候选。

    「人贴镜头占 >50%」与「整幅被照亮」的裸像素签名相同(arXiv:1901.03577),但前者纹理
    被替换 → ② 的 ncc 低、① 的残差高 → 不判光变 → 照常放行。**故不需要给大面积前景开
    size 豁免**,也不需要让不可靠的比值判据独任「判错即丢整帧」这个高危岗。

    moments 由 :func:`frame_moments` 给出——响应图已经用同一组矩算过仿射参数,这里复用。
    """
    m_a, m_b, m_aa, m_ab, m_bb = moments
    return pct_from_moments(
        m_a=m_a, m_b=m_b, m_aa=m_aa, m_ab=m_ab, m_bb=m_bb,
        m_w=float(np.mean(w)), m_wcos=float(np.mean(w * cos2d)),
        scale=scale, thr=thr,
    )


# 分块尺度上「可信邻块的光照变换也能解释本块平均亮度」的容差(灰度级)。
_NEIGHBORS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def pct_blocks(
    a: FrameFeat,
    b: FrameFeat,
    cos2d: NDArray[np.float32],
    w: NDArray[np.float32],
    scale: float,
    thr: PctThresholds,
    grid: int,
    resp_mask: NDArray[np.uint8],
) -> tuple[NDArray[np.bool_], NDArray[np.float32], NDArray[np.float32]]:
    """PCT @ 分块尺度(S2),grid×grid 网格。返回 (光变块掩码, 各块残差/σ, 各块 ncc)。

    拦的是**局部结构化光变**——半房间被照亮 / 窗帘拉开 / 云过 / 隔壁开灯 / 走廊灯亮。
    初稿把光变判据拆成「全帧漂移门 + 候选区阴影鉴别器」两套,这类变化既不够整帧均匀、
    候选区又常落在平坦墙面,**两套之间的中间尺度成了盲区**;而它在室内固定机位下不是
    边角情形。本尺度就是为补这个盲区而存在(方案 §十三 13.5)。

    七个矩用 ``INTER_AREA`` 缩到 grid×grid 一次拿到——INTER_AREA 求的正是区域均值,
    等价于分块求和再除面积,但走 SIMD、比 python 分块循环快一个量级。

    # 低 SNR 块的「光照变换预测」救援(实现期新增,方案原文没有)

    判据③在分块尺度**必须**保留:一个完全落在均匀物体内部的块,变化前后都低对比、①②都
    无从判别,与「平坦墙面被均匀照亮」在块内**完全同签名**——没有③就会把人身上的块判成
    光变而漏报。

    但只有③会让「有大片平坦墙面的房间被局部照亮」永远拦不住:亮起来那半边的纹理块被判
    光变,平坦墙块却因③不达标而存活,响应图剩一堆墙块 → 误报。这正是方案自己承认的「平坦
    区纹理判据空洞」在分块尺度的显形,而室内到处是平坦墙面/地板。

    救援规则:对每个**可信**块(①∧②∧③ 全过)取其拟合出的光照变换 ``(α_t, β_t)``,若某个
    低 SNR 块的**均值与标准差同时**被该变换预测中(各自误差 ≤ ``neighbor_k·σ``),则该块
    一并判为光变。不限邻域——一次光照事件常覆盖大片区域,而中间可能整片都是无纹理的墙,
    逐跳传播传不过去;而「被某个具体的光照变换定量预测中」本身已是很强的约束。

    方向是安全的:物体的亮度与光照变换无关,不会被预测中(除非恰好落在 ``neighbor_k·σ``
    这个窄窗内);同时要求 std 也被预测中,进一步排除「均值恰好撞上」。

    # 覆盖率门控:抑制的最后一道保险

    光照变化让**整块**都响应;物体只占块内一角。所以只有当块内响应覆盖率 ≥
    ``light_coverage_min`` 时才允许按光变抑制该块。

    这条不是锦上添花——没有它,被物体**部分覆盖**的块会被救援吃掉:一个 Δ=8 灰度的低对比
    物体覆盖某块 30%,该块均值只抬 2.4 级,正好落进 ``neighbor_k·σ`` 容差内而被判光变,于是
    物体从边缘被一点点啃掉、剩下的碎块过不了面积/紧凑度 → 漏报。与全帧尺度那条同源:
    **覆盖率决定这个假设可不可检验**。

    仍存的边界:**整间房全平坦、没有任何可信块**时无从救援(如摄像头正对一面白墙),此时
    局部光变会误报——但注意旧的裸帧差 gate 在同样场景也误报,故不构成回归,且按 D2
    「误报便宜」可接受。
    """

    def blk(x: NDArray[np.float32]) -> NDArray[np.float32]:
        return cv2.resize(x, (grid, grid), interpolation=cv2.INTER_AREA)

    af, bf = a.f32, b.f32
    m_a, m_b = blk(af), blk(bf)
    m_aa, m_ab, m_bb = blk(af * af), blk(af * bf), blk(bf * bf)
    m_w, m_wcos = blk(w), blk(w * cos2d)

    var_a = np.maximum(0.0, m_aa - m_a * m_a)
    var_b = np.maximum(0.0, m_bb - m_b * m_b)
    cov = m_ab - m_a * m_b
    std_a, std_b = np.sqrt(var_a), np.sqrt(var_b)

    flat_a = var_a <= _VAR_EPS
    flat_b = var_b <= _VAR_EPS
    alpha = np.clip(cov / np.maximum(var_a, _VAR_EPS), 0.2, 5.0)
    alpha = np.where(flat_a, 1.0, alpha)
    beta = m_b - alpha * m_a

    r2 = (cov * cov) / np.maximum(var_a * var_b, _EPS)
    frac = np.sqrt(np.maximum(0.0, 1.0 - r2))
    # 与 :func:`affine_fit` 标量版同构的三档退化:仅一边平坦 → 1(仿射解释不了);两边都
    # 平坦 → 0(没有需要解释的东西,是否判光变交给③)。改一边记得改另一边。
    frac = np.where(flat_a | flat_b, 1.0, frac)
    frac = np.where(flat_a & flat_b, 0.0, frac)
    resid_rms = std_b * frac

    # 与标量版同构:残差按 sqrt(1+α²)·σ 归一(两帧噪声独立,α 放大了 a 帧噪声);
    # ncc 需证据量达标才作数,否则弃权取 1.0。
    frame_scale = scale / _SQRT2
    noise_scale = scale * np.sqrt((1.0 + alpha * alpha) / 2.0)
    w_floor = thr.ncc_evidence_k * frame_scale * frame_scale * _NOISE_W_PER_SIGMA2
    ncc = np.where(m_w > w_floor, m_wcos / np.maximum(m_w, _EPS), 1.0)

    # 块内响应覆盖率:INTER_AREA 求均值 = 该块内超阈像素的比例。
    coverage = cv2.resize(
        resp_mask.astype(np.float32), (grid, grid), interpolation=cv2.INTER_AREA
    )
    suppressible = coverage >= thr.light_coverage_min

    tol = thr.neighbor_k * frame_scale
    floor = thr.texture_k * frame_scale
    trusted = (
        (resid_rms <= thr.residual_k * noise_scale)
        & (ncc > thr.ncc_tau)
        & (std_a >= floor)
        & (std_b >= floor)  # 双侧:见 pct_from_moments 判据③(否则均匀物体压掉纹理块)
    )
    is_light = trusted.copy()

    # 不可信块(①②在低 SNR 下空洞,或双侧纹理有一边不达标)只能靠可信块的光照变换定量预测。
    # 候选集取 ~trusted 全体而非只取低 std_a 的:否则「std_a 刚过门槛但被中等变暗压到
    # std_b 不达标」的块两头不占,既非 trusted 又进不了救援 → 存活成误报。救援判据本身
    # (同时命中均值与标准差)足够强,不需要再加前置筛选。
    candidate = ~trusted
    if candidate.any() and trusted.any():
        for at, bt in zip(alpha[trusted].tolist(), beta[trusted].tolist()):
            predicted = (np.abs(m_b - (at * m_a + bt)) <= tol) & (
                np.abs(std_b - at * std_a) <= tol
            )
            is_light |= candidate & predicted

    # 覆盖率门控:响应不铺满整块的,一律不许按光变抑制(那是物体的形状,不是光的形状)。
    is_light &= suppressible

    return (
        is_light,
        (resid_rms / np.maximum(noise_scale, _MIN_SIGMA)).astype(np.float32),
        ncc.astype(np.float32),
    )


def saturation_mean(bgr: NDArray[np.uint8]) -> float:
    """BGR 帧的平均饱和度(0-255)。IR 灰度成像下 ≈0,是昼夜/IR 模式判定的依据。

    单通道输入（已是灰度）直接返回 0.0——灰度即无色度信息,按夜/IR 处理。
    """
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        return 0.0
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return float(np.mean(hsv[:, :, 1]))
