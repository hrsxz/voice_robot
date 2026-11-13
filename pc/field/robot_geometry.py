"""
小车的尺寸 (车身坐标系, 原点在转弯中心, x 向车头, y 向右侧, 单位厘米)。

默认值是估计值: 笼区内宽、内深和车身尺寸还没有实测 (见 docs/roadmap.md "需要你提供的资料"),
实测后改 pc/config/robot_geometry.yaml 即可, 不用改代码。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from pc import constants
from pc.field.geometry import Point, Pose, rect_polygon

CONFIG_PATH = constants.project_root_path / "pc" / "config" / "robot_geometry.yaml"


@dataclass(frozen=True)
class RobotGeometry:
    body_front: float = 9.0  # 转弯中心到车头 (不含夹爪)
    body_rear: float = 8.0  # 转弯中心到车尾
    half_width: float = 7.5  # 车身半宽
    cage_offset: float = 15.0  # 转弯中心到笼区中心
    cage_inner_depth: float = 11.0  # 笼区内深 (沿车头方向)
    cage_inner_width: float = 13.0  # 笼区内宽
    cage_wall: float = 1.0  # 夹爪 (笼壁) 的厚度
    raised_front: float = 10.0  # 夹爪抬起时 (竖着), 车最前端到转弯中心的距离

    # --- 车身坐标系里的矩形 (x0, x1, y0, y1) ---

    @property
    def body_rect(self) -> tuple[float, float, float, float]:
        return (-self.body_rear, self.body_front, -self.half_width, self.half_width)

    @property
    def cage_inner_rect(self) -> tuple[float, float, float, float]:
        d, w = self.cage_inner_depth / 2, self.cage_inner_width / 2
        return (self.cage_offset - d, self.cage_offset + d, -w, w)

    @property
    def cage_front(self) -> float:
        """笼壁最前端到转弯中心的距离。"""
        return self.cage_offset + self.cage_inner_depth / 2 + self.cage_wall

    @property
    def footprint_rect(self) -> tuple[float, float, float, float]:
        """夹爪放下时整辆车 (车身 + 笼区) 的外接矩形。"""
        w = max(self.half_width, self.cage_inner_width / 2 + self.cage_wall)
        return (-self.body_rear, self.cage_front, -w, w)

    @property
    def raised_rect(self) -> tuple[float, float, float, float]:
        """夹爪抬起时 (夹爪竖着, 不往前伸) 整辆车的外接矩形。"""
        w = max(self.half_width, self.cage_inner_width / 2 + self.cage_wall)
        return (-self.body_rear, max(self.body_front, self.raised_front), -w, w)

    def sweep_radius(self, gripper_down: bool = True) -> float:
        """原地转向时扫过的半径。"""
        x0, x1, _y0, y1 = self.footprint_rect if gripper_down else self.raised_rect
        return max((x0 * x0 + y1 * y1) ** 0.5, (x1 * x1 + y1 * y1) ** 0.5)

    # --- 场地坐标系 ---

    def body_polygon(self, pose: Pose) -> list[Point]:
        return rect_polygon(pose, *self.body_rect)

    def footprint_polygon(self, pose: Pose, gripper_down: bool = True) -> list[Point]:
        """整辆车的外形: 夹爪放下时包括笼区, 抬起时只有车身 (和竖着的夹爪)。"""
        return rect_polygon(pose, *(self.footprint_rect if gripper_down else self.raised_rect))

    def cage_center(self, pose: Pose) -> Point:
        return pose.to_world(self.cage_offset, 0.0)

    def cage_polygon(self, pose: Pose) -> list[Point]:
        return rect_polygon(pose, *self.cage_inner_rect)

    def cage_tolerance(self, object_radius: float, margin: float = 0.5) -> float:
        """物体中心离笼区中线最多偏多少, 放下夹爪时还能完全在笼区里 (设计 D5)。"""
        return (self.cage_inner_width - 2 * object_radius) / 2 - margin

    @classmethod
    def load(cls, path: str | Path | None = None) -> RobotGeometry:
        """读取配置文件 (没有文件时用默认值)。"""
        p = Path(path) if path else CONFIG_PATH
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{p}: 未知的尺寸参数 {sorted(unknown)}")
        return cls(**{k: float(v) for k, v in data.items()})
