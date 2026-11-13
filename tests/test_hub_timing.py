import contextlib
import io
import unittest

from pc.agent.robot_agent import RobotAgent
from pc.spike_communication import motion_timing as mt
from pc.spike_communication.spikehub import HubTimeoutError, SpikeHub


class EstimateTest(unittest.TestCase):
    def test_estimates_per_action_type(self) -> None:
        cases = {
            "forward 100": 100 / mt.STRAIGHT_SPEED_CM_S,
            "backward 20": 20 / mt.STRAIGHT_SPEED_CM_S,
            "left 90": 90 / mt.TURN_RATE_DEG_S,
            "right 180": 180 / mt.TURN_RATE_DEG_S,
            "line_follow_left 50": 50 / mt.LINE_FOLLOW_SPEED_CM_S,
            "gripper_up": mt.GRIPPER_BOTH_S,
            "gripper_left_down": mt.GRIPPER_SINGLE_S,
            "gripper_right_pos 30": mt.GRIPPER_SINGLE_S,
            "stop": mt.STOP_S,
            "forward": 0.0,  # 不带数值的持续运动会立即就绪
            "dance": 0.0,
            "": 0.0,
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertAlmostEqual(mt.estimate_duration_s(cmd), expected)

    def test_timeout_formula(self) -> None:
        # forward 100 预计 10 秒 -> 10 * 1.5 + 2 = 17 秒
        self.assertAlmostEqual(mt.ready_timeout_s("forward 100"), 17.0)
        # 短动作和未知命令至少 3 秒
        self.assertEqual(mt.ready_timeout_s("gripper_left_up"), 3.5)
        self.assertEqual(mt.ready_timeout_s("stop"), 3.0)
        self.assertEqual(mt.ready_timeout_s(None), 3.0)

    def test_timeout_always_exceeds_estimate(self) -> None:
        for cmd in ("forward 10000", "left 360", "line_follow_right 300"):
            with self.subTest(cmd=cmd):
                self.assertGreater(mt.ready_timeout_s(cmd), mt.estimate_duration_s(cmd))


class SimulatedHubTimeoutTest(unittest.IsolatedAsyncioTestCase):
    # sim_time_scale=0.05: 模拟执行时间和超时都缩短到 1/20, 相当于时间快进,
    # 旧的固定 3 秒超时在这里相当于 0.15 秒, 而 forward 100 要执行 0.5 秒。

    async def test_long_move_followed_by_turn_does_not_time_out(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.05)
        with contextlib.redirect_stdout(io.StringIO()):
            await hub.connect()
            await hub.send("forward 100")
            await hub.send("left 90")  # 等 forward 100 完成 (约 0.5 s), 超时约 0.85 s
        self.assertEqual(hub.last_cmd, "left 90")

    async def test_unresponsive_hub_raises_with_command_name(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.05, sim_unresponsive=True)
        with contextlib.redirect_stdout(io.StringIO()):
            await hub.connect()
            await hub.send("gripper_up")
            with self.assertRaises(HubTimeoutError) as ctx:
                await hub.send("left 90")
        self.assertIn("gripper_up", str(ctx.exception))

    async def test_wait_idle(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.05)
        with contextlib.redirect_stdout(io.StringIO()):
            await hub.connect()
            await hub.send("left 90")
        self.assertTrue(await hub.wait_idle())

        stuck = SpikeHub(simulate=True, sim_time_scale=0.05, sim_unresponsive=True)
        with contextlib.redirect_stdout(io.StringIO()):
            await stuck.connect()
            await stuck.send("stop")
        self.assertFalse(await stuck.wait_idle())


class FailingHub:
    """第 fail_at 条命令 (从 0 开始) 发送失败。"""

    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        if len(self.commands) == self.fail_at:
            self.commands.append(f"FAILED {cmd}")
            raise HubTimeoutError("等待 Hub 完成 'forward 30' 超时 (3.0s)")
        self.commands.append(cmd)


class RobotAgentCommFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_remaining_commands_are_skipped_after_comm_failure(self) -> None:
        hub = FailingHub(fail_at=1)
        agent = RobotAgent(hub)
        seq = {"sequence": [{"cmd": "forward 30"}, {"cmd": "left 90"}, {"cmd": "forward 20"}]}

        with contextlib.redirect_stdout(io.StringIO()):
            result = await agent.execute_sequence(seq)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["executed"], ["forward 30"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["cmd"], "left 90")
        self.assertEqual(
            result["skipped"],
            [{"index": 2, "cmd": "forward 20", "reason": RobotAgent.COMM_SKIP_REASON}],
        )
        self.assertEqual(hub.commands, ["forward 30", "FAILED left 90"])

    async def test_invalid_command_only_skips_itself(self) -> None:
        hub = FailingHub(fail_at=99)
        agent = RobotAgent(hub)
        seq = {"sequence": [{"cmd": "forward 30"}, {"cmd": "left 999"}, {"cmd": "forward 20"}]}

        with contextlib.redirect_stdout(io.StringIO()):
            result = await agent.execute_sequence(seq)

        self.assertEqual(result["executed"], ["forward 30", "forward 20"])
        self.assertEqual(
            result["skipped"], [{"index": 1, "cmd": "left 999", "reason": "value above max"}]
        )


if __name__ == "__main__":
    unittest.main()
