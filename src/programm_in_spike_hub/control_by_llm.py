from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor, ColorSensor, UltrasonicSensor, ForceSensor
from pybricks.parameters import Button, Color, Direction, Port, Side, Stop
from pybricks.robotics import DriveBase
from pybricks.tools import wait, StopWatch

hub = PrimeHub()


from usys import stdin, stdout
from uselect import poll

left_motor = Motor(Port.E)
right_motor = Motor(Port.A)

keyboard = poll()
keyboard.register(stdin)

while True:

    # 通知 PC：我准备好了
    stdout.buffer.write(b"rdy")

    while not keyboard.poll(0):
        wait(10)

    cmd = stdin.buffer.readline().strip()

    print(cmd)

    if cmd == b"forward":

        left_motor.dc(-50)
        right_motor.dc(50)

    elif cmd == b"backward":

        left_motor.dc(50)
        right_motor.dc(-50)

    elif cmd == b"left":

        left_motor.dc(50)
        right_motor.dc(50)

    elif cmd == b"right":

        left_motor.dc(-50)
        right_motor.dc(-50)

    elif cmd == b"stop":

        left_motor.stop()
        right_motor.stop()

    elif cmd == b"bye":
        break

    stdout.buffer.write(b"OK")