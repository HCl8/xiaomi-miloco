"""Tests for Gate Layer — Visual Gate（结构判据 + 多尺度 PCT）。

判定语义已从「变了多少像素」换成「有没有真实变动」,所以本文件的用例必须用**有纹理 +
每帧独立噪声**的合成场景:纯色无噪的方帧会同时落进「零方差」和「零饱和度 → 夜间档」两个
退化分支,测不出任何真实行为。噪声独立是关键——真机上每帧噪声不相关,而一切阈值都是相对
噪声 σ 定的。
"""

import cv2
import numpy as np
import pytest
from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate import visual_gate as visual_gate_module
from miloco.perception.engine.gate.visual_gate import (
    _DIFF_SIZE,
    MODE_DAY,
    MODE_NIGHT,
    VERDICT_BLOCKS_REJECTED,
    VERDICT_COLD_START,
    VERDICT_ENV_SUPPRESSED,
    VERDICT_LIGHT_BLOCKS,
    VERDICT_LIGHT_FRAME,
    VERDICT_MODE_SWITCH,
    VERDICT_TRIGGER,
    VisualBaseline,
    _preprocess,
    compute_frame_diff,
    evaluate_visual,
)

SIGMA = 4.0  # 合成噪声标准差,贴近真机 720p 子码流量级


def _solid_frame(r: int, g: int, b: int, w: int = 100, h: int = 100) -> np.ndarray:
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = [b, g, r]  # BGR
    return frame


