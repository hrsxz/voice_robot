"""
感知 (改动 vision-field-model): 摄像头采集、内参标定、相机位姿和漂移、场地核对、小车定位、
俯视图和光照、物体检测、身份和跟踪、调试画面、整体集成。

不需要摄像头: 画面由 pc/vision/synthetic.py 按已知的相机参数合成 (含镜头畸变和噪声),
所以可以和"真值"比较误差。真实摄像头上的确认留给实验 (docs/experiments.md)。
"""

import contextlib
import io
import math
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from pc.field.geometry import Pose, heading_diff
from pc.field.layout import load_layout
from pc.field.model import load_field
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.ports import VisionObjectSource, VisionPoseSource
from pc.vision.calibrate import calibrate_images, save_result
from pc.vision.camera_model import CameraModel, Intrinsics, IntrinsicsError
from pc.vision.camera_stream import CameraStream
from pc.vision.config import CameraConfig, MarkerConfig, VisionConfig
from pc.vision.debug_view import draw_debug
from pc.vision.extrinsics import CameraPoseMonitor, anchor_specs, estimate_camera_pose
from pc.vision.field_check import check_field
from pc.vision.localizer import RobotLocalizer
from pc.vision.markers import MarkerDetector, marker_corners
from pc.vision.objects import ObjectDetection, ObjectDetector
from pc.vision.synthetic import (
    Scene,
    SceneObject,
    SceneRobot,
    default_camera,
    field_texture,
    render_board,
    render_scene,
    scene_anchors,
)
from pc.vision.topview import Reference, TopView, check_lighting
from pc.vision.tracker import ObjectTracker, hungarian
from pc.vision.vision_system import VisionSystem
from pc.world.state import WorldState

FIELD = load_field()
GEOM = RobotGeometry.load()
CFG = VisionConfig()  # 默认参数 (和 pc/config/vision.yaml 一致), 不受本机文件影响
MARKER = MarkerConfig(id=20, size_cm=9.0, height_cm=10.0, offset_cm=(-2.0, 1.0))
ANCHORS = anchor_specs(FIELD, CFG.anchors)
TRUE_CAM = default_camera(fm=FIELD)  # 真实相机 (含畸变)
DETECTOR = MarkerDetector()


def scene(objects=(), robot=None, brightness=1.0):
    return Scene(FIELD, scene_anchors(FIELD, CFG.anchors.size_cm), list(objects), robot,
                 brightness)


def robot_at(pose, visible=True):
    return SceneRobot(pose, GEOM, MARKER.id, MARKER.size_cm, MARKER.height_cm,
                      MARKER.offset_cm, MARKER.rotation_deg, visible)


def layout_objects(name="starter2022", skip=()):
    lay = load_layout(name, FIELD)
    return [SceneObject(o, FIELD.objects[o].color, p, FIELD.objects[o].radius_cm,
                        FIELD.objects[o].height_cm)
            for o, p in lay.objects.items() if o not in skip]


def projected_detections(cam, noise=0.0, seed=0, extra=None):
    """直接投影 anchor (以及车顶 marker) 的角点, 不经过画面 (合成投影测试)。"""
    rng = np.random.default_rng(seed)
    out = {}
    for i, a in ANCHORS.items():
        out[i] = cam.project(a.corners()) + rng.normal(0, noise, (4, 2))
    for mid, (corners, h) in (extra or {}).items():
        out[mid] = cam.project(np.c_[corners, np.full(4, h)]) + rng.normal(0, noise, (4, 2))
    return out


def marker_field_corners(pose, marker=MARKER):
    center = pose.to_world(*marker.offset_cm)
    return marker_corners(center, pose.heading + marker.rotation_deg, marker.size_cm)


_cache: dict = {}


def estimated_cam():
    """从合成画面里检测 anchor 求出的相机位姿 (和真值有小误差, 更接近实际)。"""
    if "cam" not in _cache:
        img = render_scene(TRUE_CAM, scene(), noise=2, seed=1)
        est = estimate_camera_pose(DETECTOR.detect(img), ANCHORS, TRUE_CAM.intr)
        assert est.ok, est.message
        _cache["cam"] = est.model
        _cache["empty"] = img
    return _cache["cam"]


