"""
合成摄像头画面 (改动 vision-field-model: 标定、定位、检测都用它做测试;
以后也可以给模拟器当"摄像头")。

- field_texture: 场地垫的俯视图纹理 (白底、彩色区域和圆圈、黑线、起终点区黑框、灰色方格),
  外面一圈是桌面, 场外的 anchor marker 也画在纹理上 (它们贴在桌面上, 高度为 0)。
- render_scene: 按相机模型 (内参含畸变 + 位姿) 渲染画面: 地面逐像素按视线和地面的交点取纹理;
  有高度的东西 (小车车身、物体) 按三维投影画出; 车顶 marker 逐像素按视线和 marker 平面的交点取图案。
- render_board: 棋盘格标定照片。
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import cv2
import numpy as np

from pc.field.geometry import Point, Pose
from pc.field.model import FieldModel
from pc.field.robot_geometry import RobotGeometry
from pc.vision.camera_model import CameraModel, Intrinsics
from pc.vision.markers import ANCHOR_UP_DEG, marker_axes, marker_image

# 场地垫上印的颜色 (BGR)
MAT = {
    "white": (238, 240, 242), "yellow": (40, 205, 245), "green": (75, 165, 45),
    "blue": (190, 105, 25), "red": (45, 45, 215), "black": (25, 25, 25), "gray": (175, 175, 175),
}
TABLE = (150, 175, 200)  # 桌面
# 乐高物体的颜色 (和印刷的颜色不完全一样)
LEGO = {"red": (35, 30, 175), "green": (60, 140, 30), "blue": (170, 80, 15),
        "yellow": (30, 190, 235)}
ROBOT_BODY = (55, 55, 60)
LINE_CM = 2.0  # 黑线宽度


@dataclass
class SceneObject:
    id: str
    color: str
    xy: Point
    radius: float
    height: float


@dataclass
class SceneRobot:
    pose: Pose
    geom: RobotGeometry
    marker_id: int = 20
    marker_size: float = 9.0
    marker_height: float = 10.0
    marker_offset: tuple[float, float] = (0.0, 0.0)  # (前, 右)
    marker_rotation: float = 0.0
    visible_marker: bool = True  # False = marker 被挡住


@dataclass
class Scene:
    field: FieldModel
    # id -> (中心, 边长, 旋转)
    anchors: dict[int, tuple[Point, float, float]] = field(default_factory=dict)
    objects: list[SceneObject] = field(default_factory=list)
    robot: SceneRobot | None = None
    brightness: float = 1.0  # 光照 (乘到整幅画面上)


def scene_anchors(fm: FieldModel, size: float = 8.0, rotation: float = 0.0
                  ) -> dict[int, tuple[Point, float, float]]:
    return {i: (p, size, rotation) for i, p in fm.anchors.items()}


# ---------------------------------------------------------------- 场地纹理


def field_texture(fm: FieldModel, ppc: float = 4.0, margin: float = 20.0,
                  anchors: dict[int, tuple[Point, float, float]] | None = None,
                  shifts: dict[str, Point] | None = None) -> np.ndarray:
    """
    场地垫纹理: 像素 (u, v) 对应场地坐标 (u / ppc - margin, v / ppc - margin)。
    shifts: 把某个区域或圆圈平移 (测试区域提取工具用), 例如 {"zone:B": (4, 0)}。
    """
    shifts = shifts or {}
    w = round((fm.width + 2 * margin) * ppc)
    h = round((fm.height + 2 * margin) * ppc)
    img = np.full((h, w, 3), TABLE, np.uint8)
    sh = 4  # 亚像素精度画图

    def pt(p: Point, d: Point = (0.0, 0.0)) -> tuple[int, int]:
        return (round((p[0] + d[0] + margin) * ppc * (1 << sh)),
                round((p[1] + d[1] + margin) * ppc * (1 << sh)))

    def poly(pts: Iterable[Point], color, d: Point = (0.0, 0.0)) -> None:
        cv2.fillPoly(img, [np.array([pt(p, d) for p in pts], np.int32)], color, cv2.LINE_AA, sh)

    def line(a: Point, b: Point, width_cm: float = LINE_CM) -> None:
        cv2.line(img, pt(a), pt(b), MAT["black"], max(1, round(width_cm * ppc)),
                 cv2.LINE_AA, sh)

    poly([(0, 0), (fm.width, 0), (fm.width, fm.height), (0, fm.height)], MAT["white"])
    for z in fm.zones.values():
        poly(z.polygon, MAT[z.color], shifts.get(f"zone:{z.id}", (0.0, 0.0)))
    for a, b, _ in fm.road_edges:
        line(fm.road_nodes[a], fm.road_nodes[b])
    for curve in fm.curves.values():
        for a, b in itertools.pairwise(curve):
            line(a, b)
    sp = fm.start_polygon
    for a, b in zip(sp, [*sp[1:], sp[0]], strict=True):
        line(a, b, 1.0)
    for p in fm.positions.values():
        poly([(p.x - 2.5, p.y - 2.5), (p.x + 2.5, p.y - 2.5), (p.x + 2.5, p.y + 2.5),
              (p.x - 2.5, p.y + 2.5)], MAT["gray"])
    for c in fm.circles.values():
        d = shifts.get(f"circle:{c.id}", (0.0, 0.0))
        cv2.circle(img, pt(c.center, d), round(c.radius * ppc * (1 << sh)), MAT[c.color],
                   -1, cv2.LINE_AA, sh)
        cv2.circle(img, pt(c.center, d), round(c.radius * ppc * (1 << sh)), MAT["black"],
                   max(1, round(0.4 * ppc)), cv2.LINE_AA, sh)
    for mid, (center, size, rot) in (anchors or {}).items():
        _paint_marker_affine(img, mid, center, ANCHOR_UP_DEG + rot, size, ppc, margin)
    return img


def _paint_marker_affine(img: np.ndarray, marker_id: int, center: Point, up_deg: float,
                         size: float, ppc: float, margin: float) -> None:
    """在纹理 (场地平面的线性映射) 上画 marker (含白边)。"""
    n = 120
    mk = marker_image(marker_id, n)
    full = size * mk.shape[0] / n  # 含白边的边长 (厘米)
    up, right = marker_axes(up_deg)
    c = np.asarray(center, float)
    tl = c + up * full / 2 - right * full / 2
    tr = c + up * full / 2 + right * full / 2
    bl = c - up * full / 2 - right * full / 2
    src = np.float32([[0, 0], [mk.shape[1], 0], [0, mk.shape[0]]])
    dst = np.float32([(p + margin) * ppc for p in (tl, tr, bl)])
    M = cv2.getAffineTransform(src, dst)
    warped = cv2.warpAffine(cv2.cvtColor(mk, cv2.COLOR_GRAY2BGR), M, (img.shape[1], img.shape[0]),
                            flags=cv2.INTER_AREA, borderValue=(0, 0, 0))
    mask = cv2.warpAffine(np.full(mk.shape, 255, np.uint8), M, (img.shape[1], img.shape[0]),
                          flags=cv2.INTER_LINEAR)
    m = mask > 127
    img[m] = warped[m]


# ---------------------------------------------------------------- 渲染画面


def render_scene(model: CameraModel, scene: Scene, ppc: float = 6.0, margin: float = 40.0,
                 noise: float = 0.0, blur: float = 0.0, seed: int = 0,
                 texture: np.ndarray | None = None) -> np.ndarray:
    """按相机模型渲染整幅画面 (BGR, 尺寸 = 内参的分辨率)。"""
    tex = texture if texture is not None else field_texture(scene.field, ppc, margin,
                                                             scene.anchors)
    g = model.plane_grid(0.0)
    mx = ((g[..., 0] + margin) * ppc).astype(np.float32)
    my = ((g[..., 1] + margin) * ppc).astype(np.float32)
    bad = ~np.isfinite(mx) | ~np.isfinite(my)
    mx[bad] = -1
    my[bad] = -1
    img = cv2.remap(tex, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                    borderValue=TABLE)

    # 有高度的东西: 从远到近画
    cx, cy, _ = model.center
    items: list[tuple[float, object]] = [(math.hypot(o.xy[0] - cx, o.xy[1] - cy), o)
                                         for o in scene.objects]
    if scene.robot is not None:
        r = scene.robot.pose
        items.append((math.hypot(r.x - cx, r.y - cy), scene.robot))
    for _d, item in sorted(items, key=lambda t: -t[0]):
        if isinstance(item, SceneObject):
            _draw_cylinder(img, model, item)
        else:
            _draw_robot(img, model, item)  # type: ignore[arg-type]

    if scene.brightness != 1.0:
        img = np.clip(img.astype(np.float32) * scene.brightness, 0, 255).astype(np.uint8)
    if blur > 0:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if noise > 0:
        rng = np.random.default_rng(seed)
        img = np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    return img


def _fill(img: np.ndarray, pts: np.ndarray, color) -> None:
    hull = cv2.convexHull(np.round(pts * 16).astype(np.int32))
    cv2.fillPoly(img, [hull], color, cv2.LINE_AA, 4)


def _draw_cylinder(img: np.ndarray, model: CameraModel, o: SceneObject) -> None:
    angles = np.linspace(0, 2 * np.pi, 32, endpoint=False)
    ring = np.stack([o.xy[0] + o.radius * np.cos(angles), o.xy[1] + o.radius * np.sin(angles)],
                    axis=1)
    base = model.project(np.hstack([ring, np.zeros((32, 1))]))
    top = model.project(np.hstack([ring, np.full((32, 1), o.height)]))
    color = np.array(LEGO.get(o.color, (128, 128, 128)), float)
    _fill(img, np.vstack([base, top]), tuple(int(v) for v in color * 0.75))
    _fill(img, top, tuple(int(v) for v in color))
    cv2.polylines(img, [np.round(top * 16).astype(np.int32)], True, (20, 20, 20), 1,
                  cv2.LINE_AA, 4)


def _draw_robot(img: np.ndarray, model: CameraModel, r: SceneRobot) -> None:
    body = np.array(r.geom.body_polygon(r.pose))
    h = r.marker_height
    pts = np.vstack([model.project(np.hstack([body, np.zeros((4, 1))])),
                     model.project(np.hstack([body, np.full((4, 1), h)]))])
    _fill(img, pts, ROBOT_BODY)
    if not r.visible_marker:
        return
    mc = np.array(r.pose.to_world(*r.marker_offset))
    _paint_marker_plane(img, model, r.marker_id, mc, r.pose.heading + r.marker_rotation,
                        r.marker_size, h)


def _paint_marker_plane(img: np.ndarray, model: CameraModel, marker_id: int, center: np.ndarray,
                        up_deg: float, size: float, height: float) -> None:
    """在画面上画一个水平放置、离地 height 的 marker: 逐像素求视线和 marker 平面的交点。"""
    n = 120
    mk = marker_image(marker_id, n)
    full = size * mk.shape[0] / n
    up, right = marker_axes(up_deg)
    corners = [center + up * a * full / 2 + right * b * full / 2
               for a, b in ((1, -1), (1, 1), (-1, 1), (-1, -1))]
    px = model.project(np.hstack([np.array(corners), np.full((4, 1), height)]))
    x0, y0 = np.floor(px.min(axis=0)).astype(int) - 2
    x1, y1 = np.ceil(px.max(axis=0)).astype(int) + 2
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, model.intr.width), min(y1, model.intr.height)
    if x1 <= x0 or y1 <= y0:
        return
    # 每个像素 4×4 超采样, 边缘更真实
    ss = 4
    u, v = np.meshgrid(x0 + (np.arange((x1 - x0) * ss) + 0.5) / ss - 0.5,
                       y0 + (np.arange((y1 - y0) * ss) + 0.5) / ss - 0.5)
    pts = model.pixel_to_plane(np.stack([u.ravel(), v.ravel()], axis=1), height)
    d = pts - center
    fwd = d @ up
    rt = d @ right
    col = (rt + full / 2) / full * mk.shape[1]
    row = (full / 2 - fwd) / full * mk.shape[0]
    inside = (col >= 0) & (col < mk.shape[1]) & (row >= 0) & (row < mk.shape[0])
    vals = np.zeros(len(col))
    ci = np.clip(col.astype(int), 0, mk.shape[1] - 1)
    ri = np.clip(row.astype(int), 0, mk.shape[0] - 1)
    vals[inside] = mk[ri[inside], ci[inside]]
    hgt, wid = u.shape
    cover = inside.reshape(hgt, wid).astype(float).reshape(hgt // ss, ss, wid // ss, ss).mean(
        axis=(1, 3))
    value = vals.reshape(hgt, wid).reshape(hgt // ss, ss, wid // ss, ss).sum(axis=(1, 3)) / (
        ss * ss)
    region = img[y0:y1, x0:x1].astype(float)
    gray = np.where(cover > 0, value / np.maximum(cover, 1e-9), 0)
    blended = region * (1 - cover[..., None]) + (gray * cover)[..., None]
    img[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 棋盘格


def board_texture(pattern: tuple[int, int] = (9, 6), square: float = 25.0,
                  ppu: float = 4.0) -> tuple[np.ndarray, float]:
    """棋盘格纹理 (内角点 pattern, 格子边长 square 毫米), 外圈一格白边。返回 (纹理, 每毫米像素)。"""
    cols, rows = pattern[0] + 1, pattern[1] + 1
    cell = round(square * ppu)
    img = np.full(((rows + 2) * cell, (cols + 2) * cell), 255, np.uint8)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                img[(r + 1) * cell:(r + 2) * cell, (c + 1) * cell:(c + 2) * cell] = 0
    return img, ppu


def render_board(intr: Intrinsics, rvec: Sequence[float], tvec: Sequence[float],
                 pattern: tuple[int, int] = (9, 6), square: float = 25.0,
                 background: int = 120) -> np.ndarray:
    """
    棋盘格照片 (灰度)。棋盘坐标: 原点在第一个内角点, x 沿行、y 沿列, 单位毫米;
    相机坐标 X_cam = R · (x, y, 0) + t。
    """
    tex, ppu = board_texture(pattern, square)
    R, _ = cv2.Rodrigues(np.asarray(rvec, float))
    t = np.asarray(tvec, float).reshape(3)
    rays = intr.ray_grid().reshape(-1, 2).astype(np.float64)
    d = np.concatenate([rays, np.ones((len(rays), 1))], axis=1)
    n = R[:, 2]
    s = (n @ t) / (d @ n)
    X = d * s[:, None] - t
    uv = X @ R  # R^T (X - t)
    origin = 2 * square * ppu  # 第一个内角点在纹理里的位置 (白边一格 + 一格)
    mx = (uv[:, 0] * ppu + origin).astype(np.float32).reshape(intr.height, intr.width)
    my = (uv[:, 1] * ppu + origin).astype(np.float32).reshape(intr.height, intr.width)
    bad = (s <= 0).reshape(intr.height, intr.width)
    mx[bad] = -1
    my[bad] = -1
    return cv2.remap(tex, mx, my, cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT,
                     borderValue=background)


def default_camera(intr: Intrinsics | None = None, fm: FieldModel | None = None,
                   eye: tuple[float, float, float] = (118.0, 215.0, 175.0)) -> CameraModel:
    """测试用的三脚架位置: 在 D 区那一侧 (场地下方) 架高斜拍, 对准场地中心。"""
    intr = intr or Intrinsics.from_fov(1920, 1080, 70.0, dist=[-0.12, 0.05, 0, 0, 0])
    width = fm.width if fm else 236.2
    height = fm.height if fm else 114.3
    return CameraModel.look_at(intr, eye, (width / 2, height / 2 + 12.0, 0.0))


__all__ = ["LEGO", "MAT", "TABLE", "Scene", "SceneObject", "SceneRobot", "board_texture",
           "default_camera", "field_texture", "render_board", "render_scene", "scene_anchors"]
