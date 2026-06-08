import asyncio

from bleak import BleakClient, BleakScanner

UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"

HUB_NAME = "Pybricks Hub"


async def main():

    ready_event = asyncio.Event()

    def handle_rx(_, data):

        if data[0] == 0x01:

            payload = data[1:]
            if payload == b"rdy":
                ready_event.set()

            else:
                print("Hub:", payload)

    device = await BleakScanner.find_device_by_name(HUB_NAME)

    async with BleakClient(device) as client:

        await client.start_notify(UUID, handle_rx)

        async def send(cmd):

            await ready_event.wait()
            ready_event.clear()

            await client.write_gatt_char(
                UUID,
                b"\x06" + cmd,
                response=True
            )

        print("按 Spike 按钮启动程序")

        while True:
            cmd = input("> ")


            if cmd == "exit":
                break

            await send((cmd + "\n").encode())


asyncio.run(main())
