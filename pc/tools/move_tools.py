async def move_forward(distance_cm:int):

    await spike.move_cm(distance_cm)

async def turn_left(angle:int):

    await spike.turn_deg(-angle)

async def stop():

    await spike.stop()