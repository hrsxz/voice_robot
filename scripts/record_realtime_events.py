"""
用合成录音连接 OpenAI 实时接口 (realtime 会话 + 函数调用), 打印事件时间线,
并把事件录成 JSONL, 作为 tests/fixtures/realtime/ 下的测试夹具。

只记录事件类型和对测试有用的字段 (不记录音频; 回复的音频本来也没有, 只要文字)。

用法 (需要 OPENAI_API_KEY):
    python -m scripts.record_realtime_events                       # 默认几条录音
    python -m scripts.record_realtime_events l3_square --model gpt-realtime-mini
    python -m scripts.record_realtime_events --save                # 同时保存夹具
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import ssl
import time
from pathlib import Path
from typing import Any

import soundfile as sf
import truststore
from dotenv import load_dotenv

from pc import constants
from pc.llm.audio_utils import STT_MODEL, STT_PROMPT
from pc.llm.realtime_tools import INSTRUCTIONS, build_session_tools

load_dotenv(constants.project_root_path / ".env")

AUDIO_DIR = constants.project_root_path / "tests" / "fixtures" / "audio"
FIXTURE_DIR = constants.project_root_path / "tests" / "fixtures" / "realtime"
DEFAULT_FILES = ["l1_forward_30", "l2_forward_right", "l3_square", "l1_stop"]
KEEP_FIELDS = ("type", "item_id", "call_id", "name", "arguments", "delta", "transcript",
               "response_id", "output_index", "audio_end_ms", "audio_start_ms")


def slim(event: Any) -> dict:
    data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
    out = {k: data[k] for k in KEEP_FIELDS if k in data and data[k] is not None}
    if data.get("type") in ("response.created", "response.done"):
        resp = data.get("response") or {}
        out["response"] = {"id": resp.get("id"), "status": resp.get("status")}
        if data["type"] == "response.done":
            out["response"]["output"] = [
                {k: o.get(k) for k in ("type", "name", "arguments", "call_id")
                 if o.get(k) is not None}
                | ({"text": "".join(c.get("text") or c.get("transcript") or ""
                                    for c in o.get("content") or [])}
                   if o.get("type") == "message" else {})
                for o in resp.get("output") or []
            ]
            out["response"]["usage"] = resp.get("usage")
    if data.get("type") == "error":
        out["error"] = data.get("error")
    return out


async def run_one(path: Path, model: str, save: bool) -> None:
    from openai import AsyncOpenAI

    audio, sr = sf.read(str(path), dtype="int16")
    assert sr == 24000, sr
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    events: list[dict] = []
    t0 = time.perf_counter()
    async with client.realtime.connect(
        model=model, websocket_connection_options={"ssl": ctx}
    ) as conn:
        t_conn = time.perf_counter()
        await conn.session.update(session={
            "type": "realtime",
            "output_modalities": ["text"],
            "instructions": INSTRUCTIONS,
            "tools": build_session_tools(),
            "tool_choice": "auto",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": STT_MODEL, "language": "zh", "prompt": STT_PROMPT},
                "turn_detection": None,
            }},
        })
        chunk = 960
        for i in range(0, len(audio), chunk):
            await conn.input_audio_buffer.append(
                audio=base64.b64encode(audio[i:i + chunk].tobytes()).decode())
        await conn.input_audio_buffer.commit()
        await conn.response.create()
        t_commit = time.perf_counter()
        print(f"\n=== {path.name} ({model})  连接 {1000 * (t_conn - t0):.0f} ms")
        done_resp = False
        done_tr = False
        calls = 0
        while not (done_resp and done_tr):
            try:
                event = await asyncio.wait_for(conn.recv(), 15)
            except TimeoutError:
                print("  超时")
                break
            ms = 1000 * (time.perf_counter() - t_commit)
            e = slim(event)
            e["t_ms"] = round(ms)
            events.append(e)
            et = e["type"]
            if et.endswith("delta") and "transcription" not in et:
                continue
            detail = {k: v for k, v in e.items() if k not in ("type", "t_ms", "event_id")}
            print(f"  {ms:6.0f} ms  {et}  {json.dumps(detail, ensure_ascii=False)[:200]}")
            if et == "response.done":
                fcs = [o for o in e["response"]["output"] if o.get("type") == "function_call"]
                if fcs and calls < 20:
                    # 回函数结果, 让模型继续给出后面的动作
                    for fc in fcs:
                        calls += 1
                        await conn.conversation.item.create(item={
                            "type": "function_call_output", "call_id": fc["call_id"],
                            "output": "已入队"})
                    await conn.response.create()
                else:
                    done_resp = True
            if et.endswith("input_audio_transcription.completed") or et == "error":
                done_tr = True
    if save:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        out = FIXTURE_DIR / f"{path.stem}.jsonl"
        out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                       encoding="utf-8")
        print(f"  已保存 {out}")


async def main_async(files: list[str], model: str, save: bool) -> None:
    for f in files:
        p = Path(f)
        if not p.exists():
            p = AUDIO_DIR / f"{f}.wav"
        await run_one(p, model, save)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("files", nargs="*", default=DEFAULT_FILES)
    parser.add_argument("--model", default="gpt-realtime")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args.files, args.model, args.save))


if __name__ == "__main__":
    main()
