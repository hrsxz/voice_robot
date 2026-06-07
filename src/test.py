import asyncio
from bleak import BleakScanner, BleakClient

# Pybricks Command/Event Characteristic
UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"

# Hub 名称
HUB_NAME = "Pybricks Hub"


async def main():

    ready_event = asyncio.Event()

    # 接收 Hub 发来的数据
    def handle_rx(_, data):

        # 0x01 = stdout
        if data[0] == 0x01:

            payload = data[1:]

            if payload == b"rdy":
                ready_event.set()

            else:
                print("Hub:", payload)

    print("Searching hub...")

    device = await BleakScanner.find_device_by_name(HUB_NAME)

    if device is None:
        print("Cannot find hub.")
        return

    print("Connected.")

    async with BleakClient(device) as client:

        await client.start_notify(UUID, handle_rx)

        async def send(cmd):

            # 等待 hub 发出 rdy
            await ready_event.wait()

            ready_event.clear()

            await client.write_gatt_char(
                UUID,
                b"\x06" + cmd,
                response=True
            )

        print("请按 Spike 中央按钮启动程序")

        while True:

            text = input("> ")

            if text == "exit":
                break

            await send((text + "\n").encode())


asyncio.run(main())