import asyncio

from bleak import BleakClient, BleakScanner


class SpikeHub:
    def __init__(self, hub_name: str = "Pybricks Hub"):
        self.UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
        self.HUB_NAME = hub_name
        self.client = None

        # Hub 发来 rdy 时 set()
        self.ready_event = asyncio.Event()

    async def connect(self):
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
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    def handle_rx(self, _, data):
        """
        接收 Hub 发来的数据
        """

        if data[0] == 0x01:

            payload = data[1:]

            if payload == b"rdy":
                self.ready_event.set()

            else:
                print("Hub:", payload)

    async def send(self, cmd: str):
        """
        发送字符串命令
        """

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
