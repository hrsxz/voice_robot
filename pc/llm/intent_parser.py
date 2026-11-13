import json


def parse_line(line: str) -> list[dict]:
    """
    解析 LLM 流式输出的一行 (NDJSON): {"action": "...", "args": {...}}。
    也兼容一行里是旧格式 {"steps": [...]} 的情况。
    动作名不认识的行原样返回 (由 RobotAgent 拒绝并记录原因), 不是 JSON 的行返回空列表。
    """
    s = (line or "").strip().rstrip(",")
    if not s.startswith("{"):
        return []
    try:
        obj = json.loads(s)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []

    steps = obj.get("steps")
    if isinstance(steps, list):
        return [n for n in (_normalize_step(step) for step in steps) if n]

    normalized = _normalize_step(obj)
    if normalized:
        return [normalized]
    action = obj.get("action")
    if isinstance(action, str) and action.strip():
        args = obj.get("args")
        return [{"action": action.strip(), "args": args if isinstance(args, dict) else {}}]
    return []


def parse_intent(llm_text: str) -> dict:
    """
    解析 LLM 的完整输出。支持两种格式:
    - 每行一个动作 (NDJSON, 现在的输出规范)
    - 旧格式: 一个 {"steps": [...]} 对象
    """
    try:
        obj = json.loads(_extract_json_text(llm_text))
        if isinstance(obj, dict):
            # Generated JSON format example:
            # '{"steps":[
            #   {"action":"forward","args":{"distance_cm":30}},
            #   {"action":"left","args":{"angle_deg":60}},
            #   {"action":"gripper_up","args":{}}
            # ]}'
            steps = obj.get("steps")
            if isinstance(steps, list):
                normalized_steps: list[dict] = []
                for step in steps:
                    normalized = _normalize_step(step)
                    if normalized:
                        normalized_steps.append(normalized)
                return {"steps": normalized_steps}
            # normalized_steps: {[
            #   {'action': 'forward', 'args': {'distance_cm': 30}}
            #   {'action': 'left', 'args': {'angle_deg': 60}}
            #   {'action': 'gripper_up', 'args': {}}
            # }
    except Exception:
        pass

    # 每行一个动作 (NDJSON)
    text = _strip_code_fence(llm_text)
    line_steps: list[dict] = []
    for line in text.splitlines():
        line_steps.extend(parse_line(line))

    # 不是合法 JSON 时不再按关键词猜动作, 返回空结果 (由调用方提示无法识别)
    return {"steps": line_steps}


def _strip_code_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = s.strip("`").splitlines()
        if lines and lines[0].lower().strip() in ("json", "jsonl", "ndjson", "javascript"):
            lines = lines[1:]
        s = "\n".join(lines)
    return s


