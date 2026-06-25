import json
import re


def parse_intent(llm_text: str) -> dict:
    try:
        obj = json.loads(_extract_json_text(llm_text))
        if isinstance(obj, dict):
            # Generated JSON format example:
            # {"steps":[
            #     {"action":"forward","params":{"distance_cm":30,"angle_deg":null}},
            #     {"action":"turn_left","params":{"distance_cm":null,"angle_deg":90}},
            #     {"action":"gripper_down","params":{"distance_cm":null,"angle_deg":null}}
            # ]}
            steps = obj.get("steps")
            if isinstance(steps, list):
                normalized_steps: list[dict] = []
                for step in steps:
                    normalized = _normalize_step(step)
                    if normalized:
                        normalized_steps.append(normalized)
                return {"steps": normalized_steps}
            # normalized_steps: {
            #   'steps': [
            #       {'action': 'forward', 'params': {'distance_cm': 30, 'angle_deg': None}},
            #       {'action': 'left', 'params': {'distance_cm': None, 'angle_deg': 60}}
            #   ]
            # }
    except Exception:
        pass

    # fallback: compatible with non-JSON output, split by connectors
    text = (llm_text or "").strip().lower()
    steps = _fallback_steps_from_text(text)
    return {"steps": steps}


def _normalize_step(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None

    action = _normalize_action(str(obj.get("action", "")))
    if not action:
        return None

    params = obj.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    params_out: dict[str, int | None] = {
        "distance_cm": _to_int_or_none(params.get("distance_cm")),
        "angle_deg": _to_int_or_none(params.get("angle_deg")),
    }

    # Action parameter constraints
    if action in ("forward", "backward", "straightforward", "straightbackward"):
        params_out["angle_deg"] = None
    elif action in ("left", "right"):
        params_out["distance_cm"] = None
    elif action in ("gripper_up", "gripper_down"):
        params_out["distance_cm"] = None
        params_out["angle_deg"] = None
    elif action == "gripper_pos":
        params_out["distance_cm"] = None
        params_out["angle_deg"] = None
    elif action == "stop":
        params_out["distance_cm"] = None
        params_out["angle_deg"] = None

    return {"action": action, "params": params_out}


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


def _fallback_steps_from_text(text: str) -> list[dict]:
    if not text:
        return []

    # Split by connectors like "then", comma, semicolon, etc.
    clauses = re.split(r"\s*(?:然后|之后|再|and then|then|,|，|;|；)\s*", text)
    clauses = [c.strip() for c in clauses if c and c.strip()]

    steps: list[dict] = []
    for clause in clauses:
        action: str | None = None
        if any(k in clause for k in ("停", "停止", "stop")):
            action = "stop"
        elif any(k in clause for k in ("gripper_up", "gripper up")):
            action = "gripper_up"
        elif any(k in clause for k in ("gripper_down", "gripper down")):
            action = "gripper_down"
        elif any(k in clause for k in ("gripper_pos", "gripper pos")):
            action = "gripper_pos"
        elif any(k in clause for k in ("前", "forward", "ahead")):
            action = "forward"
        elif any(k in clause for k in ("后", "后退", "backward")):
            action = "backward"
        elif any(k in clause for k in ("左", "left")):
            action = "left"
        elif any(k in clause for k in ("右", "right")):
            action = "right"

        if not action:
            continue

        p = _extract_params(clause)
        if action in ("forward", "backward"):
            step = {"action": action, "params": {"distance_cm": p.get("distance_cm")}}
        elif action in ("left", "right"):
            step = {"action": action, "params": {"angle_deg": p.get("angle_deg")}}
        elif action in ("gripper_up", "gripper_down", "gripper_pos"):
            step = {"action": action, "params": {}}
        else:
            step = {"action": "stop", "params": {}}

        steps.append(step)

    return steps


def _normalize_action(action: str) -> str | None:
    value = action.strip().lower()
    if value in ("forward", "f", "go forward", "ahead"):
        return "forward"
    if value in ("backward", "b", "back"):
        return "backward"
    if value in ("straightforward",):
        return "straightforward"
    if value in ("straightbackward",):
        return "straightbackward"
    if value in ("left", "l", "left turn", "turn_left"):
        return "left"
    if value in ("right", "r", "right turn", "turn_right"):
        return "right"
    if value in ("face_to", "face", "look at"):
        return "face_to"
    if value in ("stop", "hold"):
        return "stop"
    if value == "gripper up":
        return "gripper_up"
    if value == "gripper down":
        return "gripper_down"
    if value == "gripper pos":
        return "gripper_pos"
    if value in ("gripper_up", "gripper_down", "gripper_pos"):
        return value
    return None


def _extract_params(text: str) -> dict:
    params: dict = {}
    match = re.search(r"(\d+(\.\d+)?)\s*(cm|m|米|deg|°|度)?", text)
    if not match:
        return params

    value = float(match.group(1))
    unit = (match.group(3) or "").lower()
    if unit == "cm":
        params["distance_cm"] = int(value)
    elif unit in ("m", "米"):
        params["distance_cm"] = int(value * 100)
    elif unit in ("deg", "度", "°"):
        params["angle_deg"] = int(value)
    else:
        params["distance_cm"] = int(value)
    return params


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
