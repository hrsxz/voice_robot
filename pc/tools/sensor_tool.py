import random


async def execute(args: dict) -> dict:
    name = (args.get("name") or "distance").strip().lower()
    dry_run = bool(args.get("dry_run", True))

    if name == "distance":
        if dry_run:
            value = random.randint(20, 120)
            return {"status": "ok", "value": value, "unit": "cm", "detail": "simulated distance"}
        return {"status": "error", "detail": "real distance sensor not wired yet"}

    if name in ("color", "gyro"):
        return {"status": "error", "detail": f"{name} sensor not implemented yet"}

    return {"status": "error", "detail": f"unsupported sensor: {name}"}
