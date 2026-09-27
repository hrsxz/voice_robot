from pybricks.hubs import PrimeHub
from pybricks.parameters import Port, Direction
from pybricks.pupdevices import ColorSensor, Motor
from pybricks.robotics import DriveBase
from pybricks.tools import wait
from uselect import poll
from usys import stdin, stdout

hub = PrimeHub()

left_motor = Motor(Port.E, Direction.CLOCKWISE)
right_motor = Motor(Port.A, Direction.COUNTERCLOCKWISE)
gripper_left = Motor(Port.B)
gripper_right = Motor(Port.F, Direction.CLOCKWISE)
color_sensor = ColorSensor(Port.D)


# drive base
robot = DriveBase(left_motor, right_motor, wheel_diameter=62, axle_track=167)

# 启动前应把两个夹爪手动放在 down 位置
LEFT_DOWN = 0
LEFT_UP = 90
RIGHT_DOWN = 0
RIGHT_UP = -90

GRIPPER_SPEED = 200
LINE_TARGET = 58
LINE_SPEED = 50
LINE_KP = 3
MAX_TURN_RATE = 60

gripper_left.run_target(GRIPPER_SPEED, LEFT_DOWN, wait=True)
gripper_right.run_target(GRIPPER_SPEED, RIGHT_DOWN, wait=True)

keyboard = poll()
keyboard.register(stdin)


def clamp_angle(angle):
    return max(-90, min(90, int(angle)))


def clamp_user_pos_0_90(value):
    return max(0, min(90, int(value)))


def set_gripper(motor, angle):
    angle = clamp_angle(angle)
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
    # print(raw)

    parts = raw.split()
    action = parts[0] if parts else b""
    value = None

    if len(parts) > 1:
        value = int(parts[1])

    if action == b"forward":
        if value is None:
            left_motor.dc(50)
            right_motor.dc(50)
        else:
            robot.straight(value * 10)  # 这里乘以10是因为前端单位是厘米，车的单位是毫米

    elif action == b"backward":
        if value is None:
            left_motor.dc(-50)
            right_motor.dc(-50)
        else:
            robot.straight(-value * 10)  # 这里乘以10是因为前端单位是厘米，车的单位是毫米

    elif action == b"left":
        if value is None:
            left_motor.dc(50)
            right_motor.dc(-50)
        else:
            robot.turn(value)

    elif action == b"right":
        if value is None:
            left_motor.dc(-50)
            right_motor.dc(50)
        else:
            robot.turn(-value)

    elif action == b"stop":
        left_motor.stop()
        right_motor.stop()


    elif action == b"gripper_up":
        set_gripper(gripper_left, LEFT_UP)
        set_gripper(gripper_right, RIGHT_UP)
    elif action == b"gripper_down":
        set_gripper(gripper_left, LEFT_DOWN)
        set_gripper(gripper_right, RIGHT_DOWN)

    elif action in (b"gripper_left_up", b"gripper_left_up"):
        set_gripper(gripper_left, LEFT_UP)
    elif action in (b"gripper_left_down", b"gripper_left_down"):
        set_gripper(gripper_left, LEFT_DOWN)
    elif action in (b"gripper_left_pos", b"gripper_left_pos") and value is not None:
        set_gripper(gripper_left, value)

    elif action in (b"gripper_right_up", b"gripper_right_up"):
        set_gripper(gripper_right, RIGHT_UP)
    elif action in (b"gripper_right_down", b"gripper_right_down"):
        set_gripper(gripper_right, RIGHT_DOWN)
    elif action in (b"gripper_right_pos", b"gripper_right_pos") and value is not None:
        set_gripper(gripper_right, -clamp_user_pos_0_90(value))

    elif action == b"line_follow_left" and value is not None:
        follow_line(b"left", value)
    elif action == b"line_follow_right" and value is not None:
        follow_line(b"right", value)
    elif action == b"line_follow_stop":
        robot.stop()
    elif action == b"bye":
        break

    stdout.buffer.write(b"OK")
