from pybricks.hubs import PrimeHub
from pybricks.parameters import Port
from pybricks.pupdevices import Motor
from pybricks.tools import wait
from uselect import poll
from usys import stdin, stdout

hub = PrimeHub()

drive_motor = Motor(Port.C)
steering_motor = Motor(Port.B)

WHEEL_DIAMETER_MM = 56
DEFAULT_DRIVE_DC = 50
DEFAULT_STEERING_ANGLE = 45
STEERING_SPEED = 200
DRIVE_SPEED = 360
TURN_DRIVE_DEGREES_PER_HEADING_DEG = 6


def distance_cm_to_motor_degrees(distance_cm):
    distance_mm = distance_cm * 10
    wheel_circumference_mm = WHEEL_DIAMETER_MM * 3.1416
    return int(distance_mm * 360 / wheel_circumference_mm)


def steer_to(angle_deg):
    steering_motor.run_target(STEERING_SPEED, angle_deg)


def turn_by_heading(direction, heading_deg=None):
    steer_angle = direction * DEFAULT_STEERING_ANGLE
    steer_to(steer_angle)

    if heading_deg is None:
        drive_motor.dc(DEFAULT_DRIVE_DC)
        return

    drive_motor.run_angle(
        DRIVE_SPEED,
        int(heading_deg * TURN_DRIVE_DEGREES_PER_HEADING_DEG),
        wait=True,
    )
    steer_to(0)

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
            drive_motor.dc(DEFAULT_DRIVE_DC)
        else:
            drive_motor.run_angle(DRIVE_SPEED, distance_cm_to_motor_degrees(value), wait=True)

    elif action == b"backward":
        if value is None:
            drive_motor.dc(-DEFAULT_DRIVE_DC)
        else:
            drive_motor.run_angle(DRIVE_SPEED, -distance_cm_to_motor_degrees(value), wait=True)

    elif action == b"left":
        if value is None:
            turn_by_heading(-1)
        else:
            turn_by_heading(-1, value)

    elif action == b"right":
        if value is None:
            turn_by_heading(1)
        else:
            turn_by_heading(1, value)

    elif action == b"stop":
        drive_motor.stop()
        steer_to(0)

    elif action == b"bye":
        drive_motor.stop()
        steer_to(0)
        break

    stdout.buffer.write(b"OK")
