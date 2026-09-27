from pathlib import Path

import cv2

OUT_DIR = Path("aruco_markers")
OUT_DIR.mkdir(exist_ok=True)

dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

marker_ids = [10, 11, 12, 13, 20, 21]
marker_px = 800

for marker_id in marker_ids:
    marker = cv2.aruco.generateImageMarker(dictionary, marker_id, marker_px)
    out_path = OUT_DIR / f"aruco_marker_{marker_id}.png"
    cv2.imwrite(str(out_path), marker)
    print(out_path)
