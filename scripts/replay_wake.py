"""
用录音回放测试唤醒词模式 (不需要真人说话, 也不需要小车)。

把几段 WAV 依次播放 (中间插入静音), 经过真实的 VAD、本地 tiny 模型、云端识别 (有 API 密钥时)
和模拟 Hub, 最后输出: 本地识别耗时、上传次数、执行的命令。

用法:
    python -m scripts.make_test_audio                     # 先生成合成录音
    python -m scripts.replay_wake                       # 回放 tests/fixtures/audio/wake/ 下全部
    python -m scripts.replay_wake chat_lunch chat_blue    # 只回放这几段 (文件名不带 .wav)
    python -m scripts.replay_wake --gap 25 wake_only l1_left_90 # 两段之间隔 25 秒 (测试窗口超时)
    python -m scripts.replay_wake --pipeline realtime --keep-window wake_only l1_forward_30

每段录音之前都会关闭对话窗口 (--keep-window 则保留), 这样每段都从"未唤醒"开始。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from pc import constants
from pc.agent.robot_agent import RobotAgent
from pc.speech.vad import SAMPLE_RATE
from pc.voice_to_robot import VoiceController

AUDIO_DIR = constants.project_root_path / "tests" / "fixtures" / "audio"
WAKE_DIR = AUDIO_DIR / "wake"


def find(name: str) -> Path:
    for path in (Path(name), WAKE_DIR / f"{name}.wav", AUDIO_DIR / f"{name}.wav"):
        if path.exists():
            return path
    raise FileNotFoundError(name)


class RecordingHub:
    """模拟 Hub: 立即完成, 只记录命令。"""
    simulate = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


async def frames(paths: list[Path], gap_s: float, vc: VoiceController, keep_window: bool):
    gap = np.zeros(int(gap_s * SAMPLE_RATE), dtype=np.int16).tobytes()
    chunk = SAMPLE_RATE * 30 // 1000 * 2
    for path in paths:
        audio, sr = sf.read(path, dtype="int16")
        if sr != SAMPLE_RATE:
            raise ValueError(f"{path}: 需要 {SAMPLE_RATE} Hz")
        if not keep_window:
            vc.conversation_gate.deactivate()
            if vc.wake_loop is not None:
                vc.wake_loop._window_open = False  # 不提示"超时"
        print(f"\n=== {path.name} ===")
        data = audio.tobytes() + gap
        for i in range(0, len(data), chunk):
            yield data[i:i + chunk]
            await asyncio.sleep(0.03)  # 实时速度: 和真实麦克风一样, 让识别和执行有时间运行


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("names", nargs="*", help="录音文件名 (不带 .wav) 或路径")
    parser.add_argument("--gap", type=float, default=2.0, help="两段录音之间的静音 (秒)")
    parser.add_argument("--keep-window", action="store_true", help="不在每段录音之前关闭对话窗口")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--end-silence-ms", type=int, default=700)
    parser.add_argument("--pipeline", choices=("stream", "realtime"), default="stream")
    parser.add_argument("--realtime-model", default=None)
    args = parser.parse_args()

    paths = [find(n) for n in args.names] or sorted(WAKE_DIR.glob("*.wav"))
    if not paths:
        sys.exit("没有录音，先运行 python -m scripts.make_test_audio")

    vc = VoiceController(spike_simulation=True, end_silence_ms=args.end_silence_ms)
    hub = RecordingHub()
    vc.spike = hub
    vc.robot_agent = RobotAgent(hub)
    vc.upload_guard.set_mode("wake", constants.project_root_path / "logs" / "uploads_replay.jsonl")
    vc.audio_client.start_tone = lambda kind: 0.3  # 回放时不出声, 但照样屏蔽 0.3 秒输入
    vc._llm_model = args.model
    if args.pipeline == "realtime":
        if args.realtime_model:
            vc.realtime_model = args.realtime_model
        vc.pipeline = "realtime"
        vc.realtime = vc._make_realtime("wake")

    await vc.run_wake(frames(paths, args.gap, vc, args.keep_window))
    await vc._drain_reports()

    loop = vc.wake_loop
    print("\n=== 汇总 ===")
    if loop and loop.local_ms:
        ms = sorted(loop.local_ms)
        print(f"本地识别 {len(ms)} 个片段：中位数 {statistics.median(ms):.0f} ms，"
              f"最大 {ms[-1]:.0f} ms")
    print(f"上传次数：{len(vc.upload_guard.records)}（被拦截 {vc.upload_guard.blocked} 次）")
    print(f"执行的命令：{hub.commands}")
    if args.pipeline == "realtime":
        print(f"最后使用的链路：{vc.pipeline}")


if __name__ == "__main__":
    asyncio.run(main())
