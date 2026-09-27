import asyncio

from pc.spike import spikehub


async def main():

    hub = spikehub.SpikeHub()

    await hub.connect()

    print("按 Spike 按钮启动程序")

    while True:

        cmd = input("> ")

        if cmd == "w":
            await hub.forward()

        elif cmd == "s":
            await hub.backward()

        elif cmd == "a":
            await hub.turn_left()

        elif cmd == "d":
            await hub.turn_right()

        elif cmd == "x":
            await hub.stop()

        elif cmd == "exit":
            break

    await hub.disconnect()


asyncio.run(main())
