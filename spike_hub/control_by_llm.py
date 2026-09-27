from pybricks.hubs import PrimeHub
from pybricks.parameters import Port
from pybricks.pupdevices import Motor
from pybricks.robotics import DriveBase
from pybricks.tools import wait
from uselect import poll
from usys import stdin, stdout

hub = PrimeHub()

left_motor = Motor(Port.E)
right_motor = Motor(Port.A)

# 这里两个数字要按你的车实际尺寸校准
robot = DriveBase(left_motor, right_motor, wheel_diameter=56, axle_track=120)

keyboard = poll()
keyboard.register(stdin)

while True:
    stdout.buffer.write(b"rdy")

    while not keyboard.poll(0):
        wait(10)

    raw = stdin.buffer.readline().strip()
    print(raw)

    parts = raw.split()
    action = parts[0] if parts else b""
    value = None

    if len(parts) > 1:
        value = int(parts[1])

    if action == b"forward":
        if value is None:
            left_motor.dc(-50)
            right_motor.dc(50)
        else:
            robot.straight(value * 10)  # 这里乘以10是因为前端单位是厘米，车的单位是毫米

    elif action == b"backward":
        if value is None:
            left_motor.dc(50)
            right_motor.dc(-50)
        else:
            robot.straight(-value * 10)  # 这里乘以10是因为前端单位是厘米，车的单位是毫米

    elif action == b"left":
        if value is None:
            left_motor.dc(50)
            right_motor.dc(50)
        else:
            robot.turn(-value)

    elif action == b"right":
        if value is None:
            left_motor.dc(-50)
            right_motor.dc(-50)
        else:
            robot.turn(value)

    elif action == b"stop":
        left_motor.stop()
        right_motor.stop()

    elif action == b"bye":
        break

    stdout.buffer.write(b"OK")