# ---------------------------------------------------------------- 1.1 模块和演示


class DemoTest(unittest.TestCase):
    def test_all_vision_modules_import(self):
        import importlib

        for name in ("calibrate", "camera_model", "camera_stream", "config", "debug_view",
                     "extrinsics", "field_check", "generate_aruco_marker", "localizer",
                     "markers", "objects", "synthetic", "topview", "tracker", "vision_demo",
                     "vision_system"):
            with self.subTest(module=name):
                importlib.import_module(f"pc.vision.{name}")

    def test_vision_demo_on_static_image(self):
        from pc.vision import vision_demo

        img = render_scene(TRUE_CAM, scene(layout_objects(), robot_at(Pose(60, 80, 30))),
                           noise=2, seed=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            cv2.imwrite(str(path), img)
            intr = Path(tmp) / "intr.yaml"
            TRUE_CAM.intr.save(intr)
            out = Path(tmp) / "debug.png"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = vision_demo.main(["--image", str(path), "--intrinsics", str(intr),
                                         "--reference", str(Path(tmp) / "none.png"),
                                         "--save", str(out)])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertTrue(out.exists())
            self.assertIn("相机位姿已计算", buf.getvalue())
            self.assertIn("小车 (", buf.getvalue())


# ---------------------------------------------------------------- 1.2 摄像头


class FakeCapture:
    def __init__(self, *args, unsupported=(), size=(1920, 1080), opened=True):
        self.args = args
        self.props: dict[int, float] = {}
        self.unsupported = set(unsupported)
        self.size = size
        self.opened = opened

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        if prop in self.unsupported:
            return False
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            value = min(value, self.size[0])
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            value = min(value, self.size[1])
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, -1)

    def read(self):
        return True, np.zeros((self.size[1], self.size[0], 3), np.uint8)

    def release(self):
        self.opened = False


class CameraStreamTest(unittest.TestCase):
    def make(self, **kw):
        made = []

        def factory(*args):
            cap = FakeCapture(*args, **kw)
            made.append(cap)
            return cap
        return CameraStream.from_config(CameraConfig(), capture_factory=factory), made

    def test_all_settings_accepted(self):
        cam, made = self.make()
        self.assertEqual(cam.open(), [])
        cap = made[0]
        self.assertEqual(cap.props[cv2.CAP_PROP_FRAME_WIDTH], 1920)
        self.assertEqual(cap.props[cv2.CAP_PROP_FOURCC], cv2.VideoWriter_fourcc(*"MJPG"))
        self.assertEqual(cap.props[cv2.CAP_PROP_AUTOFOCUS], 0)
        self.assertEqual(cap.props[cv2.CAP_PROP_AUTO_WB], 0)
        import sys
        if sys.platform == "win32":
            self.assertEqual(cap.args, (0, cv2.CAP_DSHOW))
        self.assertEqual(cam.read().shape, (1080, 1920, 3))

    def test_autofocus_not_supported_warns(self):
        cam, _ = self.make(unsupported={cv2.CAP_PROP_AUTOFOCUS})
        warnings = cam.open()
        self.assertIn("无法锁定对焦，标定可能失效", warnings)

    def test_resolution_and_other_failures_warn(self):
        cam, _ = self.make(size=(1280, 720),
                           unsupported={cv2.CAP_PROP_AUTO_EXPOSURE, cv2.CAP_PROP_AUTO_WB})
        warnings = cam.open()
        self.assertTrue(any("实际 1280×720" in w for w in warnings), warnings)
        self.assertTrue(any("自动曝光" in w for w in warnings), warnings)
        self.assertTrue(any("自动白平衡" in w for w in warnings), warnings)

    def test_cannot_open(self):
        cam, _ = self.make(opened=False)
        with self.assertRaises(RuntimeError):
            cam.open()


# ---------------------------------------------------------------- 2.1 内参标定


