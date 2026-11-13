"""
回放测试工具: 用录音文件代替麦克风, 按实时速度走完整条流式链路
(流式识别 -> 分句路由 / LLM -> 执行队列 -> simulate Hub), 不需要真人说话, 也不需要实车。

用法 (需要 OPENAI_API_KEY):
    python -m scripts.make_test_audio                     # 先生成合成测试录音
    python -m scripts.replay_audio                        # 回放 tests/fixtures/audio/ 下全部录音
    python -m scripts.replay_audio tests/fixtures/audio/l2_forward_right.wav
    python -m scripts.replay_audio --model gpt-6-luna --log logs/replay.jsonl

对已知的测试录音会核对命令序列是否正确; 其他录音只打印结果。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from pathlib import Path

from pc import constants
from pc.speech.audio_sources import WavFileSource
from pc.speech.streaming_stt import wait_background_closes
from pc.voice_to_robot import VoiceController

AUDIO_DIR = constants.project_root_path / "tests" / "fixtures" / "audio"

# 测试录音 (scripts/make_test_audio.py) 对应的正确命令序列;
# None 表示后半句交给 LLM, 只核对前面快速通道的部分
EXPECTED: dict[str, list[str] | tuple[list[str], None]] = {
    "l1_forward_30": ["forward 30"],
    "l1_backward_20": ["backward 20"],
    "l1_left_90": ["left 90"],
    "l1_right_45": ["right 45"],
    "l1_gripper_up": ["gripper_up"],
    "l1_gripper_down": ["gripper_down"],
    "l1_left_gripper_up": ["gripper_left_up"],
    "l1_right_gripper_down": ["gripper_right_down"],
    "l1_stop": ["stop"],
    "l2_forward_right": ["forward 50", "right 90"],
    "l2_backward_gripper": ["backward 10", "gripper_up"],
    "l2_left_forward": ["left 180", "forward 20"],
    "l2_grippers": ["gripper_left_up", "gripper_right_down"],
    "l3_gift": ["gripper_down", "forward 20", "gripper_up", "backward 20"],
    "l3_wave": ["gripper_left_up", "gripper_left_down", "gripper_right_up", "gripper_right_down"],
    "l3_dance": ["left 45", "right 90", "left 45", "gripper_up"],
    "l3_square": ["forward 30", "left 90"] * 4,
    "mixed_dance": (["forward 30"], None),
}


def check(name: str, commands: list[str]) -> str:
    expected = EXPECTED.get(name)
    if expected is None:
        return "—"
    if isinstance(expected, tuple):
        prefix = expected[0]
        return "OK" if commands[:len(prefix)] == prefix else f"不对，应以 {prefix} 开头"
    if name == "l3_dance" and commands == ["left 45", "right 90", "left 45",
                                           "gripper_left_up", "gripper_right_up"]:
        return "OK"
    return "OK" if commands == expected else f"不对，应为 {expected}"


async def replay(paths: list[Path], model: str | None, log: str | None, verbose: bool) -> int:
    vc = VoiceController(spike_simulation=True, latency_log_path=log)
    await vc.robot_agent.connect()
    bad = 0
    try:
        for path in paths:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf if not verbose else sys.stdout):
                batch = await vc.process_stream(WavFileSource(str(path)), llm_model=model)
                await batch.wait()
                await vc._drain_reports()
            commands = batch.executed
            stop_sent = "急停" in buf.getvalue() or batch.timer.route == "stop"
            if stop_sent and not commands:
                commands = ["stop"]
            verdict = check(path.stem, commands)
            bad += verdict not in ("OK", "—")
            lines = buf.getvalue().splitlines()
            summary = next((ln for ln in lines if ln.startswith("[耗时]")), "")
            print(f"[{verdict}] {path.name}: {commands}")
            if summary:
                print(f"       {summary}")
    finally:
        await wait_background_closes()
        await vc.robot_agent.disconnect()
    print(f"\n核对不通过: {bad}")
    return 1 if bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("files", nargs="*", help="WAV 文件 (24 kHz)，默认回放全部测试录音")
    parser.add_argument("--model", default="gpt-6-luna", help="后半句交给 LLM 时使用的模型")
    parser.add_argument("--log", default=None, help="耗时日志 JSONL 文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示完整的处理过程")
    args = parser.parse_args()

    paths = [Path(f) for f in args.files] or sorted(AUDIO_DIR.glob("*.wav"))
    if not paths:
        print("没有录音，先运行: python -m scripts.make_test_audio")
        sys.exit(2)
    sys.exit(asyncio.run(replay(paths, args.model, args.log, args.verbose)))


if __name__ == "__main__":
    main()
