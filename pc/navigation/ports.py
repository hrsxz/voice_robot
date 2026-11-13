"""
导航、抓取和世界状态用的三个接口 (改动 navigation-grasp 设计 D1), 以及包装真实系统的适配器。

- PoseSource: 小车位姿 (场地坐标, 可能看不见)
- ObjectSource: 物体检测和跟踪结果
- HubPort: 发送一条命令、等它完成; 随时可以停止

模拟器 (pc/sim) 实现同样的三个接口, 所以导航和抓取代码不用改就能从模拟器切换到实车。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from pc.field.geometry import Point, Pose


@dataclass(frozen=True)
class PoseReading:
    pose: Pose | None  # None = 看不见 marker
    timestamp: float  # 测量时刻 (秒, 和 PoseSource.clock 同一个时钟)
    last_pose: Pose | None = None  # 看不见时, 最后一次看到的位姿

    @property
    def visible(self) -> bool:
        return self.pose is not None


@dataclass(frozen=True)
class ObjectReading:
    id: str
    position: Point  # 场地坐标 (看不见时是最后一次看到的位置)
    visible: bool
    timestamp: float


@dataclass(frozen=True)
class HubResult:
    kind: str  # DONE / STOPPED / ERR / TIMEOUT
    cmd: str
    gripper: tuple[int, int] | None = None  # (左, 右), 0 = 放下, 90 = 抬起
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "DONE"


class PoseSource(Protocol):
    def clock(self) -> float: ...
    async def read_pose(self) -> PoseReading: ...


class ObjectSource(Protocol):
    async def read_objects(self) -> dict[str, ObjectReading]:
        """已经认出的物体; 开局就没认出的物体不在结果里。"""
        ...


class HubPort(Protocol):
    async def run(self, cmd: str) -> HubResult:
        """发送一条命令, 等它完成 (DONE)、被打断 (STOPPED) 或被拒绝 (ERR)。"""
        ...

    async def stop(self) -> None:
        """立即停止 (插队, 正在执行的动作被打断)。"""
        ...


# ---------------------------------------------------------------- 适配器


class SpikeHubPort:
    """包装 pc.spike_communication.spikehub.SpikeHub (协议 v2)。"""

    def __init__(self, hub: Any) -> None:
        self.hub = hub

    async def run(self, cmd: str) -> HubResult:
        try:
            await self.hub.send(cmd)
        except Exception as exc:
            return HubResult("TIMEOUT", cmd, reason=f"发送失败：{exc}")
        reply = await self.hub.wait_done()
        if reply is None:
            return HubResult("TIMEOUT", cmd, reason="等待 Hub 完成超时")
        return HubResult(reply.kind, cmd, reply.gripper, reply.reason_text or "")

    async def stop(self) -> None:
        await self.hub.send_stop()


class VisionPoseSource:
    """
    包装感知模块 (vision-field-model) 的小车定位。localizer.latest() 返回一个对象:
    found (bool)、x / y (厘米)、heading (度)、timestamp (秒, time.monotonic)。
    """

    def __init__(self, localizer: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.localizer = localizer
        self._clock = clock
        self._last: Pose | None = None

    def clock(self) -> float:
        return self._clock()

    async def read_pose(self) -> PoseReading:
        m = self.localizer.latest()
        if m is None or not m.found:
            ts = getattr(m, "timestamp", None) if m is not None else None
            return PoseReading(None, ts if ts is not None else self._clock(), self._last)
        pose = Pose(float(m.x), float(m.y), float(m.heading) % 360)
        self._last = pose
        return PoseReading(pose, float(m.timestamp), pose)


class VisionObjectSource:
    """
    包装感知模块的物体跟踪。tracker.latest_objects() 返回可迭代的对象:
    id、x / y (厘米)、visible (bool)、timestamp。
    """

    def __init__(self, tracker: Any) -> None:
        self.tracker = tracker

    async def read_objects(self) -> dict[str, ObjectReading]:
        items: Iterable[Any] = self.tracker.latest_objects() or ()
        return {
            str(o.id): ObjectReading(str(o.id), (float(o.x), float(o.y)), bool(o.visible),
                                     float(o.timestamp))
            for o in items
        }


__all__ = [
    "HubPort", "HubResult", "ObjectReading", "ObjectSource", "PoseReading", "PoseSource",
    "SpikeHubPort", "VisionObjectSource", "VisionPoseSource",
]
