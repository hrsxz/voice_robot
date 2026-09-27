import argparse
import asyncio

from bleak import BleakClient, BleakScanner


class SpikeHub:
    def __init__(self, hub_name: str = "Pybricks Hub", simulate: bool = False):
        self.UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
        self.HUB_NAME = hub_name
        self.client = None
        self.simulate = bool(simulate)
        self.loop = None

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
        self.loop = asyncio.get_running_loop()

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

            if b"rdy" in payload:
                # Hub 可能把 OK 和下一轮 rdy 合并在同一帧里发回来，例如 OKrdy。
                try:
                    if self.loop is not None:
                        self.loop.call_soon_threadsafe(self.ready_event.set)
                    else:
                        self.ready_event.set()
                except Exception:
                    try:
                        self.ready_event.set()
                    except Exception:
                        pass

                payload = payload.replace(b"rdy", b"")

            if payload:
                try:
                    print("Hub:", payload.decode())
                except Exception:
                    print("Hub:", payload)

    async def send(self, cmd: str):
        """
        发送字符串命令，并等待 Hub 再次发出 rdy。
        """

        if self.simulate:
            # 简单模拟：打印并短暂延时模拟 BLE 交互
            print(f"[SIMULATION ANSWER] <- {cmd} Done.")
            # 模拟 hub 需要时间处理并返回 ready
            await asyncio.sleep(0.05)
            return

        # 等待 Hub 发出 rdy
        await self.ready_event.wait()

        # 为下一次发送做准备
        self.ready_event.clear()

        client = self.client
        if client is None:
            raise RuntimeError("Spike Hub is not connected")

        await client.write_gatt_char(
            self.UUID,
            b"\x06" + (cmd + "\n").encode(),
            response=True
        )

        # 命令执行完成后，Hub 会在下一轮循环重新发出 rdy。
        await self.ready_event.wait()


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


def main():
    parser = argparse.ArgumentParser(
        description="SpikeHub PC-side runner (interactive/demo)")
    parser.add_argument("--demo", action="store_true", help="运行示例命令序列")
    parser.add_argument("--simulate", action="store_true", help="启用 simulation 模式（不使用 BLE）")
    args = parser.parse_args()

    spike = SpikeHub(simulate=args.simulate)

    asyncio.run(interactive_mode(spike))


if __name__ == "__main__":
    main()
