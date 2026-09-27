import argparse
import asyncio

from bleak import BleakClient, BleakScanner


class SpikeHub:
    def __init__(self, hub_name: str = "Pybricks Hub", simulate: bool = False):
        self.UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
        self.HUB_NAME = hub_name
        self.client = None
        self.simulate = bool(simulate)

        # Hub 发来 rdy 时 set()
        self.ready_event = asyncio.Event()

        # 新增: BLE 通知分包缓冲
        self.rx_buffer = b""
        # 新增: 主事件循环引用，用于线程安全 set()
        self.loop = None

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
        
        # 新增: 保存当前 running loop，供 notify 回调线程安全调度
        self.loop = asyncio.get_running_loop()
        self.rx_buffer = b""
        self.ready_event.clear()

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

        if data[0] != 0x01:
            return

        payload = data[1:]
        # 新增: 先入缓冲，支持 OKrd + y 这种跨包
        self.rx_buffer += payload

        while True:
            idx = self.rx_buffer.find(b"rdy")
            if idx < 0:
                break

            # rdy 前面的内容作为普通输出
            head = self.rx_buffer[:idx].strip()
            # 消费到 rdy 末尾
            self.rx_buffer = self.rx_buffer[idx + 3:]

            # 识别到 rdy 后释放等待
            try:
                if self.loop is not None:
                    self.loop.call_soon_threadsafe(self.ready_event.set)
                else:
                    self.ready_event.set()
            except Exception:
                pass

            if head:
                try:
                    print("Hub:", head.decode())
                except Exception:
                    print("Hub:", head)

        # 可选: 防止极端情况下缓冲无限增长
        if len(self.rx_buffer) > 4096:
            self.rx_buffer = self.rx_buffer[-1024:]

    async def send(self, cmd: str):
        """
        发送字符串命令
        """

        if self.simulate:
            # 简单模拟：打印并短暂延时模拟 BLE 交互
            print(f"[SIMULATION ANSWER] <- {cmd} Done.")
            # 模拟 hub 需要时间处理并返回 ready
            await asyncio.sleep(0.05)
            return

        # 等待 Hub 发出 rdy
        await asyncio.wait_for(self.ready_event.wait(), timeout=3.0)

        # 为下一次发送做准备
        self.ready_event.clear()

        await self.client.write_gatt_char(
            self.UUID,
            b"\x06" + (cmd + "\n").encode(),
            response=True
        )


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
