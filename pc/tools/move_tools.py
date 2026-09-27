from typing import Any


async def execute(args: dict) -> dict:
    hub: Any = args.get("hub")
    action = (args.get("action") or "").strip().lower()
    value = args.get("value")

    if hub is None or not hasattr(hub, "send"):
        return {"status": "error", "detail": "hub is required and must support send()"}

    if not action:
        return {"status": "error", "detail": "missing action"}

    no_value_actions = {"stop", "gripper_up", "gripper_down"}
    int_value_actions = {
        "forward", "backward", "straightforward", "straightbackward",
        "left", "right", "face_to", "gripper_pos"
    }

    if action in no_value_actions:
        if value is not None:
            return {"status": "error", "detail": f"{action} does not accept value"}
        cmd = action
    elif action in int_value_actions:
        if value is None:
            return {"status": "error", "detail": f"{action} requires value"}
        try:
            ivalue = int(float(value))
        except Exception:
            return {"status": "error", "detail": f"{action} value must be integer"}
        cmd = f"{action} {ivalue}"
    else:
        return {"status": "error", "detail": f"unsupported move action: {action}"}

    try:
        await hub.send(cmd)
        return {"status": "ok", "detail": cmd}
    except Exception as exc:
        return {"status": "error", "detail": f"hub send failed: {exc}"}
