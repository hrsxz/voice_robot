"""
Spike: OpenAI 实时转写能否在"按住空格说话"期间就给出分段结果, 以及松开后多久拿到最后一段文字。

按实时速度推送 24 kHz 测试录音 (相当于用户在说话), 音频推完后立即 commit (相当于松开空格),
记录每个事件相对"音频结束"的时间。负数表示在说完之前就收到了。

用法 (需要 OPENAI_API_KEY 和 tests/fixtures/audio/ 下的录音):
    python -m scripts.spike_realtime_transcription
"""

from __future__ import annotations

import asyncio
import base64
import ssl
import statistics
import time
from dataclasses import dataclass, field

import soundfile as sf
import truststore
from dotenv import load_dotenv
from openai import AsyncOpenAI

from pc import constants

AUDIO_DIR = constants.project_root_path / "tests" / "fixtures" / "audio"
FILES = ["l2_forward_right", "l2_backward_gripper", "l2_grippers", "l3_square", "mixed_dance"]
CHUNK_S = 0.04
IDLE_TIMEOUT_S = 4.0

PROMPT = (
    "请用简体中文输出。机器人控制命令：向前走，往后退，向左转，向右转，停止，"
    "把夹子抬起来，把夹子放下来，左夹子，右夹子，厘米，度，然后，再，一共做四次。"
)

CONFIGS = {
    "A1b gpt-4o-transcribe + prompt + server_vad 300ms": {
        "transcription": {"model": "gpt-4o-transcribe", "language": "zh", "prompt": PROMPT},
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 300,
                           "prefix_padding_ms": 300},
    },
    "A2b gpt-realtime-whisper(delay=low) 无 VAD, 松开时手动提交": {
        "transcription": {"model": "gpt-realtime-whisper", "language": "zh", "delay": "low"},
        "turn_detection": None,
    },
}


@dataclass
class Run:
    name: str
    audio_s: float
    # (相对音频结束的秒, 类型, 文本)
    events: list[tuple[float, str, str]] = field(default_factory=list)
    error: str | None = None

    def completed(self) -> list[tuple[float, str]]:
        return [(t, txt) for t, typ, txt in self.events if typ.endswith("transcription.completed")]

    def deltas(self) -> list[float]:
        return [t for t, typ, _ in self.events if typ.endswith("transcription.delta")]


async def run_one(client: AsyncOpenAI, name: str, cfg: dict, ssl_ctx: ssl.SSLContext) -> Run:
    audio, sr = sf.read(AUDIO_DIR / f"{name}.wav", dtype="int16")
    assert sr == 24000
    run = Run(name=name, audio_s=len(audio) / sr)
    chunk = int(CHUNK_S * sr)
    audio_end: float | None = None

    session = {
        "type": "transcription",
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, **cfg}},
    }

    async with client.realtime.connect(
        extra_query={"intent": "transcription"},
        websocket_connection_options={"ssl": ssl_ctx},
    ) as conn:
        await conn.session.update(session=session)  # type: ignore[arg-type]

        async def sender() -> None:
            nonlocal audio_end
            start = time.perf_counter()
            for i in range(0, len(audio), chunk):
                await conn.input_audio_buffer.append(
                    audio=base64.b64encode(audio[i:i + chunk].tobytes()).decode()
                )
                await asyncio.sleep(max(0.0, start + (i + chunk) / sr - time.perf_counter()))
            audio_end = time.perf_counter()
            await conn.input_audio_buffer.commit()  # 相当于松开空格

        send_task = asyncio.create_task(sender())
        stamps: list[tuple[float, str, str]] = []
        try:
            while True:
                try:
                    event = await asyncio.wait_for(conn.recv(), IDLE_TIMEOUT_S)
                except TimeoutError:
                    if send_task.done():
                        break
                    continue
                text = (getattr(event, "transcript", None) or getattr(event, "delta", None)
                        or "")
                if event.type == "error":
                    text = str(getattr(event, "error", event))
                stamps.append((time.perf_counter(), event.type, str(text)))
        finally:
            send_task.cancel()

    end = audio_end or time.perf_counter()
    run.events = [(t - end, typ, txt) for t, typ, txt in stamps]
    errors = [txt for _, typ, txt in run.events if typ == "error"]
    if errors:
        run.error = errors[0][:200]
    return run


async def main() -> None:
    load_dotenv(constants.project_root_path / ".env")
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = AsyncOpenAI()

    for cfg_name, cfg in CONFIGS.items():
        print(f"\n===== {cfg_name}")
        tails: list[float] = []
        early_files = 0
        for name in FILES:
            run = await run_one(client, name, cfg, ssl_ctx)
            done = run.completed()
            error = f"  ERROR: {run.error}" if run.error else ""
            print(f"-- {name} ({run.audio_s:.1f}s){error}")
            for t, txt in done:
                print(f"   {t:+6.2f}s  {txt}")
            if run.deltas():
                print(f"   第一个 delta {min(run.deltas()):+.2f}s, 共 {len(run.deltas())} 个 delta")
            if done:
                tails.append(done[-1][0])
                if done[0][0] < 0:
                    early_files += 1
        if tails:
            print(f"== 说完前就拿到分段结果: {early_files}/{len(FILES)};  "
                  f"松开→最后一段文字 中位数 {statistics.median(tails):+.2f}s, "
                  f"最大 {max(tails):+.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
