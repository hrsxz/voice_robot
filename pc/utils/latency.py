"""
每条语音命令的耗时记录。

一条命令沿链路依次打点:
    recording_end -> stt_done -> route_done -> ble_sent -> hub_done
另外记录 first_enqueued: 第一个动作放进执行队列的时刻。
流式模式下第一个动作可能在说完之前就入队, 这时"首个动作入队"是负数。

在现有 Hub 协议下, Hub 只在动作完成后才回复, 所以 hub_done 表示"Hub 报告动作完成";
用户感觉到的延迟终点用 ble_sent 近似 (Hub 收到后几十毫秒内开始动)。

记录里只有时间、路由、识别文字和命令, 不包含音频数据或录音文件路径。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

STAGES = ("recording_end", "stt_done", "route_done", "first_enqueued", "ble_sent", "hub_done")

ROUTE_LABELS = {
    "fast_path": "快速通道",
    "stop": "急停",
    "llm": "LLM",
    "realtime": "Realtime",
    "task": "任务层",  # 目标级请求交给任务层 (改动 task-layer)
    "answer": "回答提问",  # 任务代理反问时的回答
}

STAGE_LABELS = {
    "stt": "STT",
    "llm": "LLM",
    "realtime": "Realtime",
    "route": "路由",
    "ble": "BLE",
    "hub": "Hub",
}

LATENCY_LOG_ENV = "VOICE_ROBOT_LATENCY_LOG"


@dataclass
class CommandTimer:
    input_mode: str = ""
    route: str | None = None
    text: str = ""
    commands: list[str] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    failed_stage: str | None = None
    error: str | None = None

    def mark(self, stage: str, at: float | None = None) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        # 同一阶段只记第一次 (比如多条命令时只记第一条 BLE 发出)
        self.marks.setdefault(stage, time.perf_counter() if at is None else at)

    def fail(self, stage: str, error: str) -> None:
        if self.failed_stage is None:
            self.failed_stage = stage
            self.error = error

    def durations_ms(self) -> dict[str, int | None]:
        m = self.marks
        start = m.get("recording_end")
        route_start = m.get("stt_done", start)
        return {
            "stt": _diff_ms(start, m.get("stt_done")),
            "route": _diff_ms(route_start, m.get("route_done")),
            "ble": _diff_ms(m.get("route_done"), m.get("ble_sent")),
            "hub": _diff_ms(m.get("ble_sent"), m.get("hub_done")),
        }

    def first_action_ms(self) -> dict[str, int | None]:
        """从说完 (recording_end) 到第一个动作入队 / 发出; 流式模式下可能是负数。"""
        start = self.marks.get("recording_end")
        return {
            "enqueued": _diff_ms(start, self.marks.get("first_enqueued")),
            "sent": _diff_ms(start, self.marks.get("ble_sent")),
        }

    def total_ms(self) -> int | None:
        start = self.marks.get("recording_end")
        if start is None or len(self.marks) < 2:
            return None
        return _diff_ms(start, max(self.marks.values()))

    def summary_line(self) -> str:
        d = self.durations_ms()
        route_label = ROUTE_LABELS.get(self.route or "", self.route or "-")
        route_segment = "LLM" if self.route == "llm" else route_label
        parts = [
            f"[耗时] 路由={route_label}",
            f"STT {_fmt(d['stt'])}",
            f"{route_segment} {_fmt(d['route'])}",
            f"BLE发出 {_fmt(d['ble'])}",
            f"Hub完成 {_fmt(d['hub'])}",
            f"总计 {_fmt(self.total_ms())}",
        ]
        first = self.first_action_ms()
        if first["enqueued"] is not None or first["sent"] is not None:
            parts.append(
                f"说完→首个动作 入队 {_fmt(first['enqueued'])} / 发出 {_fmt(first['sent'])}"
            )
        if self.commands:
            parts.append(", ".join(self.commands))
        if self.failed_stage:
            label = STAGE_LABELS.get(self.failed_stage, self.failed_stage)
            parts.append(f"失败环节={label} ({self.error})")
        return " | ".join(parts)

    def to_record(self) -> dict[str, Any]:
        return {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "input_mode": self.input_mode,
            "route": self.route,
            "durations_ms": self.durations_ms(),
            "first_action_ms": self.first_action_ms(),
            "total_ms": self.total_ms(),
            "text": self.text,
            "commands": list(self.commands),
            "failed_stage": self.failed_stage,
            "error": self.error,
        }


class LatencyLogger:
    """把耗时记录以 JSON 行追加写入本地文件。path 为空时什么都不写。"""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = os.getenv(LATENCY_LOG_ENV) or None
        self.path = Path(path) if path else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, timer: CommandTimer) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(timer.to_record(), ensure_ascii=False) + "\n")


def _diff_ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start) * 1000)


def _fmt(value: int | None) -> str:
    return "-" if value is None else f"{value}ms"
