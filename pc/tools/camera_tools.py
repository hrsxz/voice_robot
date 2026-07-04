from datetime import datetime
from pathlib import Path


async def execute(args: dict) -> dict:
    mode = (args.get("mode") or "photo").strip().lower()
    dry_run = bool(args.get("dry_run", True))

    if mode not in ("photo", "video"):
        return {"status": "error", "detail": f"unsupported mode: {mode}"}

    if mode == "video":
        return {"status": "error", "detail": "video is not implemented yet"}

    if dry_run:
        fake_path = Path("captures") / f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        return {"status": "ok", "path": str(fake_path), "detail": "simulated photo"}

    try:
        import cv2

        Path("captures").mkdir(parents=True, exist_ok=True)
        out_path = Path("captures") / f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return {"status": "error", "detail": "camera is not available"}

        ok, frame = cap.read()
        cap.release()

        if not ok:
            return {"status": "error", "detail": "failed to capture frame"}

        cv2.imwrite(str(out_path), frame)
        return {"status": "ok", "path": str(out_path), "detail": "photo captured"}
    except Exception as exc:
        return {"status": "error", "detail": f"camera error: {exc}"}