def _room(h: int = 720, w: int = 1280) -> np.ndarray:
    """无噪的室内底图:墙 + 地 + 两件家具(提供纹理,PCT 判据②③ 才有作用对象)。"""
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :] = (90, 100, 110)
    img[: h // 2] = (140, 145, 150)
    cv2.rectangle(img, (100, 400), (400, 600), (60, 70, 90), -1)
    cv2.rectangle(img, (700, 380), (1100, 640), (110, 90, 70), -1)
    cv2.rectangle(img, (450, 150), (650, 300), (170, 165, 160), -1)
    return img


def _noisy(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = rng.normal(0, SIGMA, img.shape[:2] + (1,)).astype(np.int16)
    return np.clip(img.astype(np.int16) + n, 0, 255).astype(np.uint8)


def _window(
    before: np.ndarray, after: np.ndarray, rng: np.random.Generator, n: int = 9
) -> list[np.ndarray]:
    """一个 9 帧窗口:前 1/3 是 before、其余是 after,每帧独立加噪。"""
    cut = n // 3
    return [
        _noisy(before if i < cut else after, rng) for i in range(n)
    ]


def _run(before, after, cfg=None, *, rng=None, baseline=None):
    rng = rng or np.random.default_rng(11)
    cfg = cfg or GateConfig()
    base = baseline if baseline is not None else _preprocess(_noisy(before, rng))
    return evaluate_visual(
        _window(before, after, rng), cfg, input_fps=3, prev_frame=base
    )


def _lit(img: np.ndarray, alpha: float, half: bool = False) -> np.ndarray:
    out = img.astype(np.float32).copy()
    if half:
        out[:, : img.shape[1] // 2] *= alpha
    else:
        out *= alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def _with_person(img: np.ndarray, dx: int = 0) -> np.ndarray:
    """dx 让人在窗间微动 —— blink 的用例需要「持续活跃」而非「一次性出现后静止」。"""
    out = img.copy()
    cv2.rectangle(out, (520 + dx, 330), (610 + dx, 590), (55, 60, 75), -1)
    cv2.circle(out, (565 + dx, 305), 32, (95, 105, 130), -1)
    return out


class TestLightChangeNotTriggered:
    """光变不算真实变动——旧裸帧差在这些用例上全部误报(legacy_diff_score 可见)。"""

    @pytest.mark.parametrize("alpha", [1.6, 0.55])
    def test_global_light_change_suppressed(self, alpha):
        """开灯 / AGC / 色温:整幅一次仿射亮度变换 → PCT@全帧 认领,不触发。"""
        room = _room()
        r = _run(room, _lit(room, alpha))
        assert not r.changed
        assert r.diag.verdict == VERDICT_LIGHT_FRAME
        # 旧判据在同一输入上会满帧超阈 → 这正是 v1 要消掉的那类误报
        assert r.diag.legacy_diff_score > 0.5

    def test_half_room_lit_suppressed(self):
        """半房间被照亮:够不上整帧均匀,靠 PCT@分块 拦——初稿两套判据之间的盲区。"""
        room = _room()
        r = _run(room, _lit(room, 1.5, half=True))
        assert not r.changed
        assert r.diag.verdict == VERDICT_LIGHT_BLOCKS
        assert r.diag.light_block_ratio > 0.3
        assert r.diag.legacy_diff_score > 0.3  # 旧判据同样误报

    def test_local_light_on_textureless_region_is_known_false_positive(self):
        """**已知边界,不是待修的 bug**:局部光变落在完全无纹理的区域上 → 误报。

        分块尺度上,低 SNR 块的①②都是空洞断言,只能靠「可信块(有纹理且仿射一致)拟合出的
        光照变换定量预测该块亮度」来救援。若受照区**内部**一块纹理都没有(摄像头正对大片
        白墙、斜向光带只扫过墙面),救援无从起步 → 响应原样保留 → 触发。

        为什么接受:①旧的裸帧差 gate 在同样输入上也误报(见 legacy_diff_score),故不构成
        回归;②按 D2「优先不误拦、可以误放」,误报只是多跑一次下游;③真实房间的受照区几乎
        总会包含一些家具/边缘,轴向半房照亮那例就是靠受照半边的家具救援成功的。

        这条测试断言的是**当前的已知行为**。若哪天算法改进能拦住它,请连带更新方案文档
        里的边界表,而不是默默把断言反过来。
        """
        wall = np.full((720, 1280, 3), 150, np.uint8)  # 无纹理:整幅白墙
        lit = wall.astype(np.float32).copy()
        yy, xx = np.mgrid[0:720, 0:1280]
        lit[(xx * 0.6 + yy) < 1280 * 0.35] *= 1.5
        r = _run(wall, np.clip(lit, 0, 255).astype(np.uint8))
        assert r.changed  # 已知边界
        assert r.diag.legacy_diff_score > 0.1  # 旧 gate 同样误报 → 非回归

    def test_scattered_noise_suppressed(self):
        """散碎噪点无紧凑结构 → 形态学 + 紧凑度/面积 过滤。"""
        rng = np.random.default_rng(5)
        room = _room()
        speckled = room.copy()
        ys = rng.integers(0, 720, 9000)
        xs = rng.integers(0, 1280, 9000)
        speckled[ys, xs] = 255
        r = _run(room, speckled, rng=rng)
        assert not r.changed
        assert r.diag.verdict == VERDICT_BLOCKS_REJECTED

    def test_static_scene_not_triggered(self):
        room = _room()
        r = _run(room, room)
        assert not r.changed
        assert r.diag.block_count == 0


class TestRealChangeTriggered:
    """真实变动必须触发。后两例旧裸帧差会漏报(legacy 低于 change_threshold)。"""

    def test_person_enters(self):
        room = _room()
        r = _run(room, _with_person(room))
        assert r.changed
        assert r.diag.verdict == VERDICT_TRIGGER
        assert r.diag.block_count >= 1

    def test_small_area_motion(self):
        """约占画面 1% 的手臂。旧判据 score < change_threshold(0.005)→ 漏报。"""
        room = _room()
        moved = room.copy()
        cv2.rectangle(moved, (300, 250), (430, 330), (95, 120, 175), -1)
        r = _run(room, moved)
        assert r.changed
        assert r.diag.legacy_diff_score < GateConfig().change_threshold

    def test_uniform_dark_object_triggered(self):
        """「均匀亮度且比背景暗」的单色物体(灰夹克/灰调宠物)。

        这是 PCT 判据①单独使用时的头号漏报面——它的亮度比恒定,签名与阴影一致。判据②
        (纹理保持)被设成必须门槛正是为了挡住这一类,故此处必须触发。
        """
        room = _room()
        jacket = room.copy()
        cv2.rectangle(jacket, (520, 330), (610, 590), (70, 70, 70), -1)
        r = _run(room, jacket)
        assert r.changed

    def test_closeup_over_half_frame_triggered(self):
        """人贴镜头占 >50%:裸像素签名与「整幅被照亮」相同(arXiv:1901.03577)。

        纹理被替换 → 判据② 的 ncc 低、① 的残差高 → 不判光变。**不需要给大面积前景开
        size 豁免**,这是初稿「漂移门单独用不可靠判据」那处矛盾的正解。
        """
        room = _room()
        close = room.copy()
        cv2.rectangle(close, (0, 108), (1280, 720), (58, 62, 78), -1)
        cv2.circle(close, (640, 144), 150, (100, 110, 135), -1)
        r = _run(room, close)
        assert r.changed
        assert r.diag.struct_ratio > 0.5

    def test_low_contrast_object_triggered(self):
        """~8 灰度级对比的物体。响应实心但带 ~13% 噪声空洞,依赖 CLOSE→OPEN 的形态学顺序。"""
        room = _room()
        obj = room.copy()
        cv2.rectangle(obj, (300, 250), (430, 330), (148, 153, 158), -1)
        r = _run(room, obj)
        assert r.changed

    def test_object_on_flat_background_triggered(self):
        """平坦背景 + 物体:无可信块可供救援,响应原样保留 → 触发(保不漏方向)。"""
        flat = np.full((720, 1280, 3), 110, np.uint8)
        r = _run(flat, _with_person(flat))
        assert r.changed


class TestDayNightBands:
    def test_switch_requires_confirm_windows_and_swallows_one(self):
        """彩色→IR 需连续 night_confirm_windows 窗才切档,切档那一窗被吞掉并重建基准。

        滞回是必需的:黄昏时饱和度在阈值附近徘徊,没有 N 窗确认会让参数档逐窗翻转。
        """
        rng = np.random.default_rng(7)
        cfg = GateConfig()
        room = _room()
        ir = cv2.cvtColor(cv2.cvtColor(room, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        base = None
        modes = []
        for src in (room, room, ir, ir, ir):
            r = evaluate_visual(
                [_noisy(src, rng) for _ in range(9)], cfg, input_fps=3, prev_frame=base
            )
            base = r.last_checked
            modes.append((r.diag.mode, r.diag.verdict))
        assert modes[1][0] == MODE_DAY
        assert modes[2][0] == MODE_DAY  # 第一个 IR 窗只累计 pending,不切
        assert modes[3] == (MODE_NIGHT, VERDICT_MODE_SWITCH)  # 确认后切档 + 吞窗
        assert modes[4][0] == MODE_NIGHT

    def test_single_off_band_window_does_not_flip(self):
        """单个反向窗不足以翻档(confirm_windows=2)。"""
        rng = np.random.default_rng(9)
        cfg = GateConfig()
        room = _room()
        ir = cv2.cvtColor(cv2.cvtColor(room, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        base = VisualBaseline(frame=_preprocess(_noisy(ir, rng)), mode=MODE_NIGHT)
        r = evaluate_visual(
            [_noisy(room, rng) for _ in range(9)], cfg, input_fps=3, prev_frame=base
        )
        assert r.diag.mode == MODE_NIGHT
        assert r.last_checked.pending_switch == 1

    def test_night_band_blocks_more_than_day(self):
        """同一个弱变化,夜间档(阈值抬高)拦得比白天档狠——D4 的方向,故意用漏报换成本。"""
        rng = np.random.default_rng(13)
        room = _room()
        weak = room.copy()
        cv2.rectangle(weak, (300, 250), (430, 330), (148, 153, 158), -1)
        frames = _window(room, weak, rng)
        base = _preprocess(_noisy(room, rng))
        day = evaluate_visual(frames, GateConfig(), input_fps=3, prev_frame=base)
        night = evaluate_visual(
            frames,
            GateConfig(),
            input_fps=3,
            prev_frame=VisualBaseline(frame=base, mode=MODE_NIGHT),
        )
        assert day.changed
        assert night.diag.mode == MODE_NIGHT
        assert night.diag.struct_ratio <= day.diag.struct_ratio


class TestPlumbing:
    """跨窗基准、cold-start、空帧等结构性行为——与判定算法无关,必须保持。"""

    config = GateConfig()

    def test_cold_start_passes_without_prev_frame(self):
        """无基准(cold-start):即便画面全静止也放行,用于建立基准。"""
        rng = np.random.default_rng(3)
        room = _room()
        r = evaluate_visual(
            [_noisy(room, rng) for _ in range(9)], self.config, input_fps=3
        )
        assert r.changed
        assert r.diag.verdict == VERDICT_COLD_START

    def test_empty_frames_returns_none_baseline(self):
        r = evaluate_visual([], self.config)
        assert not r.changed
        assert r.last_checked is None

    def test_all_empty_frames_returns_none_baseline(self):
        empty = np.empty((0, 0, 3), dtype=np.uint8)
        r = evaluate_visual([empty] * 3, self.config)
        assert not r.changed
        assert r.last_checked is None

    def test_empty_frame_in_window_skipped(self):
        """窗口内混入空帧(解码异常)→ 跳过不参与比较,不抛错。"""
        rng = np.random.default_rng(4)
        room = _room()
        empty = np.empty((0, 0, 3), dtype=np.uint8)
        frames = [_noisy(room, rng), empty, _noisy(room, rng)]
        r = evaluate_visual(
            frames, self.config, input_fps=3, prev_frame=_preprocess(_noisy(room, rng))
        )
        assert not r.changed
        assert r.last_checked is not None

    def test_last_checked_is_baseline_with_preprocessed_frame(self):
        """基准是 VisualBaseline:448 灰度帧 + 昼夜档 + 待切换计数(模式滞回需要记忆)。"""
        rng = np.random.default_rng(6)
        r = evaluate_visual(
            [_noisy(_room(), rng) for _ in range(9)], self.config, input_fps=3
        )
        assert isinstance(r.last_checked, VisualBaseline)
        assert r.last_checked.frame.shape == _DIFF_SIZE
        assert r.last_checked.frame.ndim == 2
        assert r.last_checked.mode in (MODE_DAY, MODE_NIGHT)

    def test_bare_ndarray_baseline_accepted(self):
        """兼容旧调用方传裸预处理帧(等价于默认档的 VisualBaseline)。"""
        rng = np.random.default_rng(8)
        room = _room()
        r = evaluate_visual(
            [_noisy(room, rng) for _ in range(9)],
            self.config,
            input_fps=3,
            prev_frame=_preprocess(_noisy(room, rng)),
        )
        assert not r.changed

    def test_last_checked_is_last_checked_frame_not_window_tail(self):
        """基准取"末次被检帧"。check_fps=2 + input_fps=3 → interval=2 → 抽检含末帧。"""
        rng = np.random.default_rng(10)
        room = _room()
        tail = _noisy(_lit(room, 1.6), rng)
        frames = [_noisy(room, rng) for _ in range(8)] + [tail]
        r = evaluate_visual(frames, self.config, input_fps=3)
        assert np.array_equal(r.last_checked.frame, _preprocess(tail))

    def test_cross_window_change_detected(self):
        """窗内全静止,但与上一窗基准不同 → 跨窗对补获。

        跨窗对的时间差 ≈ 一整个窗口(数秒),但 PCT 与结构响应都不依赖小位移,故与窗内对
        走完全相同的判定路径,无需特殊处理。
        """
        rng = np.random.default_rng(12)
        room = _room()
        person = _with_person(room)
        base = _preprocess(_noisy(room, rng))
        r = evaluate_visual(
            [_noisy(person, rng) for _ in range(9)],
            self.config,
            input_fps=3,
            prev_frame=base,
        )
        assert r.changed
        assert r.cross_max > 0
        assert r.intra_max == 0.0 or r.cross_max >= r.intra_max

    def test_intra_and_cross_scores_separated(self):
        rng = np.random.default_rng(14)
        room = _room()
        base = _preprocess(_noisy(room, rng))
        r = evaluate_visual(
            [_noisy(room, rng) for _ in range(9)],
            self.config,
            input_fps=3,
            prev_frame=base,
        )
        assert r.max_score == max(r.intra_max, r.cross_max)

    def test_diag_carries_calibration_inputs(self):
        """7 个阈值全靠线上分布标定,所以承重中间量必须都在 diag 里。"""
        rng = np.random.default_rng(15)
        room = _room()
        r = _run(room, _with_person(room), rng=rng)
        d = r.diag
        assert d.noise_sigma > 0
        assert d.pct_alpha > 0
        assert d.pct_texture > 0
        assert 0.0 <= d.struct_ratio <= 1.0
        assert 0.0 <= d.light_block_ratio <= 1.0
        assert d.verdict and d.mode


class TestLegacyDiffFallback:
    """visual_algo="diff" 回退路径:线上出问题时不改代码即可回滚。"""

    cfg = GateConfig(visual_algo="diff")

    def test_light_change_triggers_under_legacy(self):
        """回退档保持旧行为——全局光变照旧误报,这正是它作为对照基线的用途。"""
        room = _room()
        r = _run(room, _lit(room, 1.6), self.cfg)
        assert r.changed

    def test_static_not_triggered_under_legacy(self):
        room = _room()
        r = _run(room, room, self.cfg)
        assert not r.changed


class TestComputeFrameDiff:
    """旧裸帧差辅助函数,现仅用于 A/B 对照打分,行为不变。"""

    def test_identical_frames_return_zero(self):
        frame = _solid_frame(128, 128, 128)
        assert compute_frame_diff(frame, frame) == 0.0

    def test_completely_different_frames(self):
        black = _solid_frame(0, 0, 0)
        white = _solid_frame(255, 255, 255)
        assert compute_frame_diff(black, white) > 0.9

    def test_slight_difference_below_threshold(self):
        gray = _solid_frame(128, 128, 128)
        slightly = _solid_frame(130, 130, 130)
        assert compute_frame_diff(gray, slightly) == 0.0


class TestInterAreaDownsampling:
    """INTER_AREA averages source-pixel regions; sparse high-frequency noise
    that survives INTER_LINEAR should be smoothed away."""

    def test_sparse_noise_score_is_low(self):
        rng = np.random.default_rng(42)
        h, w = 1080, 1920
        clean = np.full((h, w, 3), 128, dtype=np.uint8)
        noisy = clean.copy()
        n_noise = int(h * w * 0.01)
        noisy[rng.integers(0, h, n_noise), rng.integers(0, w, n_noise)] = 255
        score = compute_frame_diff(clean, noisy)
        assert score < 0.005, f"INTER_AREA 未抗住散布噪声: {score}"

    def test_large_object_change_preserved(self):
        h, w = 1080, 1920
        clean = np.full((h, w, 3), 128, dtype=np.uint8)
        with_block = clean.copy()
        with_block[400:500, 800:900] = 255
        assert compute_frame_diff(clean, with_block) > 0.001


class TestPersistentSourceSuppression:
    """持续变化源抑制（方案 v3 纯 CV 层）。**该层默认关闭**，用例显式开启。

    关闭原因见 config.env_mask_enabled 的注释：真机实测占空比判据分不开「位置固定的间歇
    伪影」与「坐着不动的人」——前者占空比只有 0.19、后者 0.79，与方案假设正好相反。
    机制本身（统计在抑制前 / 显著性解除 / 面积上限）经真机验证是对的，故保留代码与用例。

    真机动机：安静机位的主要误报源是这类东西（OSD 时间水印每秒跳变、轻微摆动的线缆、
    风扇、屏幕）——omni 判「无人、画面静止」而 gate 照旧触发。判据**源无关**，只问
    「这个位置是不是长期都在变」。
    """

    @staticmethod
    def _watermark(img: np.ndarray, sec: int) -> np.ndarray:
        """左上角固定位置的持续变化区，模拟 OSD 时间水印 / 屏幕这类源。

        固定区内一个亮块左右交替（形状变化）：真机水印只有秒位一个字符跳变时，缩到 448 网格
        后约占 0.07% 面积，远低于 min_block_ratio(0.5%)、根本形不成块——那测不到抑制机制。
        这里让变化区达到能成块的面积（≈1.3%）。
        """
        out = img.copy()
        cv2.rectangle(out, (20, 20), (330, 60), (30, 30, 30), -1)
        cv2.putText(out, f"2026/08/27 13:20:{sec:02d}", (26, 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (245, 245, 245), 2)
        # 固定区内左右交替一个亮块 = **形状**变化。
        # 不能改成「整块底色明暗交替」:那在数学上是一次完美的仿射映射(两个灰度级必被两参数
        # 仿射拟合),PCT@分块 会正确地把它当局部光变压掉,于是测不到本层。真实水印变的是数字
        # 字形——形状变化非仿射,PCT 压不掉,正是需要本层的那类源。
        x = 40 if sec % 2 == 0 else 190
        cv2.rectangle(out, (x, 24), (x + 130, 56), (200, 200, 200), -1)
        return out

    def _run_stream(self, cfg, n_windows, rng, *, person_at=None):
        """喂 n_windows 个窗，每窗水印秒位都在跳；person_at 指定的窗里额外放一个人。"""
        room = _room()
        base = None
        results = []
        for w in range(n_windows):
            frames = []
            for i in range(9):
                sec = (w * 9 + i) % 60
                f = self._watermark(room, sec)
                if person_at is not None and w in person_at and i >= 3:
                    f = _with_person(f)
                frames.append(_noisy(f, rng))
            r = evaluate_visual(frames, cfg, input_fps=3, prev_frame=base)
            base = r.last_checked
            results.append(r)
        return results

    def test_watermark_suppressed_after_duty_builds_up(self):
        """水印每窗都跳 → 占空比爬到阈值后该块被抑制，不再触发。

        前若干窗必然触发（占空比还没建立）——这是**故意**的保守设计：抑制是漏报风险，
        必须先看够证据。
        """
        cfg = GateConfig(env_mask_enabled=True)
        rng = np.random.default_rng(21)
        res = self._run_stream(cfg, 40, rng)
        early = [r.changed for r in res[1:8]]  # 跳过 cold_start
        late = [r.changed for r in res[-8:]]
        assert any(early), "占空比未建立前应照常触发（保守方向）"
        assert not any(late), f"占空比建立后水印应被压掉，实际 late={late}"
        assert res[-1].diag.env_block_ratio > 0
        assert res[-1].diag.verdict == VERDICT_ENV_SUPPRESSED

    def test_person_still_triggers_over_suppressed_region(self):
        """**这条是抑制层的安全底线**：人出现在已被抑制的区域上仍必须触发。

        解除靠「本窗该块响应幅度显著超过其长期基线」，而不是靠占空比——若按占空比降权，
        人挡在水印/风扇前会把该处占空比推得更高、于是被压得更死，正好压掉要抓的东西
        （方案 §7.4 记的就是这个反向错误）。
        """
        cfg = GateConfig(env_mask_enabled=True)
        rng = np.random.default_rng(22)
        res = self._run_stream(cfg, 40, rng, person_at={38, 39})
        assert not res[36].changed, "无人窗应已被抑制"
        assert res[39].changed, "人出现在抑制区上必须触发（显著性解除）"

    def test_disabled_by_config(self):
        cfg = GateConfig(env_mask_enabled=False)
        rng = np.random.default_rng(23)
        res = self._run_stream(cfg, 40, rng)
        assert any(r.changed for r in res[-8:]), "关闭后不应有任何抑制"
        assert res[-1].diag.env_block_ratio == 0.0

    def test_area_cap_blocks_wholesale_suppression(self):
        """抑制区超过面积上限 → 整体不抑制（大半画面被判持续源必然是判错了）。"""
        cfg = GateConfig(env_mask_enabled=True, env_area_max_ratio=0.001)  # 逼近 0：任何抑制都超限
        rng = np.random.default_rng(24)
        res = self._run_stream(cfg, 40, rng)
        assert any(r.changed for r in res[-8:])
        assert res[-1].diag.env_block_ratio == 0.0


class TestBlinkStatistic:
    """blink(活跃状态翻转率)的**方向**必须与 duty 相反。

    这是 SuBSENSE 的 blinking-pixel / dynamic-entropy 在本实现里的对应量。为什么需要它:
    duty 数「该块是不是活跃」,真机实测**人站着不动 duty 冲到 0.87 而编码带状伪影只有
    0.28** —— 方向是反的,这正是 duty 抑制层被真机证伪的根因。blink 数「活跃↔不活跃翻转了
    多少次」:人长驻只在进/出各翻一次(实测 0.025),偶发伪影反复翻(实测 0.438)。

    ⚠️ 断言必须用 ``env_blink_at_block`` 而不是全图 ``env_blink_max``:人横穿画面时路径上
    每个块都会翻转,全图 max 立刻顶满(实测长驻用例下 blink_max == duty_max == 0.97),这个
    统计量分不开任何东西。承重的是「触发的那一块历史上闪不闪」。

    ⚠️ 这里用合成流是**恰当的**:blink 是纯时间统计量。之前记过「合成 fixture 在 fill/coh/
    stdA 上是负资产」——那是因为 `cv2.rectangle` 把「矩形性」直接编码进了被测量的那个性质;
    时间维度上不存在这个问题,活跃序列的形状就是被断言的语义本身。
    """

    def _stream(self, cfg, n_windows, rng, *, flicker_every=None, person_from=None):
        """flicker_every: 每 k 窗出现一次的偶发源(→ 高 blink)。
        person_from: 从第 k 窗起持续存在并每窗微动的人(→ 高 duty、低 blink)。
        """
        room = _room()
        base = None
        out = []
        for w in range(n_windows):
            f = room.copy()
            if flicker_every and w % flicker_every == 0 and w > 0:
                cv2.rectangle(f, (300, 250), (400, 350), (200, 205, 215), -1)
            frames = []
            for i in range(9):
                g = f
                if person_from is not None and w >= person_from:
                    # 步长按块尺寸反推:active_cov=0.25 要求 28px 块内 25% 像素响应
                    # → 448 空间约 7px 位移 → 1280 空间约 20px。步长小于它则该块永不"活跃",
                    # duty 恒为 0(踩过:dx=3 时 duty 只有 0.004)。
                    g = _with_person(f, dx=22 * (i % 4))   # 原地小幅摆动,不横扫全帧
                frames.append(_noisy(g, rng))
            r = evaluate_visual(frames, cfg, input_fps=3, prev_frame=base)
            base = r.last_checked
            out.append(r)
        return out

    def test_sporadic_source_has_high_blink_low_duty(self):
        """偶发源:活跃/不活跃反复翻转 → blink 高。"""
        cfg = GateConfig()
        res = self._stream(cfg, 36, np.random.default_rng(31), flicker_every=6)
        d = res[-1].diag
        d = next((r.diag for r in reversed(res) if r.diag.max_block_w > 0), d)
        assert d.env_blink_at_block > 0.25, (
            f"偶发源触发块的 blink 应显著,实际 {d.env_blink_at_block}"
        )
        assert d.env_blink_at_block >= d.env_duty_at_block * 0.5, (
            f"偶发源 blink 不应远低于 duty: "
            f"blink={d.env_blink_at_block} duty={d.env_duty_at_block}"
        )

    def test_persistent_activity_has_high_duty_low_blink(self):
        """人长驻:只在进入时翻一次 → duty 高而 blink 低。**方向与 duty 层相反的证据。**"""
        cfg = GateConfig()
        res = self._stream(cfg, 36, np.random.default_rng(32), person_from=4)
        d = res[-1].diag
        assert d.env_duty_at_block > 0.7, (
            f"长驻活动触发块的 duty 应高,实际 {d.env_duty_at_block}"
        )
        assert d.env_blink_at_block < d.env_duty_at_block * 0.5, (
            f"长驻活动的 blink 必须显著低于 duty: "
            f"blink={d.env_blink_at_block} duty={d.env_duty_at_block}"
        )

    def test_blink_measured_even_when_env_mask_disabled(self):
        """默认(env_mask_enabled=False)下也必须照常统计 —— 现阶段就是只测不用。"""
        cfg = GateConfig(env_mask_enabled=False)
        res = self._stream(cfg, 20, np.random.default_rng(33), flicker_every=5)
        assert res[-1].diag.env_blink_max > 0.0
        assert res[-1].diag.env_block_ratio == 0.0, "只测不用:不得产生任何抑制"


class TestDumpOnDisagree:
    """``gate_debug_dump_on_disagree`` 只落新旧判定不一致的窗。

    为什么需要这一档:``gate_debug_dump_max`` 是全局计数、只在重启时归零,落全部窗时夜里一小时
    就用光配额。实测 03:37~06:08 出现的 19 个夜间分歧窗一个都没留下帧,导致无法人工核实。
    """

    def _dump(self, tmp_path, on_disagree, a, b, *, rng=None):
        cfg = GateConfig(
            gate_debug_dump_dir=str(tmp_path),
            gate_debug_dump_max=50,
            gate_debug_dump_on_disagree=on_disagree,
        )
        rng = rng or np.random.default_rng(41)
        base = evaluate_visual([_noisy(a, rng)] * 3, cfg, input_fps=3).last_checked
        r = evaluate_visual([_noisy(b, rng) for _ in range(6)], cfg, input_fps=3,
                            prev_frame=base)
        return r, sorted(p.name for p in tmp_path.glob("*.json"))

    def test_agreement_not_dumped(self, tmp_path, monkeypatch):
        """静止窗:两者都不放行 → 不是分歧 → 不落盘。"""
        monkeypatch.setattr(visual_gate_module, "_dump_count", 0, raising=False)
        room = _room()
        r, files = self._dump(tmp_path, True, room, room)
        assert not r.changed
        assert r.diag.legacy_diff_score < GateConfig().change_threshold
        assert files == [], f"一致的窗不该落盘,实际落了 {files}"

    def test_disagreement_dumped(self, tmp_path, monkeypatch):
        """小面积运动:新判据触发而旧裸帧差低于阈值 → 分歧 → 落盘。"""
        monkeypatch.setattr(visual_gate_module, "_dump_count", 0, raising=False)
        room = _room()
        moved = room.copy()
        cv2.rectangle(moved, (300, 250), (430, 330), (95, 120, 175), -1)
        r, files = self._dump(tmp_path, True, room, moved)
        assert r.changed
        assert r.diag.legacy_diff_score < GateConfig().change_threshold
        assert len(files) == 1, f"分歧窗必须落盘,实际 {files}"

    def test_disabled_dumps_everything(self, tmp_path, monkeypatch):
        """关掉该档 → 恢复「落全部窗」(标定阈值分布需要无偏样本)。"""
        monkeypatch.setattr(visual_gate_module, "_dump_count", 0, raising=False)
        room = _room()
        r, files = self._dump(tmp_path, False, room, room)
        assert not r.changed
        assert len(files) >= 1, "关掉后静止窗也应落盘"
