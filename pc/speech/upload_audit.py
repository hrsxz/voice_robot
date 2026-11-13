"""
云端上传的统一出口: 唤醒词模式下, 对话窗口没打开时禁止上传音频, 每次上传都记一行审计日志。

日志只记录时间、输入方式、上传方式和窗口状态, 不记录音频和文字内容
(用来核查"唤醒前不上传", 见 openspec/changes/voice-input-modes)。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from pc.speech.conversation_gate import ConversationGate

ENV_LOG_PATH = "VOICE_ROBOT_UPLOAD_LOG"
DEFAULT_WAKE_LOG = Path("logs") / "uploads.jsonl"


class UploadBlocked(RuntimeError):
    """唤醒前尝试上传音频 (程序错误, 不应该发生)。"""


class UploadGuard:
    def __init__(
        self,
        gate: ConversationGate,
        input_mode: str = "ptt",
        log_path: str | Path | None = None,
    ) -> None:
        self.gate = gate
        self.input_mode = input_mode
        self.log_path: Path | None = None
        self.records: list[dict[str, Any]] = []
        self.blocked = 0
        self.set_mode(input_mode, log_path)

    def set_mode(self, input_mode: str, log_path: str | Path | None = None) -> None:
        self.input_mode = input_mode
        path = log_path or os.getenv(ENV_LOG_PATH)
        if not path and input_mode == "wake":
            path = DEFAULT_WAKE_LOG
        self.log_path = Path(path) if path else None

    @property
    def enforced(self) -> bool:
        return self.input_mode == "wake"

    def check(self, kind: str) -> None:
        """
        准备向云端发送音频之前调用。kind: "file" (整句识别) / "stream" (流式识别)。
        唤醒词模式下窗口没打开时抛出 UploadBlocked。
        """
        window_open = self.gate.is_active()
        if self.enforced and not window_open:
            self.blocked += 1
            raise UploadBlocked("未唤醒，禁止上传音频")
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "input": self.input_mode,
            "kind": kind,
            "window_open": window_open,
            "window_remaining_s": round(self.gate.remaining_seconds(), 1),
        }
        self.records.append(record)
        if self.enforced:
            print(f"[上传] {kind}（窗口剩余 {record['window_remaining_s']} 秒）")
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                print(f"[上传] 审计日志写入失败：{exc}")