def board_poses(n=20, seed=0):
    rng = np.random.default_rng(seed)
    poses = []
    for _ in range(n):
        rvec = np.array([rng.uniform(-0.5, 0.5), rng.uniform(-0.5, 0.5), rng.uniform(-0.3, 0.3)])
        R, _ = cv2.Rodrigues(rvec)
        center = R @ np.array([100.0, 62.5, 0.0])  # 棋盘中心 (毫米)
        # 标定照片要覆盖画面四角 (README 的标定步骤也这样要求)
        target = np.array([rng.uniform(-260, 260), rng.uniform(-150, 150),
                           rng.uniform(450, 700)])
        poses.append((rvec, target - center))
    return poses


class CalibrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.true = Intrinsics.from_fov(1280, 720, 70.0, dist=[-0.15, 0.06, 0.0, 0.0, 0.0])
        cls.images = [render_board(cls.true, r, t) for r, t in board_poses()]

    def test_calibrates_synthetic_boards(self):
        r = calibrate_images(self.images, (9, 6), 25.0)
        self.assertTrue(r.ok, r.message)
        self.assertLess(r.rms, 0.5)
        self.assertGreaterEqual(r.used, 14)
        K, Kt = r.intrinsics.K, self.true.K
        self.assertAlmostEqual(K[0, 0] / Kt[0, 0], 1.0, delta=0.01)
        self.assertAlmostEqual(K[0, 2], Kt[0, 2], delta=6)
        # 畸变: 画面边缘的点去畸变后和真值相差不到 1 像素
        edge = np.array([[5.0, 5.0], [1270.0, 360.0], [640.0, 715.0]])
        a = cv2.undistortPoints(edge.reshape(-1, 1, 2), K, r.intrinsics.dist, P=Kt)
        b = cv2.undistortPoints(edge.reshape(-1, 1, 2), Kt, self.true.dist, P=Kt)
        self.assertLess(float(np.abs(a - b).max()), 1.0)
        self.assertIn("重投影误差", r.message)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_result(r, Path(tmp) / "intr.yaml")
            again = Intrinsics.load(path, expected_size=(1280, 720))
            np.testing.assert_allclose(again.K, K, rtol=1e-6)
            # 分辨率不一致: 拒绝使用
            with self.assertRaises(IntrinsicsError) as cm:
                Intrinsics.load(path, expected_size=(1920, 1080))
            self.assertIn("请在当前分辨率下重新标定", str(cm.exception))

    def test_large_error_not_saved(self):
        bad = Intrinsics(self.true.K, self.true.dist, 1280, 720, rms=1.8)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IntrinsicsError) as cm:
                bad.save(Path(tmp) / "x.yaml")
            self.assertIn("请重新拍摄标定照片", str(cm.exception))
            self.assertFalse((Path(tmp) / "x.yaml").exists())

    def test_too_few_images(self):
        r = calibrate_images([*self.images[:5], np.full((720, 1280), 128, np.uint8)])
        self.assertFalse(r.ok)
        self.assertIn("至少要", r.message)
        self.assertIn(5, r.rejected)  # 灰色图片里没有棋盘格


# ---------------------------------------------------------------- 2.2–2.3 相机位姿


