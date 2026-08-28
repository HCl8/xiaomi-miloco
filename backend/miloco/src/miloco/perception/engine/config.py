"""Perception Engine MVP — Configuration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InputConfig:
    # 帧率旋钮集中在此(+ settings.yaml 的 perception.engine.input 块),不在代码各处散落:
    #   fps      —— 下发/pipeline 帧率 = tracker 帧率(下采后 tracker 逐帧消费全部, 无独立节流)
    #   omni_fps —— 送给 omni 的视频帧率, 独立解耦; omni 已是瓶颈(~3900ms/窗), 不随 fps 涨
    # 关系: omni 视频在 pipeline 内由 fps 帧再下采到 omni_fps(见 pipeline._downsample_for_omni)。
    fps: int = 3
    omni_fps: int = 1
    period_sec: int = 4
    audio_overlap_ms: int = (
        100  # overlap window to reduce audio truncation at boundaries
    )
    # 仅为接住 InputConfig(**engine_cfg["input"]) 的 kwargs（settings.yaml 的 engine.input
    # 默认含此键，config.json 亦可覆盖）。
    # 与 fps / omni_fps 不同：运行时真值由 _get_video_short_edge() 每帧读 settings 字典
    # （见 prompt_builder），不从此 dataclass 字段读——故改分辨率免重启。别照 fps 的先例
    # 在构造前设 config.input.video_short_edge 期望生效，那样会被静默忽略。
    video_short_edge: int = 512
    # media_resolution: 仅 Gemini 生效的「每帧视觉 token 预算」档位。""/"low" = 66 tok/帧
    # (默认、最省);"high" = 264 tok/帧(小目标/文字更清但 4× token)。mimo/qwen 忽略此字段。
    # 实测:小目标清晰度主要由输入像素分辨率(video_short_edge)决定,本档位只控每帧 token 预算,
    # 故默认 low;identity 等细节敏感场景可经 CLI 切 high。运行时由 GeminiAdapter 实时读 settings。
    media_resolution: str = ""


@dataclass
class CropEnhanceConfig:
    """自适应分辨率(Smart Crop)配置。

    默认值来自离线评测结论(原始实验产物未入库,取值依据写在下方各字段注释里)。
    与 InputConfig 不同,本配置**不**注册进
    PerceptionConfig / _create_engine——那样会在建引擎时缓存、失去热更新;运行时由
    crop_enhance.crop_enhance_config_from_settings() 每窗口热读 settings 字典
    (perception.engine.crop_enhance),同 video_short_edge 的模式,改配置免重启。

    激活条件:enabled **且** user_enabled。分两个 key 不是访问控制(本地部署,用户对两个
    配置文件都有全权、拦不住),而是配置层与用途不同:
      · enabled = 发版级开关(随包默认层 settings.yaml,不由 admin API 写)——团队随包关/开
        整个能力、或线上出问题时发一版止损;false 时前端开关随之置灰。
      · user_enabled = 单机用户开关(默认值同样来自随包 yaml;拨 UI「智能裁切增强」时经
        admin API 写进 config.json 覆盖)。
    两层是**按 key 深合并**的(pydantic-settings),所以用户拨过 UI 开关、config.json 里只多
    出 user_enabled 这一个 key 时,enabled 仍继承随包 yaml —— 发版级开关对这些机器照样生效。
    但 CLI 白名单里也有这条(`miloco-cli config set perception.engine.crop_enhance.enabled`),
    它写的是 config.json:哪台机器执行过,之后就固定读 config.json 的值,发版改 yaml 对它无效。
    与 video_short_edge **正交** —— 裁不裁是本配置管,多清晰是 video_short_edge 管,
    互不作废(早期版本曾用 video_short_edge==0 哨兵表达「自适应」,会让用户选的
    768/1080 在回退全景时静默降到 512,已废弃)。
    """

    # 双闸的**产品默认值在随包 settings.yaml(两闸均已放开为 true),不在这里** —— yaml 是合并
    # 的基础层,用户 config.json 缺这一块时继承的是 yaml 的 true。这里的 False 只在连 yaml 都
    # 缺 key 时兜底(理论路径),别拿它当"默认关"的依据。
    enabled: bool = False  # 发版级开关,置 false 时用户开关也不生效
    user_enabled: bool = False  # 单机用户开关(UI「智能裁切增强」,admin API 可写)
    expand_ratio_h: float = 0.40  # crop 区域水平扩展比(适应人形竖长 + 16:9)
    expand_ratio_v: float = 0.30  # 垂直扩展比
    motion_diff_threshold: int = 40  # 帧差分二值化阈值
    motion_min_block_ratio: float = 0.005  # 运动块面积占比下限(<则丢,点状噪声)
    motion_min_fill_ratio: float = 0.20  # 紧凑度 fill_ratio 下限(<则丢,条纹噪声)
    motion_global_drift_ratio: float = 0.50  # 整帧变化占比 >此值 → 全局漂移,丢弃全部运动块
    crop_min_area_ratio: float = 0.10  # crop 面积下限(防小目标过度放大截断主体)
    crop_max_area_ratio: float = 0.49  # crop 面积上限;超过则回退全景(语义上限,见下)
    # 0.49 是「区域大到接近半个画面就没必要裁」的**语义**上限,不是像素预算 —— crop 视频没有
    # 独立的短边配置,它等比缩放到逐轴贴住**同档全景画面**(见 _maybe_encode_adaptive),故编码
    # 像素恒 <= 同档全景、等号仅在区域与帧等比时取到。像素开销由那条几何上限封住,与本值无关。
    #
    # 区域小于目标网格时**放大**(离线实测:720p 源下该情形占 57% 窗口,原生裁切 +0.6pp、
    # 放大后 +7.8pp)。
    # 注:crop 视频帧率不设独立配置——crop 逐帧裁切、不抽帧,时序与全景视频完全一致,
    # 编码必须沿用 packet.frame_info.fps(下采样后真实间隔),否则时长/音画错位。


@dataclass
class GateConfig:
    # 视觉判据抽检频率。⬆ 从 1 提到 2:input.fps=3 时 interval=round(3/2)=2 → 抽检间隔
    # 0.67s(原 1.0s)。**这是全方案对「短事件漏报」最有效也最便宜的一刀**——抬手/挥手
    # 在 1s 采样间隔下会整个闪过、根本没进过差分。代价是每窗多 1-2 个帧对的计算量。
    check_fps: int = 2
    # 仅 visual_algo="diff" 回退路径使用的裸帧差阈值。结构判据路径不读它(有自己的
    # edge_threshold + min_block_ratio),不要把两者混为一个参数。
    change_threshold: float = 0.005

    # =========================================================================
    # visual 结构判据(方案 v1)。设计与依据见 gate/visual_gate.py 模块 docstring 及
    # Tolaria ``miloco/gate-视觉门禁改造方案-真实变动-vs-光变``。
    #
    # ⚠️ 下面所有阈值都是**未标定的起点**,不是实测拐点。方案要求先跑实验 E1(baseline
    # 与误报/漏报成因分解)与 E3(分块粒度),再据线上分布定值。承重的是 edge_threshold /
    # pct_* 这几个,标定前别拿它们的默认值解释线上表现。
    # =========================================================================
    # "structural" = 多尺度 PCT + 结构判据(默认);"diff" = 回退到原裸帧差单阈值。
    # 留回退档是为了线上出问题时不改代码即可回滚,以及给 E1 一个同口径对照基线。
    visual_algo: str = "structural"
    # Sobel 前的高斯核。Sobel 放大噪声,不压一下结构响应的噪声底会顶到 edge_threshold
    # 附近。<=1 关闭。
    struct_blur_ksize: int = 3
    # 响应阈 = max(struct_abs_floor, struct_k · diff_scale)。**基准是帧间差的稳健尺度**
    # (photometric.temporal_scale),不是单帧空间高频噪声——后者是首版的取法,真机上低估 4 倍、
    # 使响应阈退化成常数(偏松:静止场景 4.1% 像素超阈 → 34% 窗误触发)、光变判据阈退化成
    # ~2 灰度级(偏严:PCT 一次都没生效)。相对化的意义在于昼夜 SNR / 不同码流的差异自动吸收。
    #
    # struct_k=4.0 由第一轮真机数据按高斯假设反推。
    #
    # ⚠️ **这个值卡在一个未决的取舍上,不要当成已标定好。** 第二轮实测 |帧间差| 是**重尾**的:
    # 静止机位 4.2% 像素超过 3.7 倍稳健尺度,而高斯只该有 0.02%——尾部重 200 倍。这意味着
    # **任何「k×稳健尺度」都不可能干净分开编码伪影与低对比物体**:
    #   · k=4 → 安静机位仍有 4.2% 像素响应 → 紧凑斑块残存 → 实测 11~26% 窗误触发
    #   · k=6 → 阈值 ≈10~17,安静机位干净得多,但**直接打破「~8 灰度级低对比物体触发」这个
    #     方案写入的设计目标**(试过,test_low_contrast_object_triggered 立刻红)
    # 这是「灵敏度 ↔ 误报率」的产品取舍(= D2 拿到真实数字后的复议),不该由实现私自定,故保持
    # 4.0(偏灵敏侧,与 D2「优先不误拦」一致)。
    #
    # 真正把「编码伪影」与「真实物体」分开的不是幅度而是**空间结构**(形态学 + 紧凑度 + 最小
    # 面积)——实测抑制工作确实全由它们在做。若要继续降误报,该在结构侧下手而非抬 k。
    # resp_p90 / resp_p99 已落 trace,下一轮按真实分位分布定值,别再按倍数猜。
    struct_k: float = 4.0
    # 绝对下限,只在 diff_scale 估得异常小(极干净码流)时兜底,防阈值塌到 8bit 量化量级。
    struct_abs_floor: float = 6.0
    # diff_scale 跨窗跟踪的**双向**限速率(每窗)。无论本窗候选高低,基准最多变动 ±5%。
    # 1.15 而非 1.05:实测重启后跟踪值 1.36 而各帧对实际是 1.92,±5% 要 7 个窗才追上,期间判据
    # 全部偏严。1.15 → 3 个窗收敛,而单窗污染最多顶高 15%、由 scale_max 兜住。
    # 双向而非「快降慢升」:后者会棘轮到「史上最安静那一刻」(真机 53 窗后基准被压到 1.02,
    # 真实尺度约 2~3,导致自适应完全失效)。双向限速跟的是候选值的中位趋势——被大面积变化污染
    # 的窗最多顶高 5%,随后安静窗每窗拉回 5%;真实上升(入夜)约 20 窗(~3 分钟)追上。
    # 跨窗跟踪本身是必需的:大面积变化(全局光变/人贴镜头/相机被移动)时当前帧对的任何统计量
    # 都被污染,逐对估会让尺度暴涨、阈值暴涨、变化反而不可见(实测人贴镜头就是这样漏的)。
    scale_leak: float = 1.15
    # diff_scale 的物理上界(灰度级)。**帧间差的噪声底超过这个量级就不是噪声、是运动**——
    # 真机实测各机位噪声底在 0.5~3.0 之间,故 8.0 在正常运行时从不生效,只用来钉住病态值。
    # 必需:窗内**每个**帧对都含大变化时(冷启动后第一窗恰好开灯/相机被移动/纯色画面整体变亮),
    # min-over-pairs 没有安静帧对可选、又无跨窗历史可依,会直接采纳被污染的候选值。实测纯色
    # 整帧变亮会把尺度顶到 194 → 阈值 777 → 那个变化本身反而不可见,且按 5%/窗要爬 ~94 窗
    # (约 12 分钟)才降回来。
    scale_max: float = 8.0

    # ---- 加性偏移判据(真机主要误报源:HEVC 平滑区重量化带状伪影)----
    # 判「候选块的变化是不是一次纯加性光度偏移」。详见 photometric.additive_offset_test,
    # 那里记了完整的真机证据链与为什么 PCT@分块 抓不住这类(网格与变化区不对齐)。
    # 阈值都由真机数据的分布定,不是猜的。
    #
    # **已启用(2026-08-28),依据 26 窗全量人工核实账本的线上口径实测矩阵**(形态学→连通域→
    # 逐块判,与线上完全一致):
    # - 8 个编码伪影窗里 6 个被判 offset(coh 0.97~1.00 / cv 0.30~0.45 / stdA 9.0~31.4),
    #   7 个真实变化窗全部安全(coh ≤0.29 或 stdA ≥34.7),零误杀;
    # - 当年判死它的是合成用例「人走进」(coh 1.000 / cv 0.315)。真实素材场景(S1)重测:
    #   真实物体 coh 最坏 0.634,与 0.90 阈值余量 0.27——合成场景的人为产物,真机没有对应物;
    # - 底座纹理守卫 stdA≤35 挡住最后一类误杀:真实光度型事件(阴影/反光移动)物理上就是
    #   加性偏移(coh 0.99),但它落在高纹理上(stdA 61.7),被 stdA 单独保住;
    # - 已知残余:低 coh 伪影(coh 0.139 / cv 2.28,1/8)任何 offset 形态都抓不住,是另一物种;
    # - 真实数据仿真(37 个新鲜分歧窗)只压 1 窗(3%),外科手术刀不是散弹枪。
    # 被 offset 全灭的窗落 verdict=offset_only,且在 dump_on_disagree 开启时**单独落盘**
    # 供人工复核首遇漏报。
    offset_test_enabled: bool = True
    offset_coh_min: float = 0.90     # 实测光度型 1.000 / 结构型 ≤0.348,中间 58 样本无一落入
    offset_cv_max: float = 0.80      # 实测光度型 ≤0.394 / 结构型 ≥1.803,取 0.8 留 2 倍余量
    offset_std_delta_k: float = 3.0  # 纯加性偏移保持标准差;× diff_scale 容忍噪声
    offset_std_a_max: float = 35.0   # 底座纹理守卫:伪影长在平滑区(≤31.4),真实光度事件在高纹理(≥34.7)
    offset_min_px: int = 100         # 太小的块统计不可靠 → 不判(放行)

    # ---- 光变抑制的召回回补(2026-08-28)----
    # PCT 光变抑制会把「光漂 + 背景微动」窗整体吞掉:办公室/客厅这类场景里曝光/白平衡慢漂
    # 占主导,人在背景里的挪动只是块内一小部分响应,分块 PCT 把它们连同光变一起判成 light,
    # 连 blocks_rejected 桶都进不去。而旧 gate 靠「0.5% 像素超 25 灰阶」的高面积敏感度恰好
    # 放行这些窗——新 gate 相对旧 gate 出现的唯一系统性召回缺口就是这一片。
    #
    # 回补规则:窗内存在某帧对被光变/无合格块压制,但该对的 legacy_diff_score ≥ floor
    # (旧 gate 会放行)且 struct_ratio < max_struct_ratio(不是整幅光变),则整窗放行,
    # verdict=light_relaxed。
    #
    # max_struct_ratio=0.1 由真机分歧库 40 窗实测:争议带 struct_ratio 0.012~0.045,真正的
    # 整幅光变(α=0.2/0.88/0.91,PCT 既有收益)0.52~0.97,中间 **10 倍空隙**。代价:
    # 争议带里 dpf 盲检约 4 成无实际变化(旧 gate 本来也在消费它们,不产生假事件,只多跑
    # VLM);收益:dpf 确认的真实背景移动不再被拦(D2 方向)。
    light_relax_enabled: bool = True
    light_relax_legacy_floor: float = 0.005      # 旧 gate 的放行阈:legacy 达标才回补
    light_relax_max_struct_ratio: float = 0.1    # 超过视为整幅光变,维持抑制
    light_relax_min_comp_ratio: float = 0.0015   # 抑制前响应里至少要有这么大的连通结构(排除散点噪声)

    # ---- 调试落盘(定位误报用,默认关)----
    # 非空则把**触发窗**的输入落盘:448 灰度抽检帧序列 + 上窗基准帧 + 全分辨率参考帧 + diag。
    # 只存输入不存中间结果——中间各阶段在本地用同一份代码复现,这样改可视化不必重新部署,
    # 且复现路径与线上完全一致。
    gate_debug_dump_dir: str = ""
    gate_debug_dump_max: int = 400  # 上限,防写爆磁盘(每窗约 1MB)
    # 只落「新旧判定不一致」的窗。**这是排查用的默认档**:每次需要人工核实的恰恰是分歧窗
    # (新版拦而旧版放 = 新增漏报;新版放而旧版拦 = 新增触发),实测占比约 3%,一整天约 650 窗。
    # 关掉则落全部窗——标定阈值分布时需要无偏样本,那种场合才关。
    #
    # 为什么需要这一档:``gate_debug_dump_max`` 是全局计数、只在重启时归零,落全部窗时夜里
    # 一小时就用光,之后什么都不落。实测 03:37~06:08 出现的 19 个夜间分歧窗**一个都没留下帧**,
    # 无法人工核实。
    gate_debug_dump_on_disagree: bool = False

    # ---- 持续变化源抑制(方案 v3 的纯 CV 层)----
    # 目标 = **位置固定、几乎每窗都在变**的变化源:OSD 时间水印(秒位每秒跳变)、轻微摆动的
    # 线缆、风扇、屏幕。真机实测这类是安静机位的主要误报源(C700 静止机位 75 窗里 8 次触发,
    # omni 全判「无人、画面静止」;块特征为紧凑竖条/扁条、面积刚过下限、幅度尾部重)。方案 §一
    # 早已把水印/风扇列为「不触发」并归到 v3。
    #
    # 判据**源无关**:不去认「这是水印还是线缆」,只问「这个位置是不是长期都在变」。
    #
    # 机制的两条规则来自方案 §7.2/§7.4,都是踩过的坑,别改:
    #   ① 占空比统计跑在**抑制之前**——否则一旦开始抑制,统计就看不到那个源、自己失去输入。
    #   ② 解除抑制的判据是「**幅度显著偏离该处长期基线**」,不是「占空比高就压得更狠」——后者
    #      方向是反的:人站在水印/风扇前会让该处占空比更高、于是被压得更死,正好压掉要抓的东西。
    # ⚠️ **默认关闭**:真机数据推翻了这一层的前提,详见方案 §十七。
    #
    # 方案 §6.4 假设「风扇近乎 100% 时间在变,人活动是间歇的、占空比远达不到」。cat@mac 实测
    # **完全反过来**:
    #   · C700 静止机位的误报源(水印/摆动线缆)最热块占空比只有 **0.19**——它是「位置固定但
    #     **间歇**」,不是「持续」,duty_thresh=0.85 永远够不着 → 这一层在最需要它的机位零收益。
    #   · 3Pro 繁忙机位最热块占空比 **0.79 且仍在爬**,而那个块是**人坐的位置**——早先观测到的
    #     env_block_ratio=0.063 抑制正是压在人身上,只靠显著性解除才没误杀。
    #
    # 即**占空比判据分不开「位置固定的间歇伪影」与「坐着不动的人」**:两者都空间固定且反复
    # 出现。降 duty_thresh 去接住 0.19 会让繁忙机位压人更狠。按 D2「优先不误拦」故默认关。
    #
    # 代码与测试保留:机制本身(统计在抑制前、显著性解除、面积上限)经真机验证是对的——3Pro 上
    # 抑制生效而通过率仍 100%,证明解除条件有效。要复活这一层,该换判据:固定伪影的**幅度与
    # 范围高度可重复**(跨次方差小),人的则变化大——即跟踪方差而非占空比。这是新的设计题。
    env_mask_enabled: bool = False
    # 占空比 EMA 速率(≈1/记忆窗数)。0.1 → ~10 窗记忆,约 19 窗(2.5 分钟)达到 0.85 阈值。
    # 太慢则新出现的持续源要很久才被压住;太快则人短暂长驻就可能被误当成持续源。
    env_duty_alpha: float = 0.1
    # 判定「持续变化源」的占空比下限。
    env_duty_thresh: float = 0.85
    # 块内响应覆盖率达此值才算「本窗该块活跃」(进占空比统计)。滤掉零星噪点像素。
    env_active_cov: float = 0.25
    # 解除抑制的显著性倍数:本窗该块响应幅度 > 基线×此值 → 判为「不只是那个老源」,照常放行。
    # 1.8 偏保守(宁可解除、宁误不漏);固定源的幅度稳定,很少自发越过。
    env_salience_k: float = 1.8
    # 抑制区总面积上限。超过则整体不抑制——大半画面被判成"持续源"必然是判错了(如相机被移动)。
    env_area_max_ratio: float = 0.40
    # PCT 判据①:仿射拟合残差 RMS ≤ residual_k · diff_scale · sqrt((1+α²)/2) 才算「变化已被
    # 一次光照变换 + 噪声解释」。真机反推:静止机位残差比 ≈1.0(判光变 ✓)、繁忙机位 ≈6.7
    # (不判 ✓),1.6 落在两者之间且靠安静侧,留了余量。
    pct_residual_k: float = 1.6
    # 残差预算的**对比度正比项**。仿射模型自身的近似误差与信号幅度成正比(边缘重采样、编码
    # 量化在高梯度处更大、gamma 非线性),不是噪声。真机实测:高对比块(std≈47)的残差 3.3~7.4
    # 即 r²>0.99 的良好拟合,而纯噪声阈值只有 2.1 → 判据①永不成立、PCT 在真机上从未命中过。
    # 0.15 由那批落盘数据反推(让 r²>0.99 的块通过,而含真实前景的块残差≈std_b 仍被拒)。
    pct_residual_rel: float = 0.15
    # PCT 判据②:梯度方向 NCC 下限。**必须门槛**——判据①单独会把"均匀亮度且比背景暗"
    # 的单色物体(灰夹克的人/灰调宠物/单色上衣)判成阴影而漏报。
    pct_ncc_tau: float = 0.70
    # 判据②生效所需的最小证据量(× 纯噪声区的期望梯度权重 24σ²)。低于此值说明区域内没有
    # 可信边缘、方向内积是随机数 → ②弃权。没有这道门控,「平坦房间整体开灯」会被②否掉。
    pct_ncc_evidence_k: float = 2.0
    # PCT 判据③:纹理下限护栏 = 区域亮度标准差 ≥ texture_k · diff_scale/√2(单帧尺度)。低 SNR
    # 区(光滑墙/地板)上①②都是空洞断言 → 禁止据此判光变 → 保守放行。
    # ⚠️ 夜间档故意把它压到 0,见 night_pct_texture_k。
    pct_texture_k: float = 3.0
    # 低 SNR 块的救援容差(× σ):可信块拟合出的光照变换若能同时预测该块的均值与标准差
    # (各自误差 ≤ 此值×σ),则一并判光变。没有这条,有大片平坦墙面的房间被局部照亮时永远
    # 拦不住。详见 photometric.pct_blocks 的 docstring。
    pct_neighbor_k: float = 1.5
    # 允许按光变抑制一个块的最小响应覆盖率。光变让整块都响应,物体只占块内一角——没有这道
    # 门控,被低对比物体部分覆盖的块会被"救援"从边缘一点点啃掉而漏报。
    pct_light_coverage_min: float = 0.7
    # PCT@分块 的网格粒度(grid×grid)。**16 而不是 8**:光照边界会横切纹理区,产生一条
    # 「块内有两种光照变换」的骑跨带,那些块残差高、必然存活;网格越细,骑跨块越小、越可能
    # 单块过不了 min_block_ratio,连成的细带又会被紧凑度杀掉。合成用例上 8/16/24 都能拦住
    # 轴向半房照亮,而 12 因网格对齐运气失手——这本身就是取更细网格的理由。最终值待 E3
    # 在真实录像上按"拦截率 vs 对真实前景的误伤率"选拐点(误伤不为 0 的粒度直接淘汰)。
    pct_block_grid: int = 16
    # 形态学开闭核(448 掩码上)。crop_enhance 在全分辨率用 7×7,448 上的等比量是 3。
    morph_ksize: int = 3
    # 紧凑度:blob 填不满外接框 → 条纹/散碎噪声。复用 crop_enhance 已标定的取值。
    min_fill_ratio: float = 0.20
    # 单块最小面积占比。⚠️ 它与旧的 change_threshold 同为 0.005 并非巧合也非等价:
    # 单块场景下两者是同一个数,所以"局部化"本身**没有**降低小动作门槛。v1 对漏报的
    # 改善来自 check_fps↑ 与相对噪声阈值,别把它记成漏报修复。
    min_block_ratio: float = 0.005

    # ---- 夜间/IR 档 overrides(方案 §八,D4)----
    # 成本结构分时段:白天"优先不误拦、可以误放";夜间 IR 下 PCT 与结构响应同时退化、
    # 空窗占绝大多数、误报量级压不住 → 反过来提高阈值拦住大多数窗口,接受漏报上升。
    # 已知代价:会同时漏掉夜间入侵/夜起摔倒/宠物活动——若夜间安防或照护是产品职责,
    # 这个交换需要单独评估(方案遗留问题 Q2)。
    night_check_fps: int = 1  # 夜间既然接受漏短事件,降采样省算力
    night_blur_ksize: int = 5  # IR 噪点更重,核放大
    night_struct_k: float = 6.0  # ↑ 最直接的"拦住大多数窗口"杠杆
    night_struct_abs_floor: float = 16.0
    night_min_block_ratio: float = 0.02  # ↑ 只放行较大面积变化
    night_pct_residual_k: float = 3.0  # 放宽 → 更容易判"这是光变" → 更多帧对被拦
    night_pct_ncc_tau: float = 0.50  # 同上
    night_pct_neighbor_k: float = 3.0
    # ⚠️⚠️ **全参数表里唯一昼夜方向相反的一项**。护栏本是"低 SNR 区禁止判光变 → 保守
    # 放行"的保不漏机制;夜间目标是拦,所以要弱化它、接受空洞断言。实现/调参时最容易照抄
    # 白天逻辑而漏掉这一处反向。
    night_pct_texture_k: float = 0.0

    # ---- 昼夜模式判定(全局,非分档)----
    # 依据 = 缩放后 BGR 的平均饱和度(IR 灰度成像下 ≈0)。相机若上报 IR 标志位应优先接
    # 那个信号,饱和度只是没有标志位时的替代。双阈滞回(进夜间用低阈、退出用高阈)+ N 窗
    # 确认,防黄昏在两档间反复翻转导致参数档抖动。
    night_sat_enter: float = 8.0
    night_sat_exit: float = 14.0
    night_confirm_windows: int = 2
    cooldown_ms: int = 1000
    audio_energy_threshold: float = 0.015
    # VAD(silero)门控 speeches 字段:音频过能量 gate 后,再用 VAD 判本窗有没有真人声。
    # 无人声 → 只从 schema 剥掉 speeches(env_sounds / 喂音频照旧),根除模型在键鼠敲击 /
    # 办公底噪上脑补"像指令的话"。纯能量分不开人声与机械瞬态噪声,故独立加 VAD 这道。
    speech_vad_enabled: bool = True
    # threshold=0.4 + min_chunks=3：在 82 幻觉 + 149 真实语音 clip 上实测 FP 4% / FN 5%
    # 的拐点(对话 peak 概率中位 0.998、键鼠噪声 0.10,分得很开)。残留 FP 由下游
    # needs_response 佐证兜底。speech_prob 已落 trace,后续可据线上分布再调。
    speech_vad_threshold: float = 0.4  # 单帧人声概率阈(silero)
    speech_vad_min_speech_chunks: int = 3  # 需 >= 此数的 512 样本帧过阈才判有人声(抗单次咔哒尖峰)
    # visual 滞回时长(秒)。visual 最近通过过、距今 <= 此值时,本窗 visual
    # 不通过也强制生成 packet 并打 hold 标志。0 = 关闭。
    # 默认 90s:过长(如 6min)在画面间歇有变化的场景会持续刷新 last_visual_pass_ts、
    # hold 窗反复续命,期间纯静默窗也被拉起跑下游,浪费 omni 调用。运维侧可在
    # settings.yaml 的 perception.engine.gate.hold_duration_sec 覆盖。
    hold_duration_sec: float = 90.0


@dataclass
class IdentityConfig:
    static_displacement_threshold: float = (
        0.05  # ratio of displacement to bbox diagonal
    )
    crop_padding_ratio: float = 0.2
    static_frame_resolution: str = "high"
    dynamic_frame_resolution: str = "medium"
    tracking_service_mode: str = "mock"  # "mock" | "real" | "deep_sort"（v1.2 加 deep_sort：含 ReID 给陌生人池复用）
    tracking_service_url: str = ""
    # Real tracking service config
    perception_model_dir: str = ""  # empty = auto-detect
    perception_use_gpu: bool = False
    perception_input_width: int = 1280
    perception_input_height: int = 720


# =============================================================================
# IdentityEngineConfig —— omni 身份识别系统总配置
# =============================================================================


@dataclass
class SortConfigDC:
    """SortTracker 调参。

    ``max_age_sec`` 是真实世界秒数；SortTracker 实例化时按 fps 换算成帧数：
        max_age_frames = max_age_sec × fps
    """

    n_init: int = 1
    max_age_sec: float = 1.0  # track 丢失多久（秒）后注销
    iou_threshold: float = 0.3
    detector_conf_threshold: float = 0.5
    track_human_only: bool = True


@dataclass
class DeepSortConfigDC:
    """DeepSORT 跟踪器调参。

    与 ``SortConfigDC`` 同名字段语义对齐;额外加 fast 模式开关与 ReID 抽取频率。
    yaml 仅暴露用户可能调的 7 字段:
        - 跟踪基本:n_init/max_age_sec/iou_threshold/detector_conf_threshold/track_human_only
        - fast 模式:mode/human_reid_skip_windows

    内部走代码默认值的:max_cosine_distance(DeepSORT 标准 0.2) / reid_model_path /
    use_gpu 等部署/接口级配置;static_displacement_ratio / static_min_abs_px 等
    几何固定阈值(位移/对角线比 0.05 + 绝对位移 10px,业务无关,在 TrackerConfig
    里有默认值)。
    """

    # 跟踪基本参数(与 SortConfigDC 同名同义)
    n_init: int = 1
    max_age_sec: float = 1.0
    iou_threshold: float = 0.3
    detector_conf_threshold: float = 0.5
    track_human_only: bool = True

    # fast 模式(静止 track 跳过 ReID 推理)
    mode: str = "fast"                       # "normal" / "fast"
    human_reid_skip_windows: int = 4         # 静止 track 每 N 个 window 才抽一次 ReID


@dataclass
class StabilityConfigDC:
    """Pending State 置信度感知 commit 阈值。

    omni 单次识别可能误判，按 ``best_conf`` 决定需要几次同答才能 commit：
    高置信 1 次即落定、中/低置信各 3 次（越不确定要越多票，阈值单调不降）。
    """

    high_conf_threshold: float = 0.85
    mid_conf_threshold: float = 0.65
    low_conf_threshold: float = 0.50
    commit_threshold_high: int = 1
    commit_threshold_mid: int = 3
    commit_threshold_low: int = 3
    pending_timeout_sec: float = 60.0
    # 重审周期用**秒**标定(墙钟语义), 由 needs_omni_call 入口按 engine_fps 运行时换算成
    # frame_index 帧数(round(sec × fps))——与 max_age_sec 同款"秒标定、运行时换算", 改 fps
    # 自动跟随、墙钟周期不漂移。
    recheck_interval_sec: float = (
        30.0  # confirmed / unknown 状态下重审周期
        # 30s 盲窗的取舍: unknown 状态下太长(如 60s)会"离开回来后卡 unknown 数分钟",
        # 太短则 omni 调用频率高。同时用作 confirmed track 写库**冷却期内**的慢重审间隔。
    )
    # confirmed track 处于"攒写库资格"阶段(非冷却)的快重审间隔, 比 recheck_interval_sec 短,
    # 让"连续 N 次一致"更快凑齐。代价: 攒库阶段 omni 调用更密(仅 confirmed 且非冷却时;
    # 攒满即写并进冷却转慢, 不会持续高频)。
    recheck_interval_accumulating_sec: float = 10.0
    # 身份库(persons)为空时 unknown track 的重派慢周期（秒）。库空=没有任何成员可匹配，
    # unknown 常规 30s 重派唯一价值（等人转身/走近再匹配成员）恒为 0 → 放慢到此周期，只保留
    # 周期性给 omni 翻 no_person 的机会（静物误检的延迟纠正），不破坏"永不永久静音"不变式。
    # 只作用于 unknown；pending 不放慢（no_person 落定靠 pending 期连续投票，见 needs_omni_call）。
    # 库非空自动回落到 recheck_interval_sec。按 engine_fps 换算成帧，与其它周期同款秒标定。
    gallery_empty_recheck_sec: float = 600.0
    hysteresis_unmatched_count: int = 2
    # 时序一致性写库门 (tier_c 污染修复): 同一 person_id **连续 N 次 confirmed 重审一致**
    # 才允许写入 tier_c。把"显示用 commit"(灵敏, conf-aware 1/2/3 次) 跟"写库资格"(严格)
    # 解耦, 挡偶发高置信误判 / ID-switch 残留。严格连续: 中途 None(弃权) / 矛盾 / coasting
    # 任一打断即清 0 重攒; commit(pending→confirmed) 时也清 0, 保证攒库发生在确认之后。
    write_eligible_min_count: int = 6
    # tier_c 写库冷却: 攒满 write_eligible_min_count 写一张后进入冷却期不再晋升,
    # 冷却时长(秒) = mult × write_eligible_min_count × 快重审间隔(秒) = 2×6×10 = 120s
    # (engine 内按 engine_fps 换算成帧计冷却; 秒标定、改 fps 不漂移)。
    # 冷却期内: 慢重审验身份(矛盾仍能退回)、不写、不攒(write_eligible 冻结在 0)。
    tier_c_cooldown_mult: int = 2

    # 翻转专用 commit 阈值: 仅"由 confirmed 退回的 pending"重确认用, 比首次严防误翻。
    # 首次识别仍用上面的 commit_threshold_*(高1/中3/低3), 保高置信单票快速上名。
    flip_commit_threshold_high: int = 2
    flip_commit_threshold_mid: int = 2
    flip_commit_threshold_low: int = 3
    # 翻转黏滞: 退回 pending 后显示保持旧名的最大重审窗数; 超了仍未定则放手交还正常状态机。
    flip_sticky_max_recheck: int = 2


@dataclass
class DispatchConfigDC:
    """识别派发节流参数（避免每帧调 omni 浪费 token）。"""

    min_interval_sec: float = 5.0
    max_queries_per_call: int = 4
    stale_threshold_sec: float = 30.0
    max_retries: int = 1


@dataclass
class GalleryConfigDC:
    """omni prompt 中 gallery snapshot 渲染参数。

    每人渲染 1 张 body composite + 1 张 face composite 给 omni 做识别参考；
    人脸用于精准匹配，全身用于体型 / 衣着辅助。
    """

    body_refs_per_person: int = 3
    face_refs_per_person: int = 3
    library_root: str = "data/identity_lib"


@dataclass
class StrangerConfigDC:
    """陌生人编号策略。

    默认 false：不维持陌生人身份编号；所有未识别目标归为 ``"unknown"``。
    设 true 时启用 ``unknown_<n>`` 自增编号；仅 ``tracking=track_based`` 时合法
    （track_free 没有 track_id 持久性，无法稳定分配编号，启动会报错）。
    """

    distinguish: bool = False


@dataclass
class TierCClearConfig:
    """tier_c 闲时定期清:每晚低活跃窗口整池清空该相机 tier_c(对错都清),把污染寿命压到
    ≤1 天 + 让 tier_c 只反映"今天"的外观。清空 = 退纯 tier_a(安全态)。

    默认 ``require_absence=False``:凌晨到点**无条件清**(不判在场)。代价:若有人在场,
    其当天好样本也一并清掉,需重新累积。置 True 恢复旧"确认无人"漏斗(mtime/gate 静默 +
    live 检测收口),见 ``api._tierc_clear_tick``。
    """

    enabled: bool = True
    # False(默认):到点无条件清。True:走"确认无人"漏斗(下方三项仅此模式生效)
    require_absence: bool = False
    # 清理时间窗(本地 24h):[start_hour, end_hour) 内才评估。无条件模式在窗起点(凌晨3点)首轮即清一次。
    window_start_hour: int = 3
    window_end_hour: int = 5
    poll_interval_sec: int = 3600        # 窗内轮询间隔(60min);幂等键=日历日,同日窗每相机
                                         # 每晚至多清一次(跨午夜窗按日历日各算一次,但二次必遇
                                         # 空池前置→标记跳过,详见 api._tierc_clear_tick)
    # —— 以下三项仅 require_absence=True(确认无人模式)时生效 ——
    # 前置过滤:该相机所有 person 的 tier_c 最新写入 mtime ≥ 此秒数(近期没在写)
    pool_quiet_sec: float = 1800.0
    # 前置过滤:该相机距上次 visual gate pass ≥ 此秒数(近期画面无变化)
    gate_quiet_sec: float = 600.0
    # 收口:主动拉一帧跑检测,无 conf≥此值的人才判"确无人"(唯一能抓静止人的闸)
    detect_person_conf: float = 0.8


@dataclass
class DriftCheckConfigDC:
    """Track 身份漂移自检参数（commit 后人物交叉/交互致 track 跟错人的纠正安全网）。

    每窗对每个"已绑成员的 confirmed track"，用该 person 近期同摄 TierC body 质心
    作参考，比对 track 当前外观质心(cos)；累计 ``consecutive_windows`` 个低窗
    （``sim < threshold``；无数据窗不计不清）入嫌疑集。三档（``mode``）：

      - ``enforce`` —— 撤嫌疑集回 pending 丢回 omni 重判 + 采信复认护栏（默认）。
      - ``observe`` —— 只算 sim + 打 ``[Identity/drift]`` 日志、不撤（调试 / 抓阈值分布）。
      - ``off``     —— 完全不算、不打、不撤。

    全程 body-only、零额外推理（复用 tracker 已有 ReID 特征 + 库里已落盘 .npy）。
    """

    mode: str = "enforce"              # "off" | "observe" | "enforce"
    recency_sec: float = 900.0         # 参考样本时间窗 15min（跨天旧外观对今天不可比）
    threshold: float = 0.55            # sim < 此值视为不同人；0.55 = 该 ReID 模型区分"绝对不同人"的既定经验阈值(误撤极少)
    consecutive_windows: int = 2       # 累计 M 个低窗才入嫌疑集（无数据窗不计不清）
    min_track_emb: int = 3             # track 质心要求最少 emb 数（不足跳过，不拿噪声质心误判）


@dataclass
class NoPersonConfigDC:
    """无人误检（no_person）抑制参数。

    检测模型偶尔把静态杂物 / 家具误报成人体框 → 形成 track → 注入 omni → caption 据框
    脑补"陌生人在做某事"。omni 在 identities 里把"框内确无人"判为 ``no_person``（区别于
    ``unknown``=有人但不认识，见 ``field_registry``），本配置控制引擎侧如何消费该判定：
    连续 ``vote_threshold`` 次 no_person 才落定 ``no_person`` 状态（停派发 omni、身份不导出、
    不进陌生人池）。

    召回保护偏"少误判无人"——落定后有三重解除通道，确保任何 track 都不会被永久静音：
      1) 落定需连续 ``vote_threshold`` 票（防单次误判掉背对 / 遮挡的真人）；
      2) 框相对落定锚点明显移动 → 立即回 pending（会动的真人即时自愈，走
         ``clear_no_person_on_motion``）；
      3) 每 ``no_person_recheck_sec`` 放行一次慢重审，重审判到人即回 pending（走
         ``clear_no_person_to_pending``）。这条是最后兜底的"不永久卡死"自愈不变式，非主恢复
         通道：只为极少数"未注册真人一动不动、被连判两票误压、又不触发移动解除"的残留兜底。
         注意急性摔倒 / 倒地动作由上游动作 / 事件检测捕获、不依赖本状态，故此周期可放得很长。
    """

    enabled: bool = True
    # 连续命中 no_person 多少次才落定状态（防单次误判把背对 / 遮挡的真人误当无人）
    vote_threshold: int = 2
    # 落定后"明显移动"判据：中心位移 ≥ min_abs_px **且** ≥ ratio×bbox 对角线，两者同时满足才算移动
    motion_clear_displacement_ratio: float = 0.15
    motion_clear_min_abs_px: float = 30.0
    # 慢重审自愈周期（秒）：落定后每此秒数放行一次 omni 重审，判到人即回 pending。这是"任何状态
    # 都不会被永久卡死"的最后兜底（主恢复靠上面的移动解除 + 上游动作检测），故可放长；
    # needs_omni_call 入口按 engine_fps 换算成帧。默认 1200s(20min)，现场想更懒可调 1800/3600。
    no_person_recheck_sec: float = 1200.0

    # reject_region（P3，默认关）：把 no_person 的屏幕区域登记为"禁新建 track 区"，
    # 直到真人 track 与之明显重叠或区域 TTL 到期才失效。默认关闭——需现场验证后再开
    # （误开会挡住门口 / 座位等真人常现位置的新 track）。
    reject_region_enabled: bool = False
    reject_region_ttl_sec: float = 300.0      # 区域登记后多久自动过期失效
    reject_region_clear_iou: float = 0.3      # 真人 track 与禁区 IoU ≥ 此值即解除该禁区


@dataclass
class IdentityEngineConfig:
    """omni 身份识别系统总配置。"""

    enabled: bool = True

    # 跟踪策略：track_based 维持跨帧身份连续性；track_free 暂未实施
    tracking: str = "track_based"  # "track_based" | "track_free"
    # omni 调用方式：
    #   "fused"    —— 主调用同时输出 environments / speeches / identity_assignments
    #   "separate" —— 占位，未实施
    omni_call_mode: str = "fused"

    # 子组件
    sort: SortConfigDC = field(default_factory=SortConfigDC)
    deep_sort: DeepSortConfigDC = field(default_factory=DeepSortConfigDC)   # v1.2 新增
    stability: StabilityConfigDC = field(default_factory=StabilityConfigDC)
    dispatch: DispatchConfigDC = field(default_factory=DispatchConfigDC)
    gallery: GalleryConfigDC = field(default_factory=GalleryConfigDC)
    stranger: StrangerConfigDC = field(default_factory=StrangerConfigDC)
    tierc_clear: TierCClearConfig = field(default_factory=TierCClearConfig)
    drift_check: DriftCheckConfigDC = field(default_factory=DriftCheckConfigDC)
    no_person: NoPersonConfigDC = field(default_factory=NoPersonConfigDC)   # 无人误检抑制

    # crop / 累积
    body_crop_padding_ratio: float = 0.05
    tier_c_accumulate_on_commit: bool = True
    # 写 tier_c 前用 omni 做一次"待入库样本 vs 该成员权威库(tier_a)"的同人校验(设计文档 E7),
    # 挡非本人误判落库。默认开;也是 tier_c 回喂 gallery(C3)的前提。需配合注入 omni_config,
    # 开了却没注入则降级为仅 pHash 把关(启动打 ERROR, 不崩溃)。
    tier_c_verify_enabled: bool = True
    # 上述校验的放行门:omni 判 same_person 且 confidence >= 此值才写库。
    tier_c_verify_conf_threshold: float = 0.8

    # response 中低于此置信度的 person_id 强制视作 unknown，防 omni 幻觉
    confidence_cutoff: float = 0.5

    # 暂未启用的开关（对应功能尚未实施）
    ensemble_grounding: bool = False  # ensemble grounding 兜底
    body_attr_text: bool = False  # 人体特征文本
    alias_frequency: bool = False  # AliasFrequencyTracker（track_free 专用）


@dataclass
class OmniConfig:
    model: str = "xiaomi/mimo-v2.5"
    api_key: str = ""  # Set via MILOCO_MODEL__OMNI__API_KEY env var or config
    base_url: str = "https://api.xiaomimimo.com/v1"
    max_completion_tokens: int = 512
    temperature: float = 0.1  # 低温更利于结构化感知判定：实测 grounded prompt 下温度越低误报/坏 JSON 越少
    top_p: float = 0.95
    timeout: float = 30.0
    stream: bool = False


@dataclass
class PerceptionConfig:
    input: InputConfig = field(default_factory=InputConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    omni: OmniConfig = field(default_factory=OmniConfig)
    identity_engine: IdentityEngineConfig = field(default_factory=IdentityEngineConfig)


# =============================================================================
# 配置加载 helper：dict（来自 yaml）→ 嵌套 dataclass
# =============================================================================


def identity_engine_config_from_dict(d: dict | None) -> IdentityEngineConfig:
    """把 settings.yaml 中 ``perception.engine.identity_engine`` 字典转为
    嵌套 dataclass 树。缺失字段走 dataclass 默认值，未知字段被忽略（向前兼容）。
    """
    if not d:
        return IdentityEngineConfig()

    def _filter(cls, sub: dict | None) -> dict:
        """只保留 cls 定义的字段；丢弃 yaml 里 unknown key。"""
        if not sub:
            return {}
        from dataclasses import fields

        valid_keys = {f.name for f in fields(cls)}
        return {k: v for k, v in sub.items() if k in valid_keys}

    sub_factories = {
        "sort": SortConfigDC,
        "deep_sort": DeepSortConfigDC,    # v1.2 主动注册改造 · 漏加导致 yaml 走 dict 不转 dataclass
        "stability": StabilityConfigDC,
        "dispatch": DispatchConfigDC,
        "gallery": GalleryConfigDC,
        "stranger": StrangerConfigDC,
        "tierc_clear": TierCClearConfig,
        "drift_check": DriftCheckConfigDC,
        "no_person": NoPersonConfigDC,
    }
    kwargs: dict = {}

    # 顶层 scalar 字段
    from dataclasses import fields

    top_level_keys = {f.name for f in fields(IdentityEngineConfig)}
    for k, v in d.items():
        if k in top_level_keys and k not in sub_factories:
            kwargs[k] = v

    # 嵌套子 dataclass
    for k, cls in sub_factories.items():
        kwargs[k] = cls(**_filter(cls, d.get(k)))

    return IdentityEngineConfig(**kwargs)
