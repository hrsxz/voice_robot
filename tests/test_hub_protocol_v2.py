import ast
import asyncio
import contextlib
import io
import time
import unittest
from unittest.mock import patch

from pc import constants
from pc.agent.robot_agent import CANCELLED, EXECUTED, INTERRUPTED, REJECTED, RobotAgent
from pc.spike_communication import hub_protocol as hp
from pc.spike_communication.spikehub import HubTimeoutError, HubVersionError, SpikeHub
from skills import load_skill_registry


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ParserTest(unittest.TestCase):
    def test_parse_replies(self) -> None:
        r = hp.parse_reply("DONE forward 30 gl=0 gr=90")
        self.assertEqual((r.kind, r.cmd, r.gripper), ("DONE", "forward 30", (0, 90)))
        r = hp.parse_reply("STOPPED forward gl=0 gr=0 reason=watchdog")
        self.assertEqual((r.kind, r.cmd, r.reason), ("STOPPED", "forward", "watchdog"))
        r = hp.parse_reply("ERR gripper_left_pos 120 bad_value")
        self.assertEqual((r.kind, r.cmd, r.reason, r.reason_text),
                         ("ERR", "gripper_left_pos 120", "bad_value", "参数非法"))
        self.assertEqual(hp.parse_reply("ACK left 90").cmd, "left 90")
        self.assertEqual(hp.parse_reply("VERSION 2").kind, "VERSION")
        self.assertEqual(hp.parse_reply("rdy").kind, "RDY")

    def test_split_packets_and_glued_replies(self) -> None:
        hub = SpikeHub(simulate=True)
        seen: list[str] = []
        hub._process = lambda reply: seen.append(reply.kind)
        # BLE 分包: 一行被拆开, 多行粘在一起
        parts = ("ACK forw", "ard 30\nDONE forward 30 g",
                 "l=0 gr=0\nrdy\nERR dance unknown\nrdy\n")
        for part in parts:
            hub._feed(part)
        self.assertEqual(seen, ["ACK", "DONE", "RDY", "ERR", "RDY"])

    def test_legacy_hub_output(self) -> None:
        hub = SpikeHub(simulate=True)
        seen: list[str] = []
        hub._process = lambda reply: seen.append(reply.raw)
        hub._feed("rdyOKrdy")
        self.assertEqual(seen, ["rdy", "OK", "rdy"])

    def test_validate_command(self) -> None:
        self.assertIsNone(hp.validate_command("forward 30"))
        self.assertIsNone(hp.validate_command("forward"))  # 持续运动
        self.assertEqual(hp.validate_command("dance"), "unknown")
        self.assertEqual(hp.validate_command("gripper_left_pos"), "missing_value")
        self.assertEqual(hp.validate_command("gripper_left_pos 120"), "bad_value")
        self.assertEqual(hp.validate_command("stop 3"), "bad_value")


class HandshakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_v2_program_is_accepted(self) -> None:
        hub = SpikeHub(simulate=True)
        with quiet():
            await hub.connect()
        self.assertTrue(hub.protocol_ok)
        self.assertEqual(hub.protocol_version, 2)

    async def test_old_program_blocks_motion(self) -> None:
        hub = SpikeHub(simulate=True, version_timeout=0.1)

        def old_program(line: str) -> None:
            # 旧程序: 不认识 version, 照样回 "OK" + "rdy" (没有换行)
            asyncio.get_running_loop().call_soon(hub._feed, "OKrdy")

        out = io.StringIO()
        with patch.object(hub, "_sim_receive", old_program), contextlib.redirect_stdout(out):
            await hub.connect()
            with self.assertRaises(HubVersionError):
                await hub.send("forward 30")
        self.assertFalse(hub.protocol_ok)
        self.assertIn("Hub 程序需要更新", out.getvalue())


class StopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_interrupts_running_command_without_waiting(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=1.0)  # forward 100 要 10 秒
        with quiet():
            await hub.connect()
            await hub.send("forward 100")
            t0 = time.perf_counter()
            confirmed = await hub.send_stop()
            elapsed = time.perf_counter() - t0
            reply = await hub.wait_done()

        self.assertTrue(confirmed)
        self.assertLess(elapsed, 0.3)  # 不用等 10 秒的动作做完
        self.assertEqual((reply.kind, reply.cmd), ("STOPPED", "forward 100"))

    async def test_stop_when_idle(self) -> None:
        hub = SpikeHub(simulate=True)
        with quiet():
            await hub.connect()
            self.assertTrue(await hub.send_stop())

    async def test_hard_stop_when_hub_does_not_confirm(self) -> None:
        hub = SpikeHub(simulate=True, sim_unresponsive=True, stop_confirm_timeout=0.05)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await hub.connect()
            await hub.send("forward 100")
            confirmed = await hub.send_stop()
            with self.assertRaises(HubTimeoutError):
                await hub.send("left 90")  # 程序已停止
        self.assertFalse(confirmed)
        self.assertFalse(hub.program_running)
        self.assertIsNone(hub.gripper)
        self.assertIn("硬急停", out.getvalue())

        hub.sim_unresponsive = False
        with quiet():
            await hub.restart_program()  # 需要用户确认后才调用
            await hub.send("left 90")
        self.assertTrue(hub.program_running)
        self.assertTrue(hub.protocol_ok)


class HeartbeatTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_during_continuous_motion_only(self) -> None:
        hub = SpikeHub(simulate=True, heartbeat_interval=0.05)
        written: list[str] = []
        original = hub._sim_receive

        def record(line: str) -> None:
            written.append(line)
            original(line)

        with patch.object(hub, "_sim_receive", record), quiet():
            await hub.connect()
            await hub.send("forward")  # 不带数值: 持续运动
            await asyncio.sleep(0.28)
            beats = written.count("hb")
            await hub.send_stop()
            await asyncio.sleep(0.15)
        self.assertGreaterEqual(beats, 4)
        self.assertEqual(written.count("hb"), beats)  # 停止后不再发心跳

    async def test_no_heartbeat_for_bounded_motion(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.02, heartbeat_interval=0.02)
        written: list[str] = []
        original = hub._sim_receive
        def receive(line):
            written.append(line)
            return original(line)

        with patch.object(hub, "_sim_receive", receive), quiet():
            await hub.connect()
            await hub.send("forward 30")
            await hub.wait_done()
        self.assertNotIn("hb", written)


class SimulatedErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_err_replies(self) -> None:
        hub = SpikeHub(simulate=True)
        with quiet():
            await hub.connect()
            await hub.send("dance")
            r1 = await hub.wait_done()
            await hub.send("gripper_left_pos")
            r2 = await hub.wait_done()
        self.assertEqual((r1.kind, r1.reason), ("ERR", "unknown"))
        self.assertEqual((r2.kind, r2.reason), ("ERR", "missing_value"))


class AgentV2Test(unittest.IsolatedAsyncioTestCase):
    async def test_stop_interrupts_running_and_cancels_queued(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=1.0)
        agent = RobotAgent(hub)
        with quiet():
            await agent.connect()
            batch = agent.submit({"sequence": [{"cmd": "forward 100"}, {"cmd": "left 90"}]})
            await asyncio.sleep(0.05)  # forward 100 正在执行
            await agent.stop()
            await asyncio.wait_for(batch.wait(), 1)
        self.assertEqual([r.status for r in batch.records], [INTERRUPTED, CANCELLED])
        self.assertIn("被停止中断 forward 100", batch.summary_line())

    async def test_gripper_state_in_results(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.01)
        agent = RobotAgent(hub)
        with quiet():
            await agent.connect()
            self.assertIsNone(agent.gripper_state)  # 还没回报过
            result = await agent.execute_sequence({"sequence": [{"cmd": "gripper_up"}]})
        self.assertEqual(result["gripper"], (90, 90))
        self.assertEqual(agent.gripper_state, (90, 90))

        with quiet():
            await hub.hard_stop()
        self.assertIsNone(agent.gripper_state)  # 程序停止后状态未知

    async def test_hub_err_is_rejected_and_rest_continues(self) -> None:
        hub = SpikeHub(simulate=True, sim_time_scale=0.01)
        agent = RobotAgent(hub)
        real_validate = hp.validate_command

        def hub_rejects_left(cmd: str):
            return "unknown" if cmd.startswith("left") else real_validate(cmd)

        with patch.object(hp, "validate_command", hub_rejects_left), quiet():
            await agent.connect()
            batch = agent.submit({"sequence": [
                {"cmd": "forward 30"}, {"cmd": "left 90"}, {"cmd": "forward 20"},
            ]})
            await asyncio.wait_for(batch.wait(), 2)
        self.assertEqual([r.status for r in batch.records], [EXECUTED, REJECTED, EXECUTED])
        self.assertEqual(batch.records[1].detail, "Hub：未知命令")


class SkillHubConsistencyTest(unittest.TestCase):
    def test_every_move_skill_action_is_supported_by_hub(self) -> None:
        registry = load_skill_registry()
        move_actions = {a for a, r in registry.actions.items() if r.route == "move"}
        self.assertEqual(move_actions, set(hp.HUB_ACTIONS))
        for action in move_actions:
            rule = registry.actions[action]
            cmd = action if rule.value_type == "none" else f"{action} {rule.max_value}"
            with self.subTest(cmd=cmd):
                self.assertIsNone(hp.validate_command(cmd))

    def test_hub_program_action_table_matches_protocol(self) -> None:
        path = constants.project_root_path / "spike_hub" / "control_by_llm.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        table = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ACTIONS":
                pairs = zip(node.value.keys, node.value.values, strict=True)
                table = {k.value.decode(): v.value for k, v in pairs}
        self.assertEqual(table, hp.HUB_ACTIONS)


if __name__ == "__main__":
    unittest.main()