class ExtrinsicsTest(unittest.TestCase):
    def test_pose_from_projected_anchors(self):
        est = estimate_camera_pose(projected_detections(TRUE_CAM, 0.3), ANCHORS, TRUE_CAM.intr)
        self.assertTrue(est.ok, est.message)
        err = np.linalg.norm(np.subtract(est.model.center, TRUE_CAM.center))
        self.assertLess(err, 1.5)
        self.assertLess(est.error_cm, 0.3)
        self.assertIn("anchor 平均误差", est.message)

    def test_pose_from_rendered_image(self):
        cam = estimated_cam()
        self.assertLess(np.linalg.norm(np.subtract(cam.center, TRUE_CAM.center)), 2.0)
        # 场地上的点: 估计的相机和真实相机投影差不到 1 像素
        pts = [(0, 0), (236, 0), (236, 114), (0, 114), (118, 57)]
        self.assertLess(float(np.abs(cam.project(pts) - TRUE_CAM.project(pts)).max()), 1.0)

    def test_missing_anchor(self):
        det = projected_detections(TRUE_CAM)
        del det[13]
        est = estimate_camera_pose(det, ANCHORS, TRUE_CAM.intr)
        self.assertFalse(est.ok)
        self.assertEqual(est.missing, [13])
        self.assertIn("缺少 anchor 13", est.message)
        self.assertIsNone(est.model)

    def test_drift_triggers_recalculation(self):
        mon = CameraPoseMonitor(ANCHORS, TRUE_CAM.intr, recheck_s=5.0, drift_cm=1.0)
        self.assertTrue(mon.start(projected_detections(TRUE_CAM, 0.2), now=0.0).ok)
        # 三脚架被碰: 相机转了 1°
        bumped = CameraModel(TRUE_CAM.intr, TRUE_CAM.rvec + np.array([[0.0], [0.0175], [0.0]]),
                             TRUE_CAM.tvec)
        moved = projected_detections(bumped, 0.2, seed=1)
        self.assertFalse(mon.maybe_check(moved, now=2.0).checked)  # 还没到 5 秒
        check = mon.maybe_check(moved, now=5.0)
        self.assertTrue(check.checked)
        self.assertGreater(check.max_cm, 1.0)
        self.assertTrue(check.recalibrated)
        self.assertIn("相机位置发生了变化", check.warning)
        self.assertLess(np.linalg.norm(np.subtract(mon.model.center, bumped.center)), 1.5)
        # 没有再动: 不再重新计算
        again = mon.maybe_check(projected_detections(bumped, 0.2, seed=2), now=10.0)
        self.assertTrue(again.checked)
        self.assertFalse(again.recalibrated)
        self.assertLessEqual(again.max_cm, 1.0)


# ---------------------------------------------------------------- 3.3 场地核对


class FieldCheckTest(unittest.TestCase):
    def test_matching_reference(self):
        top = field_texture(FIELD, 4.0, 0.0)
        self.assertEqual(check_field(top, FIELD, 4.0), [])

    def test_shifted_elements_reported(self):
        top = field_texture(FIELD, 4.0, 0.0, shifts={"zone:B": (4.0, 0.0),
                                                       "circle:red": (0.0, 3.0)})
        devs = {(d.element, d.what): d for d in check_field(top, FIELD, 4.0)}
        self.assertIn(("zone:B", "左边"), devs)
        self.assertIn(("zone:B", "右边"), devs)
        self.assertAlmostEqual(devs[("zone:B", "左边")].deviation_cm, 4.0, delta=0.5)
        self.assertAlmostEqual(devs[("circle:red", "圆心")].deviation_cm, 3.0, delta=0.5)
        self.assertNotIn(("zone:A", "右边"), devs)
        self.assertIn("相差", devs[("zone:B", "左边")].describe())

    def test_rendered_reference_matches(self):
        cam = estimated_cam()
        view = TopView(cam, FIELD, 4.0)
        top = view.rectify(_cache["empty"])
        self.assertEqual(check_field(top, FIELD, 4.0), [])


# ---------------------------------------------------------------- 4.1–4.2 定位


