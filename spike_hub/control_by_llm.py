# Spike Hub 程序 (Pybricks), 协议 v2。PC 端对应 pc/spike_communication/hub_protocol.py
#
# 主循环分两种状态:
#   空闲: 等待一行命令, 校验后开始执行 (不等待动作完成), 回复 ACK, 进入忙碌
#   忙碌: 每 10 ms 检查一次: 动作做完了没有? 有没有新命令?
#         收到 stop 立即停车并回复 STOPPED; 收到 hb 只刷新心跳时间; 其他命令回复 ERR busy
#
# 回复格式 (每条一行):
#   rdy / ACK <cmd> / DONE <cmd> gl=.. gr=.. / STOPPED <cmd> gl=.. gr=.. [reason=watchdog]
#   ERR <cmd> <unknown|missing_value|bad_value|busy> / VERSION 2
# 夹爪角度统一成 0 = 放下, 90 = 抬起。
#
# 需要较新的 Pybricks 固件 (straight/turn/run_target 支持 wait=False, 以及 done())。

from pybricks.hubs import PrimeHub
from pybricks.parameters import Direction, Port
from pybricks.pupdevices import ColorSensor, Motor
from pybricks.robotics import DriveBase
from pybricks.tools import StopWatch, wait
from uselect import poll
from usys import stdin, stdout

PROTOCOL_VERSION = 2

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
DC_POWER = 50

LOOP_MS = 10
WATCHDOG_MS = 1000  # 持续运动时, 超过这个时间没收到 PC 的消息就停车

gripper_left.run_target(GRIPPER_SPEED, LEFT_DOWN, wait=True)
gripper_right.run_target(GRIPPER_SPEED, RIGHT_DOWN, wait=True)

keyboard = poll()
keyboard.register(stdin)
clock = StopWatch()

# 动作名 -> 参数要求 (和 PC 端 hub_protocol.HUB_ACTIONS 一致)
ACTIONS = {
    b"forward": "optional_distance",
    b"backward": "optional_distance",
    b"left": "optional_angle",
    b"right": "optional_angle",
    b"stop": "none",
    b"gripper_up": "none",
    b"gripper_down": "none",
    b"gripper_pos": "angle90",
    b"gripper_left_up": "none",
    b"gripper_left_down": "none",
    b"gripper_left_pos": "angle90",
    b"gripper_right_up": "none",
    b"gripper_right_down": "none",
    b"gripper_right_pos": "angle90",
    b"line_follow_left": "distance",
    b"line_follow_right": "distance",
}


def send(line):
    stdout.buffer.write((line + "\n").encode())


def gripper_state():
    # 统一换算成 0 = 放下, 90 = 抬起 (右夹爪的电机角度是负的)
    gl = gripper_left.angle() - LEFT_DOWN
    gr = RIGHT_DOWN - gripper_right.angle()
    return "gl=%d gr=%d" % (gl, gr)


def parse(parts):
    """校验命令, 返回 (错误原因或 None, 数值或 None)。"""
    action = parts[0]
    if action not in ACTIONS:
        return "unknown", None
    kind = ACTIONS[action]
    if len(parts) > 2:
        return "bad_value", None
    if len(parts) == 1:
        if kind == "none" or kind.startswith("optional"):
            return None, None
        return "missing_value", None
    if kind == "none":
        return "bad_value", None
    try:
        value = int(parts[1])
    except ValueError:
        return "bad_value", None
    if value < 0 or (kind == "angle90" and value > 90):
        return "bad_value", None
    return None, value


class Task:
    """正在执行的动作。kind: drive | gripper | line | dc | instant"""

    def __init__(self, cmd, kind):
        self.cmd = cmd
        self.kind = kind
        self.edge_sign = 1
        self.start_distance = 0
        self.target_mm = 0


