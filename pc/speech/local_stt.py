"""
唤醒词模式的本地识别: faster-whisper tiny (CPU, int8), 音频不出本机。

只用来判断唤醒词和急停, 不追求准确 (命令部分唤醒后交给云端识别)。
**不加 initial_prompt**: 加上唤醒词提示后, 静音、噪声和普通聊天都会被"识别"成唤醒词
(见 openspec/changes/voice-input-modes/measurements.md)。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import numpy as np

MODEL_NAME = "tiny"
SAMPLE_RATE = 16000
# 一个片段最多 2.5 秒, 正常不超过 15 个字; 限制输出长度, 防止模型反复输出同一个字卡住
MAX_NEW_TOKENS = 24
# 模型认为"没有人说话"的概率超过这个值时丢弃结果
NO_SPEECH_THRESHOLD = 0.6


class LocalTranscriber:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self._model: Any = None
        self._lock = threading.Lock()
        self.last_ms = 0.0

    def load(self) -> None:
        """加载模型 (第一次运行时会下载, tiny 约 75 MB)。启动时调用, 避免第一次唤醒时等待。"""
        with self._lock:
            if self._model is not None:
                return
            import truststore
            from faster_whisper import WhisperModel

            truststore.inject_into_ssl()  # 公司网络 TLS 拦截: 下载模型时使用系统证书库
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=max(1, (os.cpu_count() or 4) - 1),
            )

    def transcribe(self, audio16k: np.ndarray) -> str:
        """audio16k: 16 kHz 单声道 (int16 或 float32)。阻塞调用, 在线程里运行。"""
        self.load()
        if audio16k.dtype != np.float32:
            audio16k = audio16k.astype(np.float32) / 32768.0
        start = time.perf_counter()
        segments, _ = self._model.transcribe(
            audio16k,
            language="zh",
            beam_size=1,
            vad_filter=False,  # 已经由 VAD 切好
            condition_on_previous_text=False,
            without_timestamps=True,
            max_new_tokens=MAX_NEW_TOKENS,
        )
        text = "".join(
            s.text for s in segments if s.no_speech_prob < NO_SPEECH_THRESHOLD
        ).strip()
        self.last_ms = (time.perf_counter() - start) * 1000
        return text
