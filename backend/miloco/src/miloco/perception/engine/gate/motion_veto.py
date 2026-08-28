# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Frigate 式运动检测否决通道(improved_motion.py 的移植,不含检测级)。

对齐 frigate/motion/improved_motion.py 的核心设计:
- **100px 缩略图**:高频纹理的亚像素抖动(植物摇晃/相机微振)在降采样+模糊后平均归零;
  相对像素 gate 的 448 管线,噪声底从 5.7% 降到 ~0.0%(真机 428 窗实测)
- **分位数对比度拉伸**(4%/96% 分位,50 帧缓冲):整幅光变被归一化掉,而不是被「判别」
- **运动门控 EMA**:无运动恒更新(frame_alpha=0.01);有运动连续满 10 帧后以 0.2 权重
  吸收(物体停住→成为背景)——解决「吸收 vs 事件保留」的两难
- 校准态:pct_motion<0.05 且运动框≤4 持续后视为已校准

在本 gate 中的用法是**否决通道**(veto):像素 gate 触发后,若运动级在触发 bbox 处
未见运动(cover<pct 阈值),则整窗压制。真机 428 窗标定:稳态 pct≤0.008、真实事件
cover 0.44~0.89;伪影(幅度≤阈值30)、屏幕文字、光照在 100px 下不可见 → 一并压掉。
已知代价:小幅真实事件(≤25 灰阶、100px 不可见)会被压(样本 1/17),属 D2 复议范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

FRAME_H = 100
THRESHOLD = 30
FRAME_ALPHA = 0.01
CONTOUR_AREA = 10
MOTION_GRACE = 10
ABSORB_WEIGHT = 0.2
QUANTILE_BUF = 50
CALIBRATED_FRAMES = 50  # 分位缓冲预热完成前 fail-open(不否决,D2 安全)


@dataclass
class MotionVetoState:
    """跨窗携带的运动级状态(挂在 VisualBaseline 上)。"""

    avg: np.ndarray  # float32 (nh, nw) 背景均值帧
    quantiles: list = field(default_factory=list)  # [(q4,q96)] 最多 50 帧
    motion_frames: int = 0
    frames_seen: int = 0
    calibrated: bool = False

    @staticmethod
    def create(gray: np.ndarray) -> "MotionVetoState":
        f = MotionVetoState._prep(gray)
        return MotionVetoState(avg=f.astype(np.float32))

    @staticmethod
    def _prep(gray: np.ndarray) -> np.ndarray:
        h, w = gray.shape[:2]
        nh = FRAME_H
        nw = max(1, int(round(w * nh / h)))
        return cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)

    def _contrast(self, f: np.ndarray) -> np.ndarray:
        q4, q96 = np.percentile(f, 4), np.percentile(f, 96)
        self.quantiles.append((float(q4), float(q96)))
        if len(self.quantiles) > QUANTILE_BUF:
            self.quantiles.pop(0)
        m4 = float(np.mean([q[0] for q in self.quantiles]))
        m96 = float(np.mean([q[1] for q in self.quantiles]))
        if m96 - m4 < 1:
            return f
        return np.clip((f - m4) * 255.0 / (m96 - m4), 0, 255)

    def step(self, gray: np.ndarray) -> tuple[float, list]:
        """喂一帧 448 灰度,返回 (pct_motion, 运动框列表@100px)。同时更新状态。"""
        f = self._prep(gray)
        f = self._contrast(f).astype(np.uint8)
        f = cv2.GaussianBlur(f, (0, 0), 1)
        diff = cv2.absdiff(f, self.avg.astype(np.uint8))
        th = ((diff > THRESHOLD).astype(np.uint8)) * 255
        th = cv2.dilate(th, np.ones((3, 3), np.uint8), iterations=1)
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = [cv2.boundingRect(c) for c in cnts if cv2.contourArea(c) > CONTOUR_AREA]
        motion_px = sum(bw * bh for _, _, bw, bh in boxes)
        pct = motion_px / float(th.size)
        self.frames_seen += 1
        if not self.calibrated and self.frames_seen >= CALIBRATED_FRAMES:
            self.calibrated = True
        if boxes:
            self.motion_frames += 1
            if self.motion_frames >= MOTION_GRACE:
                # 连续运动满 10 帧:物体停住了,以大权重吸收为新背景
                self.avg = (1 - ABSORB_WEIGHT) * self.avg + ABSORB_WEIGHT * f.astype(np.float32)
        else:
            self.motion_frames = 0
            self.avg = (1 - FRAME_ALPHA) * self.avg + FRAME_ALPHA * f.astype(np.float32)
        return pct, boxes

    def cover(self, boxes: list, bbox, shape448) -> float:
        """gate bbox(448 口径)与运动框的重叠占 gate bbox 面积比。"""
        if not boxes or not bbox or not bbox[2]:
            return 0.0
        h, w = shape448
        nh = FRAME_H
        nw = max(1, int(round(w * nh / h)))
        sx, sy = nw / w, nh / h
        x, y, bw, bh = bbox
        gx0, gy0 = x * sx, y * sy
        gx1, gy1 = (x + bw) * sx, (y + bh) * sy
        ov = 0.0
        for bx, by, bw2, bh2 in boxes:
            ix = max(0.0, min(gx1, bx + bw2) - max(gx0, bx))
            iy = max(0.0, min(gy1, by + bh2) - max(gy0, by))
            ov += ix * iy
        gate_area = max((gx1 - gx0) * (gy1 - gy0), 1.0)
        return min(ov / gate_area, 1.0)