class LocalizerTest(unittest.TestCase):
    POSES = (Pose(118, 57, 0), Pose(10, 8, 45), Pose(226, 8, 135), Pose(10, 105, 270),
             Pose(226, 105, 300), Pose(60, 30, 200), Pose(180, 90, 90))

    def test_projection_accuracy_everywhere(self):
        cam = estimate_camera_pose(projected_detections(TRUE_CAM, 0.3), ANCHORS,
                                   TRUE_CAM.intr).model
        loc = RobotLocalizer(cam, MARKER, GEOM)
        for i, pose in enumerate(self.POSES):
            with self.subTest(pose=pose):
                det = projected_detections(TRUE_CAM, 0.3, seed=10 + i,
                                           extra={20: (marker_field_corners(pose), 10.0)})
                r = loc.update(det, timestamp=float(i))
                self.assertTrue(r.found)
                self.assertLessEqual(math.hypot(r.x - pose.x, r.y - pose.y), 2.0)
                self.assertLessEqual(abs(heading_diff(r.heading, pose.heading)), 3.0)

    def test_far_corner_needs_height_correction(self):
        # 不修正 marker 高度 (当作在地面上) 时远角误差很大; 修正后满足精度
        cam = TRUE_CAM
        pose = Pose(10, 8, 0)
        det = {20: cam.project(np.c_[marker_field_corners(pose), np.full(4, 10.0)])}
        good = RobotLocalizer(cam, MARKER, GEOM).update(det, 0.0)
        flat = MarkerConfig(20, 9.0, 0.0, MARKER.offset_cm)
        bad = RobotLocalizer(cam, flat, GEOM).locate(det[20])
        self.assertLessEqual(math.hypot(good.x - pose.x, good.y - pose.y), 0.5)
        # 当作在地面上: 尺寸对不上 (被当成误检) 或者位置偏很多
        if bad is not None:
            self.assertGreater(math.hypot(bad[0].x - pose.x, bad[0].y - pose.y), 5.0)

    def test_turning_in_place(self):
        loc = RobotLocalizer(TRUE_CAM, MARKER, GEOM)
        centers, cages = [], []
        for k, h in enumerate(range(0, 360, 30)):
            pose = Pose(150, 60, h)
            det = projected_detections(TRUE_CAM, 0.3, seed=k,
                                       extra={20: (marker_field_corners(pose), 10.0)})
            r = loc.update(det, float(k))
            centers.append((r.x, r.y))
            cages.append(r.cage_center)
            expected = pose.to_world(GEOM.cage_offset, 0)
            self.assertLessEqual(math.dist(r.cage_center, expected), 2.0)
        c = np.array(centers)
        self.assertLessEqual(float(np.linalg.norm(c - c.mean(axis=0), axis=1).max()), 1.0)

    def test_rendered_images(self):
        cam = estimated_cam()
        loc = RobotLocalizer(cam, MARKER, GEOM)
        for i, pose in enumerate([Pose(60, 80, 30), Pose(222, 10, 160), Pose(15, 12, 300)]):
            with self.subTest(pose=pose):
                img = render_scene(TRUE_CAM, scene(robot=robot_at(pose)), noise=2, seed=i)
                r = loc.update(DETECTOR.detect(img), float(i))
                self.assertTrue(r.found, r.reason)
                self.assertLessEqual(math.hypot(r.x - pose.x, r.y - pose.y), 2.0)
                self.assertLessEqual(abs(heading_diff(r.heading, pose.heading)), 3.0)

    def test_not_visible(self):
        loc = RobotLocalizer(TRUE_CAM, MARKER, GEOM)
        pose = Pose(100, 50, 0)
        loc.update({20: TRUE_CAM.project(np.c_[marker_field_corners(pose), np.full(4, 10.0)])},
                   timestamp=10.0)
        r = loc.update({}, timestamp=12.0)
        self.assertFalse(r.found)
        self.assertIsNone(r.pose)
        self.assertIsNone(r.x)
        self.assertAlmostEqual(r.last_pose.x, 100, delta=0.5)
        self.assertAlmostEqual(r.age_s, 2.0)
        self.assertEqual(r.reason, "看不到车顶 marker")
        self.assertIs(loc.latest(), r)


# ---------------------------------------------------------------- 5.x 俯视图、检测、跟踪


class DetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cam = estimated_cam()
        cls.view = TopView(cls.cam, FIELD, CFG.px_per_cm)
        cls.ref = cls.view.rectify(_cache["empty"])
        cls.det = ObjectDetector(cls.view, cls.ref, CFG.detection, GEOM, MARKER.height_cm)

    def top(self, objects=(), robot=None, seed=5, brightness=1.0):
        img = render_scene(TRUE_CAM, scene(objects, robot, brightness), noise=2, seed=seed)
        return self.view.rectify(img)

    def test_lighting_warning(self):
        dark = check_lighting(self.ref, self.top(brightness=0.5), max_change=0.25)
        self.assertTrue(dark.warn)
        self.assertIn("光照和参考图差别过大", dark.message)
        ok = check_lighting(self.ref, self.top(brightness=0.95), max_change=0.25)
        self.assertFalse(ok.warn)

    def test_reference_roundtrip(self):
        ref = Reference.capture(self.ref, self.view)
        with tempfile.TemporaryDirectory() as tmp:
            path = ref.save(Path(tmp) / "ref.png")
            again = Reference.load(path)
            self.assertIsNone(again.check_compatible(self.view))
            moved = CameraModel.look_at(TRUE_CAM.intr, (118, 230, 175), (118, 69, 0))
            self.assertIn("相机位置", again.check_compatible(TopView(moved, FIELD, 4.0)))

    def test_empty_field_detects_nothing(self):
        self.assertEqual(self.det.detect(self.top(seed=9)), [])

    def test_red_robot_on_red_circle(self):
        c = FIELD.circles["red"].center
        ds = self.det.detect(self.top([SceneObject("red_robot", "red", c, 3.0, 5.0)]))
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].color, "red")
        self.assertLessEqual(math.dist(ds[0].position(5.0), c), 2.0)

    def test_full_layout_accuracy_and_robot_excluded(self):
        objs = layout_objects("starter2022")
        robot = Pose(60, 80, 30)
        ds = self.det.detect(self.top(objs, robot_at(robot)), robot)
        self.assertEqual(len(ds), len(objs))
        for d in ds:
            o = min(objs, key=lambda o: math.dist(o.xy, d.ground))
            with self.subTest(obj=o.id):
                self.assertEqual(d.color, o.color)
                self.assertLessEqual(math.dist(d.position(o.height), o.xy), 2.0)


class TrackerTest(unittest.TestCase):
    STARTS = load_layout("starter2022", FIELD).objects

    def dets(self, positions):
        return [ObjectDetection(FIELD.objects[o].color, p, 30.0) for o, p in positions.items()]

    def test_hungarian(self):
        cost = np.array([[4, 1, 3], [2, 0, 5], [3, 2, 2]], float)
        self.assertEqual(hungarian(cost), [(0, 1), (1, 0), (2, 2)])
        self.assertEqual(hungarian(np.array([[1e9, 1.0]])), [(0, 1)])
        self.assertEqual(hungarian(np.zeros((0, 0))), [])

    def test_identity_full_layout(self):
        t = ObjectTracker(FIELD, self.STARTS)
        shuffled = dict(reversed(list(self.STARTS.items())))
        r = t.initialize(self.dets({o: (p[0] + 1, p[1] - 1) for o, p in shuffled.items()}), 0.0)
        self.assertEqual(set(r.assigned), set(self.STARTS))
        self.assertEqual(r.missing, [])
        self.assertEqual(r.extra, [])
        self.assertIn("认出 10 个物体", r.message)

    def test_missing_package(self):
        t = ObjectTracker(FIELD, self.STARTS)
        pos = {o: p for o, p in self.STARTS.items() if o != "package_2"}
        stray = ObjectDetection("green", (150.0, 60.0), 30.0)
        r = t.initialize([*self.dets(pos), stray], 0.0)
        self.assertEqual(r.missing, ["package_2"])
        self.assertEqual(r.extra, [stray])
        self.assertIn("缺少 package_2", r.message)

    def test_tracking_move_occlude_capture(self):
        t = ObjectTracker(FIELD, self.STARTS)
        t.initialize(self.dets(self.STARTS), 0.0)
        pos = dict(self.STARTS)
        # 用手把黄色小人移动 20 cm (分几帧)
        x, y = pos["yellow_person"]
        for k in range(1, 5):
            pos["yellow_person"] = (x + 5 * k, y + 0.0)
            t.update(self.dets(pos), float(k))
        yp = t.objects["yellow_person"]
        self.assertTrue(yp.visible)
        self.assertAlmostEqual(yp.x, x + 20, delta=0.01)
        # 两个包裹 (同色) 交换不了身份: 各自跟着最近的
        self.assertEqual(t.objects["package_1"].xy, self.STARTS["package_1"])
        # 被挡住: 保留最后的位置, 标为被遮挡
        hidden = {o: p for o, p in pos.items() if o != "red_robot"}
        t.update(self.dets(hidden), 5.0)
        rr = t.objects["red_robot"]
        self.assertFalse(rr.visible)
        self.assertEqual(rr.xy, self.STARTS["red_robot"])
        self.assertEqual(rr.timestamp, 4.0)
        # 被夹住: 跟着笼区中心走, 即使看不见
        no_yellow = {o: p for o, p in pos.items() if o != "yellow_person"}
        t.update(self.dets(no_yellow), 6.0, captured="yellow_person", cage_center=(100.0, 50.0))
        t.update(self.dets(no_yellow), 7.0, captured="yellow_person", cage_center=(130.0, 50.0))
        yp = t.objects["yellow_person"]
        self.assertEqual(yp.xy, (130.0, 50.0))
        self.assertTrue(yp.captured)


