"""
场地模型、布局和接近方向 (vision-field-model 任务 3.1、3.2; navigation-grasp 任务 3.1)。
"""

import copy
import unittest

from pc.field.approach import choose_approach
from pc.field.geometry import Shape, heading_diff
from pc.field.layout import load_layout, parse_layout, random_layout
from pc.field.model import DEFAULT_FIELD, FieldModelError, load_field, parse_field, read_yaml
from pc.field.robot_geometry import RobotGeometry


def raw_field() -> dict:
    return copy.deepcopy(read_yaml(DEFAULT_FIELD))


class FieldDataTest(unittest.TestCase):
    """填好的练习场地数据 (任务 3.2)。"""

    @classmethod
    def setUpClass(cls):
        cls.field = load_field()

    def test_zone_b_is_green(self):
        b = self.field.zone("B")
        self.assertEqual(b.color, "green")
        self.assertGreaterEqual(len(b.polygon), 4)

    def test_red_circle(self):
        c = self.field.circle("red")
        self.assertAlmostEqual(c.radius, 5.0)
        self.assertTrue(self.field.inside(c.center))

    def test_blue_person_default_target_is_b(self):
        o = self.field.objects["blue_person"]
        self.assertEqual(o.type, "large_person")
        self.assertEqual(o.target, "zone:B")

    def test_red_robot_default_target_is_red_circle(self):
        o = self.field.objects["red_robot"]
        self.assertEqual(o.type, "service_robot")
        self.assertEqual(o.target, "circle:red")

    def test_every_object_target_exists_and_has_entries(self):
        for o in self.field.objects.values():
            with self.subTest(object=o.id):
                self.assertTrue(self.field.target(o.target).entries)

    def test_road_network_connected(self):
        self.assertTrue(self.field.road_connected())
        names = set(self.field.road_nodes)
        self.assertTrue({"v1_bottom", "v2_top", "h_c"} <= names)

    def test_keepouts_default_off(self):
        self.assertEqual(len(self.field.keepouts), 5)
        self.assertFalse(any(k.enabled for k in self.field.keepouts.values()))
        f2 = self.field.with_keepouts({"barrier": True})
        self.assertEqual([k.id for k in f2.enabled_keepouts()], ["barrier"])

    def test_named_positions(self):
        for pid in ["pos1", "pos2", "pos3", "pos4", "pos5_side",
                    "d1", "d2", "d3", "d4", "d5", "d6"]:
            self.assertIn(pid, self.field.positions)

    def test_calibrator_reads_anchors_from_field_file(self):
        try:
            from pc.vision.calibration import MapCalibrator
        except ImportError as exc:  # opencv 不可用时跳过
            self.skipTest(str(exc))
        anchors = MapCalibrator._load_anchors(DEFAULT_FIELD)
        self.assertEqual(sorted(anchors), [10, 11, 12, 13])


class FieldValidationTest(unittest.TestCase):
    """加载校验 (任务 3.1): 指出具体错误并拒绝启动。"""

    def assert_error(self, data, text):
        with self.assertRaises(FieldModelError) as ctx:
            parse_field(data, "test")
        self.assertTrue(any(text in e for e in ctx.exception.errors), ctx.exception.errors)

    def test_valid(self):
        parse_field(raw_field(), "test")

    def test_duplicate_id(self):
        text = DEFAULT_FIELD.read_text(encoding="utf-8").replace(
            "  pos2: {x: 5.5, y: 48.4", "  pos1: {x: 5.5, y: 48.4")
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.yaml"
            p.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(FieldModelError, "重复的编号：pos1"):
                load_field(p)

    def test_object_target_must_exist(self):
        d = raw_field()
        d["objects"]["red_robot"]["target"] = "circle:purple"
        self.assert_error(d, "物体 red_robot 的目标不存在")

    def test_out_of_range(self):
        d = raw_field()
        d["positions"]["d1"]["x"] = 300
        self.assert_error(d, "位置点 d1 超出场地范围")

    def test_anchor_may_be_outside(self):
        d = raw_field()
        d["anchors"][10] = {"x_cm": -12, "y_cm": -8}
        parse_field(d, "test")

    def test_edge_references_missing_node(self):
        d = raw_field()
        d["roads"]["edges"].append(["a_top", "nowhere"])
        self.assert_error(d, "引用了不存在的路线点 nowhere")

    def test_keepout_references_missing_position(self):
        d = raw_field()
        d["keepouts"]["tree_pos1"]["circle"]["at"] = "pos_9"
        self.assert_error(d, "禁行区 tree_pos1 引用了不存在的位置 pos_9")

    def test_target_without_entry(self):
        d = raw_field()
        d["zones"]["C"]["entries"] = []
        self.assert_error(d, "目标 zone:C 缺少进入点")

    def test_road_not_connected(self):
        d = raw_field()
        d["roads"]["nodes"]["island"] = [100, 50]
        with self.assertRaisesRegex(FieldModelError, "道路网不连通"):
            parse_field(d, "test")

    def test_infeasible_approach_direction(self):
        # 2 号位从墙那一侧 (朝东) 接近: 车身会越出场地
        d = raw_field()
        d["positions"]["pos2"]["approach"] = 0
        self.assert_error(d, "位置点 pos2 的接近方向 0° 不可行")

    def test_unknown_zone_color(self):
        d = raw_field()
        d["zones"]["A"]["color"] = "purple"
        self.assert_error(d, "区域 A 的颜色不对")


class LayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.field = load_field()

    def test_starter2022(self):
        layout = load_layout("starter2022", self.field)
        self.assertEqual(len(layout.objects), 10)
        self.assertEqual(layout.positions["package_1"], "pos2")
        self.assertEqual(layout.objects["blue_person"], self.field.positions["c_inner"].xy)
        self.assertTrue(self.field.target("zone:C").contains(layout.objects["blue_person"]))
        self.assertEqual(len([o for o in layout.obstacles.values() if o.type == "tree"]), 4)

    def test_free_placement(self):
        layout = parse_layout({"objects": {"yellow_person": [150, 60]}}, self.field)
        self.assertEqual(layout.objects["yellow_person"], (150.0, 60.0))
        self.assertIsNone(layout.positions["yellow_person"])

    def test_missing_position(self):
        with self.assertRaises(FieldModelError) as ctx:
            parse_layout({"objects": {"package_1": "pos_9"}}, self.field, "x")
        self.assertIn("物体 package_1 引用了不存在的位置 pos_9", ctx.exception.errors)

    def test_unknown_object_and_outside(self):
        with self.assertRaises(FieldModelError) as ctx:
            parse_layout({"objects": {"ufo": "pos1", "red_robot": [500, 10]}}, self.field)
        errs = " ".join(ctx.exception.errors)
        self.assertIn("ufo 不在物体目录里", errs)
        self.assertIn("red_robot 在场地外", errs)

    def test_overlap(self):
        with self.assertRaisesRegex(FieldModelError, "重叠"):
            parse_layout({"objects": {"red_robot": [100, 60], "blue_robot": [102, 60]}},
                         self.field)

    def test_random_layout_reproducible_and_valid(self):
        a = random_layout(self.field, seed=7)
        b = random_layout(self.field, seed=7)
        c = random_layout(self.field, seed=8)
        self.assertEqual(a.objects, b.objects)
        self.assertNotEqual(a.objects, c.objects)
        self.assertEqual(set(a.objects), set(self.field.objects))
        geom = RobotGeometry.load()
        for oid, p in a.objects.items():
            r = self.field.objects[oid].radius_cm
            self.assertFalse(any(t.touches(p, r) for t in self.field.targets.values()))
            others = [Shape(o, center=q, radius=self.field.objects[o].radius_cm)
                      for o, q in a.objects.items() if o != oid]
            others += a.obstacle_shapes()
            self.assertIsNotNone(choose_approach(p, r, geom, self.field.width,
                                                 self.field.height, others), oid)


class ApproachTest(unittest.TestCase):
    """接近方向 (navigation-grasp 任务 3.1)。"""

    @classmethod
    def setUpClass(cls):
        cls.field = load_field()
        cls.geom = RobotGeometry.load()

    def plan(self, p, radius=3.0, heading=None, obstacles=()):
        return choose_approach(p, radius, self.geom, self.field.width, self.field.height,
                               obstacles, heading)

    def test_package_at_wall_approached_from_inside(self):
        pos = self.field.positions["pos2"]
        self.assertEqual(pos.approach, 180)
        plan = self.plan(pos.xy, heading=pos.approach)
        self.assertIsNotNone(plan)
        # 小车在包裹的场地内侧 (右边), 车头朝墙
        self.assertGreater(plan.grasp.x, pos.x)
        self.assertGreater(plan.pregrasp.x, plan.grasp.x)
        # 物体在笼区里
        local = plan.grasp.to_local(pos.xy)
        self.assertLessEqual(abs(local[1]), self.geom.cage_tolerance(3.0))
        x0, x1, _, _ = self.geom.cage_inner_rect
        self.assertTrue(x0 + 3.0 <= local[0] <= x1 - 3.0)

    def test_free_position_gets_automatic_direction(self):
        plan = self.plan((150, 60))
        self.assertIsNotNone(plan)
        self.assertLessEqual(abs(plan.grasp.to_local((150, 60))[1]), 0.01)

    def test_auto_direction_avoids_wall(self):
        # 靠近上边墙的物体: 只能从下方 (朝上) 接近
        plan = self.plan((120, 6))
        self.assertIsNotNone(plan)
        self.assertLessEqual(abs(heading_diff(270, plan.heading)), 45)

    def test_corner_is_unreachable(self):
        self.assertIsNone(self.plan((3, 3)))

    def test_surrounded_object_is_unreachable(self):
        import math
        ring = [Shape(f"o{i}", center=(150 + 10 * math.cos(math.radians(a)),
                                       60 + 10 * math.sin(math.radians(a))), radius=3)
                for i, a in enumerate(range(0, 360, 30))]
        self.assertIsNone(self.plan((150, 60), obstacles=ring))


if __name__ == "__main__":
    unittest.main()
