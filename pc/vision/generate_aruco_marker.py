"""
生成要打印的 ArUco marker (4 个场外 anchor + 车顶 marker), 编号和字典来自 pc/config/vision.yaml。

    python -m pc.vision.generate_aruco_marker              # 输出到 aruco_markers/
打印时让黑色方块的边长等于 vision.yaml 里的 size_cm (anchor 8 cm, 车顶 9 cm), 外面留白边。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

from pc.field.model import load_field
from pc.vision.config import VisionConfig
from pc.vision.markers import marker_image

OUT_DIR = Path("aruco_markers")


def main() -> int:
    cfg = VisionConfig.load()
    ids = [*sorted(load_field().anchors), cfg.marker.id]
    OUT_DIR.mkdir(exist_ok=True)
    for mid in ids:
        out = OUT_DIR / f"aruco_marker_{mid}.png"
        cv2.imwrite(str(out), marker_image(mid, 800, cfg.aruco_dictionary))
        size = cfg.marker.size_cm if mid == cfg.marker.id else cfg.anchors.size_cm
        print(f"{out}（黑色方块打印成 {size:g} cm）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
