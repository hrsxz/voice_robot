import asyncio

from bleak import BleakClient, BleakScanner

UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"

HUB_NAME = "Pybricks Hub"


async def main():

    ready_event = asyncio.Event()
    rx_buffer = b""
    loop = asyncio.get_running_loop()

    def handle_rx(_, data):
        nonlocal rx_buffer

        if not data or data[0] != 0x01:
            return

        rx_buffer += data[1:]

        while True:
            idx = rx_buffer.find(b"rdy")
            if idx < 0:
                break

            head = rx_buffer[:idx].strip()
            rx_buffer = rx_buffer[idx + 3:]

            loop.call_soon_threadsafe(ready_event.set)

            if head:
                try:
                    print("Hub:", head.decode())
                except Exception:
                    print("Hub:", head)

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