def start(action, value, cmd):
    """开始执行一个动作 (不等待它完成), 返回 Task。"""
    if action in (b"forward", b"backward"):
        sign = 1 if action == b"forward" else -1
        if value is None:
            left_motor.dc(sign * DC_POWER)
            right_motor.dc(sign * DC_POWER)
            return Task(cmd, "dc")
        robot.straight(sign * value * 10, wait=False)  # 前端单位是厘米, 车的单位是毫米
        return Task(cmd, "drive")

    if action in (b"left", b"right"):
        sign = 1 if action == b"left" else -1
        if value is None:
            left_motor.dc(sign * DC_POWER)
            right_motor.dc(-sign * DC_POWER)
            return Task(cmd, "dc")
        robot.turn(sign * value, wait=False)
        return Task(cmd, "drive")

    if action == b"stop":
        halt_drive()
        return Task(cmd, "instant")

    if action in (b"line_follow_left", b"line_follow_right"):
        task = Task(cmd, "line")
        # 左边缘：左侧白、右侧黑；右边缘相反
        task.edge_sign = 1 if action == b"line_follow_left" else -1
        task.start_distance = robot.distance()
        task.target_mm = value * 10
        return task

    # 夹爪
    left_target = None
    right_target = None
    if action == b"gripper_up":
        left_target, right_target = LEFT_UP, RIGHT_UP
    elif action == b"gripper_down":
        left_target, right_target = LEFT_DOWN, RIGHT_DOWN
    elif action == b"gripper_pos":
        left_target, right_target = value, -value
    elif action == b"gripper_left_up":
        left_target = LEFT_UP
    elif action == b"gripper_left_down":
        left_target = LEFT_DOWN
    elif action == b"gripper_left_pos":
        left_target = value
    elif action == b"gripper_right_up":
        right_target = RIGHT_UP
    elif action == b"gripper_right_down":
        right_target = RIGHT_DOWN
    elif action == b"gripper_right_pos":
        right_target = -value

    if left_target is not None:
        gripper_left.run_target(GRIPPER_SPEED, left_target, wait=False)
    if right_target is not None:
        gripper_right.run_target(GRIPPER_SPEED, right_target, wait=False)
    return Task(cmd, "gripper")


def follow_line_step(task):
    """巡线走一步, 走够距离返回 True。"""
    if abs(robot.distance() - task.start_distance) >= task.target_mm:
        robot.stop()
        return True
    error = color_sensor.reflection() - LINE_TARGET
    turn_rate = task.edge_sign * LINE_KP * error
    turn_rate = max(-MAX_TURN_RATE, min(MAX_TURN_RATE, turn_rate))
    robot.drive(LINE_SPEED, turn_rate)
    return False


def is_done(task):
    if task.kind == "drive":
        return robot.done()
    if task.kind == "gripper":
        return gripper_left.done() and gripper_right.done()
    if task.kind == "line":
        return follow_line_step(task)
    if task.kind == "dc":
        return False  # 持续运动: 直到收到 stop 或心跳超时
    return True


def halt_drive():
    robot.stop()
    left_motor.stop()
    right_motor.stop()


def halt(task):
    if task.kind == "gripper":
        gripper_left.hold()
        gripper_right.hold()
    else:
        halt_drive()


task = None
last_rx = clock.time()
send("rdy")

while True:
    if keyboard.poll(0):
        raw = stdin.buffer.readline().strip()
        last_rx = clock.time()
        parts = raw.split()
        cmd = raw.decode()
        action = parts[0] if parts else b""

        if action == b"" or action == b"hb":
            pass
        elif task is not None:
            # 忙碌: 只接受 stop
            if action == b"stop":
                halt(task)
                send("STOPPED %s %s" % (task.cmd, gripper_state()))
                task = None
                send("rdy")
            else:
                send("ERR %s busy" % cmd)
        elif action == b"bye":
            break
        elif action == b"version":
            send("VERSION %d" % PROTOCOL_VERSION)
            send("rdy")
        else:
            error, value = parse(parts)
            if error is not None:
                send("ERR %s %s" % (cmd, error))
                send("rdy")
            else:
                task = start(action, value, cmd)
                send("ACK %s" % cmd)

    if task is not None:
        if task.kind == "dc" and clock.time() - last_rx > WATCHDOG_MS:
            halt(task)
            send("STOPPED %s %s reason=watchdog" % (task.cmd, gripper_state()))
            task = None
            send("rdy")
        elif is_done(task):
            send("DONE %s %s" % (task.cmd, gripper_state()))
            task = None
            send("rdy")

    wait(LOOP_MS)
