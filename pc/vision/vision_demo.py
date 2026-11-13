"""
感知演示 / 调试画面 (改动 vision-field-model 任务 1.1、6.1)。

    python -m pc.vision.vision_demo --image 照片.png --save logs/debug.png   # 用一张静态图片
    python -m pc.vision.vision_demo --camera --show                         # 摄像头实时调试画面
    python -m pc.vision.vision_demo --camera --capture-reference             # 场地清空后拍参考图

没有标定文件时可以用 --fov 70 近似 (按水平视场角, 不修正畸变, 精度会差)。
有参考图 (pc/config/local/reference_topview.png) 时同时检测物体, 并按 --layout 的起始布局确定身份。
调试画面: 白线 = 场地边界, 彩色线 = 区域和圆圈, 紫框 = anchor, 绿箭头 = 小车朝向,
黄十字 = 笼区中心, 物体旁边是编号 (橙色 = 被遮挡)。窗口里按 q 退出。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

from pc.field.layout import load_layout
from pc.field.model import load_field
from pc.vision.camera_model import Intrinsics, IntrinsicsError
from pc.vision.config import VisionConfig
from pc.vision.debug_view import draw_debug
from pc.vision.vision_system import VisionSystem


def load_intrinsics(args, cfg: VisionConfig, size: tuple[int, int]) -> Intrinsics:
    if args.fov:
        return Intrinsics.from_fov(size[0], size[1], args.fov)
    path = args.intrinsics or cfg.intrinsics_path
    return Intrinsics.load(path, expected_size=size)


def run(args) -> int:
    cfg = VisionConfig.load(args.config) if args.config else VisionConfig.load()
    fm = load_field()
    starts = load_layout(args.layout, fm).objects if args.layout else {}
    cam = None
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"读不到图片 {args.image}")
            return 2
    else:
        from pc.vision.camera_stream import CameraStream

        cam = CameraStream.from_config(cfg.camera)
        for w in cam.open():
            print(f"警告：{w}")
        frame = cam.read()
    size = (frame.shape[1], frame.shape[0])
    try:
        intr = load_intrinsics(args, cfg, size)
    except IntrinsicsError as exc:
        print(f"内参不能用：{exc}")
        return 2
    vs = VisionSystem(fm, intr, cfg, starts=starts)
    est = vs.start(frame)
    print(est.message)
    if not est.ok:
        if args.save:
            Path(args.save).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(args.save, frame)
        return 1

    if args.capture_reference:
        ref = vs.set_reference(frame)
        out = ref.save(args.reference or cfg.reference_path)
        print(f"参考图已保存 {out}（亮度 {ref.brightness:.0f}）")
        return 0
    ref_path = Path(args.reference or cfg.reference_path)
    if ref_path.exists():
        problem = vs.load_reference(ref_path)
        print(f"参考图不能用：{problem}" if problem else f"参考图：{ref_path}")
    if vs.detector is not None and starts:
        init = vs.begin_tracking(frame)
        print(init.message)

    try:
        while True:
            out = vs.process(frame)
            for w in out.warnings:
                print(f"警告：{w}")
            r = out.robot
            if r is not None and r.found:
                print(f"小车 ({r.x:.1f}, {r.y:.1f}) 朝向 {r.heading:.1f}°")
            elif r is not None:
                print(f"小车：{r.reason}")
            if vs.detector is not None and not vs.tracker.objects:
                for d in out.detections:
                    print(f"物体 {d.color} ({d.ground[0]:.1f}, {d.ground[1]:.1f}) "
                          f"{d.area_cm2:.0f} cm²")
            img = draw_debug(frame, vs.monitor.model, fm, r, vs.tracker.latest_objects(),
                             cfg.marker.height_cm, out.markers)
            if args.save:
                Path(args.save).parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(args.save, img)
                print(f"调试画面：{args.save}")
            if not args.show:
                return 0
            cv2.imshow("vision debug", cv2.resize(img, None, fx=args.scale, fy=args.scale))
            if (cv2.waitKey(1 if cam else 0) & 0xFF) == ord("q") or cam is None:
                return 0
            frame = cam.read()
            time.sleep(0)
    finally:
        if cam is not None:
            cam.release()
        if args.show:
            cv2.destroyAllWindows()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pc.vision.vision_demo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="静态图片（代替摄像头）")
    src.add_argument("--camera", action="store_true", help="使用摄像头（pc/config/vision.yaml）")
    p.add_argument("--intrinsics", help="内参文件（默认 pc/config/local/camera_intrinsics.yaml）")
    p.add_argument("--fov", type=float, help="没有标定时按水平视场角近似（度）")
    p.add_argument("--reference", help="参考图（默认 pc/config/local/reference_topview.png）")
    p.add_argument("--capture-reference", action="store_true", help="把当前画面存为空场地参考图")
    p.add_argument("--layout", default="starter2022",
                   help="起始布局（确定物体身份用），空字符串 = 不用")
    p.add_argument("--config", help="感知参数文件（默认 pc/config/vision.yaml）")
    p.add_argument("--save", help="保存调试画面")
    p.add_argument("--show", action="store_true", help="显示调试画面窗口")
    p.add_argument("--scale", type=float, default=0.6, help="窗口缩放")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
