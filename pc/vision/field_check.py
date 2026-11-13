"""
用参考图核对场地模型 (改动 vision-field-model 设计 D6, 任务 3.3)。

场地坐标是从任务书照片里量的 (约 ±2 cm)。摄像头到位后, 从空场地的矫正俯视图里自动提取
四个彩色区域 (A–D) 和四个圆圈, 和 wro_field.yaml 对比, 偏差超过 2 cm 的地方报告出来。
- 区域: 在 YAML 区域周围 8 cm 的范围里找同色的最大连通区域, 比较左、右、上、下四条边;
- 圆圈: 在 YAML 圆心周围找同色区域, 比较圆心和半径。

用法 (需要已经拍好的参考图, 见 README 第 15 节):
    python -m pc.vision.field_check
    python -m pc.vision.field_check --reference 俯视图.png
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import cv2
import numpy as np

from pc.field.model import FieldModel, load_field
from pc.vision.objects import HUES

MAX_DEVIATION_CM = 2.0
SEARCH_CM = 8.0
MIN_SAT = 80


@dataclass(frozen=True)
class Deviation:
    element: str  # "zone:B" / "circle:red"
    what: str  # 左边 / 右边 / 上边 / 下边 / 圆心 / 半径 / 没找到
    expected: float | None
    measured: float | None
    deviation_cm: float

    def describe(self) -> str:
        if self.measured is None:
            return f"{self.element}：没有在参考图里找到"
        return (f"{self.element} {self.what}：场地模型 {self.expected:.1f}，"
                f"参考图 {self.measured:.1f}，相差 {self.deviation_cm:.1f} cm")


def color_mask(img: np.ndarray, color: str) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = np.zeros(img.shape[:2], bool)
    for lo, hi in HUES[color]:
        m |= (hsv[..., 0] >= lo) & (hsv[..., 0] < hi)
    m &= (hsv[..., 1] >= MIN_SAT) & (hsv[..., 2] >= 50)
    return m.astype(np.uint8) * 255


def _window(fm: FieldModel, xs, ys, ppc: float, margin: float, search: float):
    x0 = max(0, int((min(xs) - search + margin) * ppc))
    y0 = max(0, int((min(ys) - search + margin) * ppc))
    x1 = int((max(xs) + search + margin) * ppc)
    y1 = int((max(ys) + search + margin) * ppc)
    return x0, y0, x1, y1


def _largest(mask: np.ndarray) -> np.ndarray | None:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < 20:
        return None
    return (labels == i).astype(np.uint8)


def measure(reference: np.ndarray, fm: FieldModel, ppc: float, margin: float = 0.0
            ) -> dict[str, dict[str, float]]:
    """从参考图里量出每个区域的四条边 / 每个圆的圆心和半径 (场地坐标)。"""
    out: dict[str, dict[str, float]] = {}
    for z in fm.zones.values():
        xs = [p[0] for p in z.polygon]
        ys = [p[1] for p in z.polygon]
        x0, y0, x1, y1 = _window(fm, xs, ys, ppc, margin, SEARCH_CM)
        comp = _largest(color_mask(reference[y0:y1, x0:x1], z.color))
        if comp is None:
            continue
        rows, cols = np.nonzero(comp)
        out[f"zone:{z.id}"] = {
            "左边": (cols.min() + x0) / ppc - margin, "右边": (cols.max() + 1 + x0) / ppc - margin,
            "上边": (rows.min() + y0) / ppc - margin, "下边": (rows.max() + 1 + y0) / ppc - margin,
        }
    for c in fm.circles.values():
        cx, cy = c.center
        x0, y0, x1, y1 = _window(fm, [cx - c.radius, cx + c.radius],
                                 [cy - c.radius, cy + c.radius], ppc, margin, SEARCH_CM)
        comp = _largest(color_mask(reference[y0:y1, x0:x1], c.color))
        if comp is None:
            continue
        rows, cols = np.nonzero(comp)
        mx = (cols.mean() + 0.5 + x0) / ppc - margin
        my = (rows.mean() + 0.5 + y0) / ppc - margin
        r = float(np.sqrt(len(rows) / np.pi)) / ppc
        out[f"circle:{c.id}"] = {"x": mx, "y": my, "半径": r}
    return out


def check_field(reference: np.ndarray, fm: FieldModel, ppc: float, margin: float = 0.0,
                max_dev: float = MAX_DEVIATION_CM) -> list[Deviation]:
    """和场地模型对比, 返回偏差超过 max_dev 的地方 (空列表 = 一致)。"""
    meas = measure(reference, fm, ppc, margin)
    devs: list[Deviation] = []
    for z in fm.zones.values():
        key = f"zone:{z.id}"
        if key not in meas:
            devs.append(Deviation(key, "没找到", None, None, float("inf")))
            continue
        xs = [p[0] for p in z.polygon]
        ys = [p[1] for p in z.polygon]
        expected = {"左边": min(xs), "右边": max(xs), "上边": min(ys), "下边": max(ys)}
        for side, e in expected.items():
            m = meas[key][side]
            # 贴着场地边的边 (在俯视图之外) 量不出来, 不比较
            if (side == "左边" and e <= 0) or (side == "上边" and e <= 0) or \
                    (side == "右边" and e >= fm.width) or (side == "下边" and e >= fm.height):
                continue
            if abs(m - e) > max_dev:
                devs.append(Deviation(key, side, e, m, abs(m - e)))
    for c in fm.circles.values():
        key = f"circle:{c.id}"
        if key not in meas:
            devs.append(Deviation(key, "没找到", None, None, float("inf")))
            continue
        m = meas[key]
        d = float(np.hypot(m["x"] - c.center[0], m["y"] - c.center[1]))
        if d > max_dev:
            devs.append(Deviation(key, "圆心", 0.0, d, d))
        if abs(m["半径"] - c.radius) > max_dev:
            devs.append(Deviation(key, "半径", c.radius, m["半径"], abs(m["半径"] - c.radius)))
    return devs


def main(argv: list[str] | None = None) -> int:
    from pc.vision.config import VisionConfig

    p = argparse.ArgumentParser(prog="python -m pc.vision.field_check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reference", default=None, help="矫正俯视图 PNG（默认用保存的参考图）")
    args = p.parse_args(argv)
    cfg = VisionConfig.load()
    path = args.reference or cfg.reference_path
    img = cv2.imread(str(path))
    if img is None:
        print(f"读不到参考图 {path}")
        return 2
    fm = load_field()
    devs = check_field(img, fm, cfg.px_per_cm, cfg.margin_cm)
    if not devs:
        print(f"参考图和场地模型一致（偏差都不超过 {MAX_DEVIATION_CM:.0f} cm）")
        return 0
    print(f"偏差超过 {MAX_DEVIATION_CM:.0f} cm 的地方（请核对 pc/field/data/wro_field.yaml）：")
    for d in devs:
        print("  " + d.describe())
    return 1


if __name__ == "__main__":
    sys.exit(main())
