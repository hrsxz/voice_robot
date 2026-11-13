"""
抓取一个物体时小车从哪个方向接近、停在哪里 (改动 navigation-grasp 设计 D5, 任务 3.1)。

- 抓取位姿: 物体在笼区里 (左右居中), 夹爪放下时的小车位姿。优先让物体在笼区中心;
  靠墙时让物体靠近笼区前端, 小车就可以停得离墙远一点。
- 预抓取位姿: 抓取位姿沿车头反方向退 PREGRASP_BACKOFF, 在这里抬起夹爪、对准物体。
- 可行: 从预抓取到抓取的整段直线接近 (夹爪抬着), 车身都在场地内, 不碰启用的禁行区和其他物体;
  在抓取位姿放下夹爪时, 笼区也不越界、不压到别的东西。

命名位置点可以在场地文件里指定接近方向 (靠墙的位置必须从场地内侧接近);
没有指定时 (包括任意坐标) 在 24 个方向里自动选离障碍最远的那个。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pc.field.geometry import Point, Pose, Shape, heading_vector, polygon_outside_rect
from pc.field.robot_geometry import RobotGeometry

PREGRASP_BACKOFF = 15.0  # 预抓取点在抓取位姿后方多远 (设计 D5: 物体前方约 15 cm)
# 地方不够 (例如左边 1–4 号位和障碍杆之间) 时逐步缩短
BACKOFF_OPTIONS = (PREGRASP_BACKOFF, 12.0, 10.0, 8.0, 5.0)
EDGE_MARGIN = 0.5  # 车身离场地边界至少多远
OBSTACLE_MARGIN = 1.0  # 车身离其他物体至少多远
AUTO_STEP_DEG = 15
SAMPLE_STEP = 2.0


@dataclass(frozen=True)
class GraspPlan:
    heading: float
    grasp: Pose  # 放下夹爪时的位姿
    pregrasp: Pose  # 抬起夹爪、对准物体的位姿
    depth: float  # 抓取位姿下, 物体中心在车身坐标系里离转弯中心多远
    clearance: float  # 接近过程中离边界 / 障碍的最小距离


def _poses_along(a: Pose, b: Pose) -> list[Pose]:
    """从 a 到 b (同一朝向, 沿车头方向) 每隔 SAMPLE_STEP 取一个位姿。"""
    hx, hy = heading_vector(a.heading)
    length = (b.x - a.x) * hx + (b.y - a.y) * hy
    n = max(1, int(abs(length) // SAMPLE_STEP) + 1)
    return [a.moved(length * i / n) for i in range(n + 1)]


def _clearance(
    poses: Iterable[Pose], geom: RobotGeometry, width: float, height: float,
    obstacles: Sequence[Shape], target: Shape | None, gripper_down: bool = False,
) -> float:
    """这些位姿下车身离场地边界和障碍的最小距离 (负数 = 越界或碰撞)。"""
    best = float("inf")
    for pose in poses:
        foot = geom.footprint_polygon(pose, gripper_down)
        out = polygon_outside_rect(foot, width, height, EDGE_MARGIN)
        if out > 1e-6:
            return -out
        # 离边界还有多远
        xs = [p[0] for p in foot]
        ys = [p[1] for p in foot]
        best = min(best, min(xs) - EDGE_MARGIN, width - EDGE_MARGIN - max(xs),
                   min(ys) - EDGE_MARGIN, height - EDGE_MARGIN - max(ys))
        for ob in obstacles:
            d = ob.distance_to_polygon(foot) - OBSTACLE_MARGIN
            if d < 0:
                return d
            best = min(best, d)
        if target is not None:
            # 要抓的物体只能进笼区, 不能碰到车身 (夹爪抬着, 笼区是空的)
            d = target.distance_to_polygon(geom.body_polygon(pose)) - OBSTACLE_MARGIN
            if d < 0:
                return d
    return best


def plan_for_heading(
    obj: Point, radius: float, heading: float, geom: RobotGeometry,
    width: float, height: float, obstacles: Sequence[Shape] = (),
) -> GraspPlan | None:
    """按给定的接近方向算抓取和预抓取位姿; 不可行时返回 None。"""
    hx, hy = heading_vector(heading)
    target = Shape("_target", center=obj, radius=radius)
    x0, x1, _y0, _y1 = geom.cage_inner_rect
    deepest = x1 - radius - EDGE_MARGIN  # 物体最靠前 (贴近笼区前端) 时离转弯中心的距离
    depths = [geom.cage_offset]
    d = geom.cage_offset + 0.5
    while d <= deepest:
        depths.append(d)
        d += 0.5
    if deepest < x0 + radius:
        return None  # 物体比笼区还大
    for backoff in BACKOFF_OPTIONS:
        for depth in depths:
            grasp = Pose(obj[0] - hx * depth, obj[1] - hy * depth, heading % 360)
            pregrasp = grasp.moved(-backoff)
            # 接近时夹爪抬着; 到了抓取位姿放下夹爪, 笼区不能压到墙和其他物体
            c = min(
                _clearance(_poses_along(pregrasp, grasp), geom, width, height, obstacles, target),
                _clearance([grasp], geom, width, height, obstacles, None, gripper_down=True),
            )
            if c >= -1e-6:  # 正好贴着边界也算可行 (浮点误差)
                return GraspPlan(heading % 360, grasp, pregrasp, depth, max(c, 0.0))
    return None


def choose_approach(
    obj: Point, radius: float, geom: RobotGeometry, width: float, height: float,
    obstacles: Sequence[Shape] = (), heading: float | None = None,
) -> GraspPlan | None:
    """
    选接近方向。heading 给定时只检查这个方向; 否则在 24 个方向里选离障碍最远的
    (正东南西北方向优先一点, 和场地上的线对齐, 更容易讲解)。都不可行时返回 None。
    """
    if heading is not None:
        return plan_for_heading(obj, radius, heading, geom, width, height, obstacles)
    best: tuple[float, GraspPlan] | None = None
    for h in range(0, 360, AUTO_STEP_DEG):
        plan = plan_for_heading(obj, radius, float(h), geom, width, height, obstacles)
        if plan is None:
            continue
        score = min(plan.clearance, 10.0) + (1.0 if h % 90 == 0 else 0.0)
        if best is None or score > best[0]:
            best = (score, plan)
    return best[1] if best else None


def keepout_shapes(field) -> list[Shape]:
    """场地模型里启用的禁行区。"""
    return [Shape(k.id, center=k.center, radius=k.radius, polygon=k.polygon)
            for k in field.enabled_keepouts()]


def approach_errors(field, geom: RobotGeometry | None = None) -> list[str]:
    """检查场地文件里每个指定的接近方向都可行 (只看场地边界和启用的禁行区)。"""
    geom = geom or RobotGeometry.load()
    errors = []
    # 用目录里最小的物体检查: 方向至少对能放在这里的物体可行;
    # 运行时按实际物体的大小重新检查, 不可行时自动换方向
    radius = min((o.radius_cm for o in field.objects.values()), default=3.0)
    obstacles = keepout_shapes(field)
    for pos in field.positions.values():
        if pos.approach is None:
            continue
        if plan_for_heading(pos.xy, radius, pos.approach, geom,
                            field.width, field.height, obstacles) is None:
            errors.append(f"位置点 {pos.id} 的接近方向 {pos.approach:g}° 不可行"
                          "（车身会越出场地或碰到禁行区）")
    return errors
