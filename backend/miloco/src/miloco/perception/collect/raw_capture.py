"""调试用:把相机的**裸 H.264 码流**抓到磁盘,供离线取运动矢量。

为什么需要裸码流:判据「真实运动有运动矢量、量化带状伪影没有」只在**相机自己那次编码**的
码流里成立。我们收到的是已解码帧,带状伪影此时已经烘进像素、成了真实内容,再拿 libx264
重编一遍只会得到我们自己编码器的选择,证明不了任何事(实测过)。

**纯调试通道**:默认关闭;开启后只写文件、到时/到量自动注销,不进任何判决路径。抓完请把
配置改回关闭 —— 它会持续占用一路 raw 注册。

已知盲区(实测,见 Tolaria 方案):**I 帧上运动矢量恒为 0,连真在移动的物体也是**。所以离线
分析必须按「窗内任一 P/B 帧上该区域有无 MV 活动」来聚合,不能逐帧对判决。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 单次抓取的硬上限:防止配置写错时把磁盘写满。
_MAX_BYTES = 256 * 1024 * 1024


class RawCapture:
    """一路相机的裸码流抓取。线程安全性依赖调用方:回调来自流线程,单线程串行。"""

    def __init__(self, did: str, out_dir: str, seconds: int) -> None:
        self.did = did
        self.seconds = max(1, seconds)
        self._deadline = time.monotonic() + self.seconds
        self._written = 0
        self._logged_shape = False
        self.done = False
        d = Path(out_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        # .h264 裸流:若相机发的是 Annex B(带 00 00 00 01 起始码)则 ffmpeg 可直接解;
        # 若是 AVCC(长度前缀)则需要另行加壳 —— 首帧头部字节会打进日志供判断。
        self.path = d / f"{did.replace(':', '_')}_{int(time.time())}.h264"
        self._fh = self.path.open("wb")
        logger.info(
            "raw capture started: did=%s file=%s seconds=%d", did, self.path, self.seconds
        )

    def feed(self, *args: object) -> None:
        """流线程回调。**绝不允许抛异常影响拉流**。"""
        if self.done:
            return
        try:
            data = next((a for a in args if isinstance(a, (bytes, bytearray))), None)
            if not self._logged_shape:
                self._logged_shape = True
                # 回调是 5 参的 Callable[[str, bytes, int, int, int], ...],但各位含义
                # 未在代码里写明 —— 把实际布局与头部字节打出来,离线据此判容器格式。
                logger.info(
                    "raw capture callback layout: types=%s | head=%s",
                    [type(a).__name__ for a in args],
                    (bytes(data[:16]).hex() if data else "none"),
                )
            if data:
                self._fh.write(data)
                self._written += len(data)
            if time.monotonic() >= self._deadline or self._written >= _MAX_BYTES:
                self.close()
        except Exception:  # 调试通道绝不能影响主流程
            logger.debug("raw capture feed failed", exc_info=True)
            self.close()

    def close(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            self._fh.close()
        except Exception:
            logger.debug("raw capture close failed", exc_info=True)
        logger.info(
            "raw capture finished: did=%s file=%s bytes=%d", self.did, self.path, self._written
        )