def _normalize_step(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None

    action = _normalize_action(str(obj.get("action", "")))
    if not action:
        return None

    raw_args = obj.get("args")
    if isinstance(raw_args, dict):
        payload = raw_args
    else:
        payload = {}

    if action in ("forward", "backward", "line_follow_left", "line_follow_right",):
        distance = _to_int_or_none(payload.get("distance_cm"))
        return {"action": action, "args": {"distance_cm": distance}}

    if action in ("left", "right", "gripper_pos",
                  "gripper_left_pos", "gripper_right_pos",):
        angle = _to_int_or_none(payload.get("angle_deg"))
        return {"action": action, "args": {"angle_deg": angle}}

    if action in ("gripper_up", "gripper_down", "stop"):
        return {"action": action, "args": {}}

    if action == "camera":
        mode = _to_str_or_none(payload.get("mode"))
        if mode not in ("photo", "video"):
            mode = "photo"
        return {"action": action, "args": {"mode": mode}}

    if action == "sensor":
        name = _to_str_or_none(payload.get("name"))
        if name not in ("distance", "color", "gyro"):
            name = "distance"
        return {"action": action, "args": {"name": name}}

    if action in ("stop", "gripper_up", "gripper_down",
                  "gripper_left_up", "gripper_left_down",
                  "gripper_right_up", "gripper_right_down",):
        return {"action": action, "args": {}}

    return None


def _to_int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _to_str_or_none(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(value).strip().lower() or None
    except Exception:
        return None


def _normalize_action(action: str) -> str | None:
    value = action.strip().lower()

    aliases = {
        # 移动
        "forward": "forward",
        "f": "forward",
        "go forward": "forward",
        "ahead": "forward",

        "backward": "backward",
        "b": "backward",
        "back": "backward",

        # 旧动作名 (已从技能中删除), 作为别名保留
        "straightforward": "forward",
        "straightbackward": "backward",

        # 转向
        "left": "left",
        "l": "left",
        "left turn": "left",
        "turn_left": "left",

        "right": "right",
        "r": "right",
        "right turn": "right",
        "turn_right": "right",

        # 停止
        "stop": "stop",
        "hold": "stop",

        # 同时控制两个夹爪
        "gripper_up": "gripper_up",
        "gripper up": "gripper_up",
        "gripper_down": "gripper_down",
        "gripper down": "gripper_down",
        "gripper_pos": "gripper_pos",
        "gripper pos": "gripper_pos",

        # 左夹爪，B 口
        "gripper_left_up": "gripper_left_up",
        "gripper left up": "gripper_left_up",
        "left gripper up": "gripper_left_up",
        "左夹爪抬起": "gripper_left_up",
        "抬起左夹爪": "gripper_left_up",

        "gripper_left_down": "gripper_left_down",
        "gripper left down": "gripper_left_down",
        "left gripper down": "gripper_left_down",
        "左夹爪放下": "gripper_left_down",
        "放下左夹爪": "gripper_left_down",

        "gripper_left_pos": "gripper_left_pos",
        "gripper left pos": "gripper_left_pos",
        "left gripper pos": "gripper_left_pos",

        # 右夹爪，F 口
        "gripper_right_up": "gripper_right_up",
        "gripper right up": "gripper_right_up",
        "right gripper up": "gripper_right_up",
        "右夹爪抬起": "gripper_right_up",
        "抬起右夹爪": "gripper_right_up",

        "gripper_right_down": "gripper_right_down",
        "gripper right down": "gripper_right_down",
        "right gripper down": "gripper_right_down",
        "右夹爪放下": "gripper_right_down",
        "放下右夹爪": "gripper_right_down",

        "gripper_right_pos": "gripper_right_pos",
        "gripper right pos": "gripper_right_pos",
        "right gripper pos": "gripper_right_pos",

        # 巡线
        "line_follow_left": "line_follow_left",
        "line follow left": "line_follow_left",
        "沿左侧巡线": "line_follow_left",
        "沿黑线左边行走": "line_follow_left",

        "line_follow_right": "line_follow_right",
        "line follow right": "line_follow_right",
        "沿右侧巡线": "line_follow_right",
        "沿黑线右边行走": "line_follow_right",

        # 相机
        "camera": "camera",
        "photo": "camera",
        "take_photo": "camera",
        "take picture": "camera",
        "拍照": "camera",
        "照相": "camera",

        # 传感器
        "sensor": "sensor",
        "read_sensor": "sensor",
        "读取传感器": "sensor",
    }

    if value in aliases:
        return aliases[value]

    return None


def _extract_json_text(raw: str) -> str:
    # Guard empty input and strip surrounding whitespace
    s = (raw or "").strip()
    if not s:
        return "{}"

    # Compatible with markdown fenced blocks: ```json ... ```
    if s.startswith("```"):
        s = s.strip("`")
        lines = s.splitlines()
        if lines and lines[0].lower().strip() in ("json", "javascript"):
            lines = lines[1:]
        s = "\n".join(lines).strip()

    # Keep only first JSON object range to avoid noisy prefixes/suffixes
    left = s.find("{")
    right = s.rfind("}")
    if left != -1 and right != -1 and right > left:
        return s[left:right + 1]
    return s
