import asyncio
import contextlib
import io
import unittest

from pc.agent.robot_agent import CANCELLED, EXECUTED, FAILED, REJECTED, RobotAgent


class SlowHub:
    """每个动作发出后要等测试代码调用 finish() 才算执行完。"""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._busy: asyncio.Event | None = None

    async def send(self, cmd: str) -> None:
        # 和真实 Hub 一样: 上一个动作没做完就要等
        if self._busy is not None:
            await self._busy.wait()
        self.commands.append(cmd)
        self._busy = asyncio.Event()

    async def wait_idle(self) -> bool:
        if self._busy is not None:
            await self._busy.wait()
        return True

    def finish(self) -> None:
        if self._busy is not None:
            self._busy.set()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class ExecutionOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_sources_execute_in_enqueue_order(self) -> None:
        hub = SlowHub()
        agent = RobotAgent(hub)
        batch = agent.new_batch()

        agent.enqueue(batch, "forward 30")  # 来自快速通道
        await settle()
        agent.enqueue(batch, "left 90")  # 随后来自 LLM
        batch.close()

        await settle()
        self.assertEqual(hub.commands, ["forward 30"])  # 一次只执行一条
        hub.finish()
        await settle()
        self.assertEqual(hub.commands, ["forward 30", "left 90"])
        hub.finish()
        await asyncio.wait_for(batch.wait(), 1)
        self.assertEqual([r.status for r in batch.records], [EXECUTED, EXECUTED])

    async def test_batch_stays_open_until_closed(self) -> None:
        hub = SlowHub()
        agent = RobotAgent(hub)
        batch = agent.new_batch()
        agent.enqueue(batch, "stop")
        await settle()
        hub.finish()
        await settle()
        self.assertFalse(batch.is_done)  # 流式模式下这句话可能还有后续命令
        batch.close()
        await asyncio.wait_for(batch.wait(), 1)


class StopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_queued_commands(self) -> None:
        hub = SlowHub()
        agent = RobotAgent(hub)
        batch = agent.submit(
            {"sequence": [{"cmd": "forward 100"}, {"cmd": "left 90"}, {"cmd": "right 30"}]}
        )
        await settle()
        self.assertEqual(hub.commands, ["forward 100"])  # 正在执行

        stop_task = asyncio.create_task(agent.stop())
        await settle()
        hub.finish()  # 1b 中正在执行的动作还不能被打断, 要等它做完 (hub-protocol-v2 解决)
        cancelled = await asyncio.wait_for(stop_task, 1)
        hub.finish()

        self.assertEqual([r.display for r in cancelled], ["left 90", "right 30"])
        self.assertEqual(hub.commands, ["forward 100", "stop"])
        await asyncio.wait_for(batch.wait(), 1)
        self.assertEqual(
            [r.status for r in batch.records], [EXECUTED, CANCELLED, CANCELLED]
        )
        self.assertIn("被停止取消 left 90", batch.summary_line())

    async def test_stop_with_empty_queue_just_sends_stop(self) -> None:
        hub = SlowHub()
        agent = RobotAgent(hub)
        cancelled = await agent.stop()
        self.assertEqual(cancelled, [])
        self.assertEqual(hub.commands, ["stop"])


class ResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_command_has_one_status_and_summary(self) -> None:
        class FailOnRight(SlowHub):
            async def send(self, cmd: str) -> None:
                if cmd.startswith("right"):
                    raise ValueError("bad write")
                await super().send(cmd)
                self.finish()

        hub = FailOnRight()
        agent = RobotAgent(hub)
        with contextlib.redirect_stdout(io.StringIO()):
            batch = agent.submit({"sequence": [
                {"cmd": "forward 30"}, {"cmd": "forward 20000"}, {"cmd": "dance"},
            ]})
            await asyncio.wait_for(batch.wait(), 1)

        self.assertEqual([r.status for r in batch.records], [EXECUTED, REJECTED, REJECTED])
        line = batch.summary_line()
        self.assertIn("已执行 forward 30", line)
        self.assertIn("被拒绝 forward 20000（value above max）", line)
        self.assertIn("被拒绝 dance（unknown action）", line)
        self.assertEqual(batch.result()["status"], "partial")

    async def test_comm_failure_marks_failed_and_skips_rest(self) -> None:
        class BrokenHub(SlowHub):
            async def send(self, cmd: str) -> None:
                if cmd == "left 90":
                    raise ConnectionError("BLE disconnected")
                await super().send(cmd)
                self.finish()

        agent = RobotAgent(BrokenHub())
        batch = agent.submit({"sequence": [
            {"cmd": "forward 30"}, {"cmd": "left 90"}, {"cmd": "forward 20"},
        ]})
        await asyncio.wait_for(batch.wait(), 1)

        statuses = [r.status for r in batch.records]
        self.assertEqual(statuses[:2], [EXECUTED, FAILED])
        self.assertIn("因通信失败跳过 forward 20", batch.summary_line())


if __name__ == "__main__":
    unittest.main()
