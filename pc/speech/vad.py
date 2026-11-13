"""
语音活动检测 (VAD) 的公共部分。

麦克风统一按 24 kHz 采集 (流式识别只支持 24 kHz), 每帧 30 ms;
webrtcvad 和本地 Whisper 需要 16 kHz, 在这里降采样。
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 24000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 720
FRAME_BYTES = FRAME_SAMPLES * 2


def to_16k(pcm24: np.ndarray) -> np.ndarray:
    """
    24 kHz int16 -> 16 kHz int16
    (线性插值; 语音主要能量在 4 kHz 以下, 足够 VAD 和唤醒检测用)。
    """
    if len(pcm24) == 0:
        return np.zeros(0, dtype=np.int16)
    n = len(pcm24) * 2 // 3
    x = np.linspace(0, len(pcm24) - 1, n)
    return np.interp(x, np.arange(len(pcm24)), pcm24.astype(np.float32)).astype(np.int16)


class WebRtcVad:
    """判断一帧 (30 ms, 24 kHz PCM16 bytes) 是不是人声。"""

    def __init__(self, aggressiveness: int = 2) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(aggressiveness)

    def __call__(self, frame: bytes) -> bool:
        pcm16k = to_16k(np.frombuffer(frame, dtype=np.int16))
        return self._vad.is_speech(pcm16k.tobytes(), 16000)
