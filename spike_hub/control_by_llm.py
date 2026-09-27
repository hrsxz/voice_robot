from pybricks.hubs import PrimeHub
from pybricks.parameters import Port
from pybricks.pupdevices import ColorSensor, Motor
from pybricks.robotics import DriveBase
from pybricks.tools import wait
from uselect import poll
from usys import stdin, stdout

hub = PrimeHub()

left_motor = Motor(Port.E)
right_motor = Motor(Port.A)
gripper_b = Motor(Port.B)
gripper_f = Motor(Port.F)
color_sensor = ColorSensor(Port.D)

# drive base
robot = DriveBase(left_motor, right_motor, wheel_diameter=62, axle_track=167)

# 启动前应把两个夹爪手动放在 down 位置
gripper_b.reset_angle(0)
gripper_f.reset_angle(0)

GRIPPER_SPEED = 200
LINE_TARGET = 58
LINE_SPEED = 50
LINE_KP = 3
MAX_TURN_RATE = 60

keyboard = poll()
keyboard.register(stdin)


def set_gripper(motor, angle):
    angle = max(0, min(90, angle))
    motor.run_target(GRIPPER_SPEED, angle, wait=True)


def follow_line(edge, distance_cm):
    start_distance = robot.distance()
    target_distance_mm = distance_cm * 10

    # 左边缘：左侧白、右侧黑；右边缘相反
    edge_sign = 1 if edge == b"left" else -1

    while abs(robot.distance() - start_distance) < target_distance_mm:
        reflection = color_sensor.reflection()
        error = reflection - LINE_TARGET
        turn_rate = edge_sign * LINE_KP * error
        turn_rate = max(-MAX_TURN_RATE, min(MAX_TURN_RATE, turn_rate))

        robot.drive(LINE_SPEED, turn_rate)
        wait(10)

    robot.stop()


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

    elif action == b"gripper_b_down":
        set_gripper(gripper_b, 0)
    elif action == b"gripper_b_up":
        set_gripper(gripper_b, 90)
    elif action == b"gripper_b_pos" and value is not None:
        set_gripper(gripper_b, value)

    elif action == b"gripper_f_down":
        set_gripper(gripper_f, 0)
    elif action == b"gripper_f_up":
        set_gripper(gripper_f, 90)
    elif action == b"gripper_f_pos" and value is not None:
        set_gripper(gripper_f, value)

    elif action == b"line_follow_left" and value is not None:
        follow_line(b"left", value)
    elif action == b"line_follow_right" and value is not None:
        follow_line(b"right", value)
    elif action == b"line_follow_stop":
        robot.stop()
    elif action == b"bye":
        break

    stdout.buffer.write(b"OK")