# ---------------------------------------------------------------- 6.1 调试画面 + 集成


class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_vision_system_end_to_end(self):
        t = [0.0]
        vs = VisionSystem(FIELD, TRUE_CAM.intr, VisionConfig(marker=MARKER), GEOM,
                          starts=load_layout("starter2022", FIELD).objects, clock=lambda: t[0])
        objs = layout_objects("starter2022")
        est = vs.start(render_scene(TRUE_CAM, scene(), noise=2, seed=11))
        self.assertTrue(est.ok, est.message)
        vs.set_reference(render_scene(TRUE_CAM, scene(), noise=2, seed=12))
        pose = Pose(60, 62, 0)  # 空地, 往前 30 cm 也碰不到别的物体
        init = vs.begin_tracking(render_scene(TRUE_CAM, scene(objs, robot_at(pose)), noise=2,
                                              seed=13))
        self.assertEqual(init.missing, [], init.message)
        self.assertEqual(len(init.assigned), 10)

        # 夹住黄色小人, 小车前进 30 cm: 黄色小人跟着笼区中心走
        world = WorldState(FIELD, lambda: t[0])
        poses = VisionPoseSource(vs.localizer, clock=lambda: t[0])
        objects = VisionObjectSource(vs.tracker)
        moved = pose.moved(30)
        rest = [o for o in objs if o.id != "yellow_person"]
        t[0] = 1.0
        out = vs.process(render_scene(TRUE_CAM, scene(rest, robot_at(moved)), noise=2, seed=14),
                         holding="yellow_person")
        self.assertTrue(out.robot.found)
        snap = await world.refresh(poses, objects)
        self.assertLessEqual(math.dist(snap.robot.xy, moved.xy), 2.0)
        yp = vs.tracker.objects["yellow_person"]
        self.assertLessEqual(math.dist(yp.xy, GEOM.cage_center(moved)), 2.0)
        # 调试画面
        img = draw_debug(_cache.get("empty", np.zeros((1080, 1920, 3), np.uint8)),
                         vs.monitor.model, FIELD, out.robot, vs.tracker.latest_objects(),
                         MARKER.height_cm, out.markers)
        self.assertEqual(img.shape, (1080, 1920, 3))
        # 看不见 marker: 不可见, 不输出旧位姿
        t[0] = 2.0
        out = vs.process(render_scene(TRUE_CAM, scene(rest, robot_at(moved, visible=False)),
                                      noise=2, seed=15))
        self.assertFalse(out.robot.found)
        reading = await poses.read_pose()
        self.assertFalse(reading.visible)
        self.assertIsNotNone(reading.last_pose)

    def test_debug_view_not_visible_text(self):
        from pc.vision.localizer import LocalizerReading

        frame = np.zeros((1080, 1920, 3), np.uint8)
        img = draw_debug(frame, TRUE_CAM, FIELD, LocalizerReading(False, 1.0, age_s=2.0))
        self.assertGreater(int(img.sum()), 0)
        self.assertFalse(np.array_equal(img, frame))


if __name__ == "__main__":
    unittest.main()
