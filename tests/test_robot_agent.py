import unittest

from pc.agent.robot_agent import RobotAgent


class DummyHub:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class RobotAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_forward_command_executes(self) -> None:
        hub = DummyHub()
        agent = RobotAgent(hub)

        result = await agent.execute_sequence({"sequence": [{"cmd": "forward 30"}]})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["executed"], ["forward 30"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(hub.commands, ["forward 30"])

    async def test_left_command_executes(self) -> None:
        hub = DummyHub()
        agent = RobotAgent(hub)

        result = await agent.execute_sequence({"sequence": [{"cmd": "left 90"}]})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["executed"], ["left 90"])
        self.assertEqual(hub.commands, ["left 90"])

    async def test_camera_command_routes_to_tool(self) -> None:
        hub = DummyHub()
        agent = RobotAgent(hub)

        result = await agent.execute_sequence({"sequence": [{"cmd": "camera photo"}]})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["executed"], ["simulated photo"])
        self.assertEqual(hub.commands, [])

    async def test_sensor_command_routes_to_tool(self) -> None:
        hub = DummyHub()
        agent = RobotAgent(hub)

        result = await agent.execute_sequence({"sequence": [{"cmd": "sensor distance"}]})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["executed"], ["simulated distance"])
        self.assertEqual(hub.commands, [])

    async def test_out_of_range_command_is_skipped(self) -> None:
        hub = DummyHub()
        agent = RobotAgent(hub)

        result = await agent.execute_sequence({"sequence": [{"cmd": "left 999"}]})

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["executed"], [])
        self.assertEqual(
            result["skipped"],
            [{"index": 0, "cmd": "left 999", "reason": "value above max"}],
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(hub.commands, [])


if __name__ == "__main__":
    """
        python -m unittest tests.test_robot_agent                          
        ...Skipped command: left 999, reason: value above max
        ..
        ----------------------------------------------------------------------
        Ran 5 tests in 0.140s
        OK
    """
    unittest.main()
