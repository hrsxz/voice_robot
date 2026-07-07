from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Detection:
    found: bool
    center: Point | None = None
    area: float = 0.0
    bbox: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class RobotPose:
    found: bool
    marker_id: int | None = None
    center: Point | None = None
    heading_deg: float | None = None
    corners: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class WorldState:
    robot: RobotPose
    red_block: Detection
    frame_width: int
    frame_height: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
