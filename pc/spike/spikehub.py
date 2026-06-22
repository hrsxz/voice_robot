import argparse
import asyncio
import time

from bleak import BleakClient, BleakScanner


class SpikeHub:
    def __init__(self, hub_name: str = "Pybricks Hub", simulate: bool = False):
        self.UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
        self.HUB_NAME = hub_name
        self.client = None
        self.simulate = bool(simulate)

        # Hub 发来 rdy 时 set()
        self.ready_event = asyncio.Event()

    async def connect(self):
        if self.simulate:
            print("[SIM] SpikeHub simulation mode: connected (no BLE)")
            # simulation mode: no BLE, mark ready
            try:
                # ensure ready_event is set for first send
                self.ready_event.set()
            except Exception:
                pass
            return

        print("Searching hub...")

        device = await BleakScanner.find_device_by_name(self.HUB_NAME)
        if device is None:
            raise Exception(f"Cannot find {self.HUB_NAME}")

        self.client = BleakClient(device)

        await self.client.connect()
        await self.client.start_notify(
            self.UUID,
            self.handle_rx
        )
        print("Connected.")

    async def disconnect(self):
        if self.simulate:
            print("[SIM] SpikeHub simulation mode: disconnected")
            return

        if self.client is not None:
            try:
                await self.client.stop_notify(self.UUID)
            except Exception:
                pass
            await self.client.disconnect()
            self.client = None

    def handle_rx(self, _, data):
        """
        接收 Hub 发来的数据
        """

        if not data:
            return

        if data[0] == 0x01:

            payload = data[1:]

            if payload == b"rdy":
                # 在 notify 回调线程中安全地 set
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon_threadsafe(self.ready_event.set)
                except Exception:
                    # fallback
                    try:
                        self.ready_event.set()
                    except Exception:
                        pass

            else:
                try:
                    print("Hub:", payload.decode())
                except Exception:
                    print("Hub:", payload)

    async def send(self, cmd: str):
        """
        发送字符串命令
        """

        if self.simulate:
            # 简单模拟：打印并短暂延时模拟 BLE 交互
            print(f"[SIM] -> {cmd}")
            # 模拟 hub 需要时间处理并返回 ready
            await asyncio.sleep(0.05)
            return

        # 等待 Hub 发出 rdy
        await self.ready_event.wait()

        # 为下一次发送做准备
        self.ready_event.clear()

        await self.client.write_gatt_char(
            self.UUID,
            b"\x06" + (cmd + "\n").encode(),
            response=True
        )

    async def forward(self):
        await self.send("forward")

    async def backward(self):
        await self.send("backward")

    async def stop(self):
        await self.send("stop")

    async def turn_left(self):
        await self.send("left")

    async def turn_right(self):
        await self.send("right")

    async def move_cm(self, distance_cm: int):
        """
        以后实现
        """
        raise NotImplementedError

    async def turn_deg(self, angle_deg: int):
        """
        以后实现
        """
        raise NotImplementedError


# ----- CLI / demo runner -----
async def interactive_mode(spike: SpikeHub):
    print("尝试连接 Spike Hub...")
    await spike.connect()
    print("连接成功。请在 Spike 上运行 Hub 程序（control_by_llm.py），按 Spike 按钮启动后输入命令。输入 'exit' 退出。")
    try:
        while True:
            cmd = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            if not cmd:
                continue
            if cmd.strip().lower() in ("exit", "quit"):
                break
            await spike.send(cmd.strip())
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await spike.disconnect()
        print("已断开连接。")


async def demo_sequence(spike: SpikeHub, commands, delay: float = 3):
    await spike.connect()
    for c in commands:
        print("发送:", c)
        await spike.send(c)
        await asyncio.sleep(delay)
    await spike.disconnect()
    print("示例序列完成。")


def main():
    parser = argparse.ArgumentParser(
        description="SpikeHub PC-side runner (interactive/demo)")
    parser.add_argument("--demo", action="store_true", help="运行示例命令序列")
    parser.add_argument("--simulate", action="store_true", help="启用 simulation 模式（不使用 BLE）")
    args = parser.parse_args()

    spike = SpikeHub(simulate=args.simulate)

    if args.demo:
        cmds = ["forward", "stop", "left", "right", "backward", "stop"]
        asyncio.run(demo_sequence(spike, cmds))
    else:
        asyncio.run(interactive_mode(spike))


if __name__ == "__main__":
    main()
