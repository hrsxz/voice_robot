"""
对比工具: 同一批录音分别跑 stream 链路和 realtime 链路 (Hub 用 simulate), 输出每条录音的
识别结果 (动作序列) 是否正确, 以及延迟的中位数 (改动 voice-realtime-pipeline, 设计 D8)。

录音按实时速度推送 (相当于按住空格说完再松开), 两条链路的"说完"时刻定义相同:
最后一块音频送出、松开空格的时刻。

延迟指标:
- 首个动作: 说完 -> 第一个动作放进执行队列 (stream 命中快速通道时可能是负数: 说完之前就执行了)
- 全部动作: 说完 -> 这句话的最后一个动作确定 (stream: 路由完成; realtime: 模型不再调用工具)

用法 (需要 OPENAI_API_KEY, 会产生 API 费用, 约每条录音几美分以内):
    python -m scripts.make_test_audio                      # 先生成合成测试录音
    python -m scripts.compare_pipelines                    # 全部测试录音, 每条跑一次
    python -m scripts.compare_pipelines --rounds 3 --realtime-model gpt-realtime
    python -m scripts.compare_pipelines tests/fixtures/audio/l1_forward_30.wav --only realtime
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from pc.speech.audio_sources import WavFileSource
from pc.speech.realtime_session import DEFAULT_MODEL as REALTIME_MODEL
from pc.speech.streaming_stt import wait_background_closes
from pc.voice_to_robot import VoiceController
from scripts.replay_audio import AUDIO_DIR, check


@dataclass
class Result:
    pipeline: str
    name: str
    commands: list[str]
    verdict: str
    first_ms: int | None
    all_ms: int | None
    text: str
    error: str | None = None


def _ms(timer, stage: str) -> int | None:
    start = timer.marks.get("recording_end")
    end = timer.marks.get(stage)
    if start is None or end is None:
        return None
    return round((end - start) * 1000)


async def run_stream(vc: VoiceController, path: Path, model: str | None) -> Result:
    batch = await vc.process_stream(WavFileSource(str(path)), llm_model=model)
    timer = batch.timer
    await batch.wait()
    await vc._drain_reports()
    commands = batch.executed or (["stop"] if timer.route == "stop" else [])
    # 全部动作确定的时刻: 交给 LLM 的打了 route_done; 只走快速通道的, 说完 (松开) 时就确定了
    all_ms = _ms(timer, "route_done")
    if all_ms is None and timer.route in ("fast_path", "stop"):
        all_ms = max(0, _ms(timer, "first_enqueued") or 0)
    first = _ms(timer, "first_enqueued") if timer.route != "stop" else _ms(timer, "route_done")
    return Result("stream", path.stem, commands, check(path.stem, commands),
                  first, all_ms, timer.text, timer.error)


async def run_realtime(vc: VoiceController, path: Path) -> Result:
    turn = await vc.process_realtime(WavFileSource(str(path)))
    if turn is None:
        return Result("realtime", path.stem, [], "失败", None, None, "", "没有生成轮次")
    await asyncio.wait_for(turn.wait(), 30)
    await turn.batch.wait()
    await asyncio.sleep(0.3)  # 等转写结果 (只用于显示)
    await vc._drain_reports()
    timer = turn.timer
    commands = turn.batch.executed or (["stop"] if timer.route == "stop" else [])
    first = _ms(timer, "first_enqueued") if timer.route != "stop" else _ms(timer, "route_done")
    return Result("realtime", path.stem, commands, check(path.stem, commands),
                  first, _ms(timer, "route_done"), timer.text, timer.error)


def median(values: list[int | None]) -> str:
    vals = [v for v in values if v is not None]
    return f"{statistics.median(vals):.0f}" if vals else "-"


async def compare(paths: list[Path], rounds: int, only: str | None, llm_model: str,
                  rt_model: str, log: str | None, verbose: bool) -> list[Result]:
    results: list[Result] = []
    vc = VoiceController(spike_simulation=True, latency_log_path=log, realtime_model=rt_model)
    await vc.robot_agent.connect()
    if only != "stream":
        vc.pipeline = "realtime"
        vc.realtime = vc._make_realtime("ptt")
        await vc.realtime.open()
    try:
        for r in range(rounds):
            for path in paths:
                for pipeline in ("stream", "realtime"):
                    if only and pipeline != only:
                        continue
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(sys.stdout if verbose else buf):
                        if pipeline == "stream":
                            res = await run_stream(vc, path, llm_model)
                        else:
                            if vc.pipeline != "realtime":
                                print(buf.getvalue())
                                raise RuntimeError("realtime 链路已失败，停止对比")
                            res = await run_realtime(vc, path)
                    results.append(res)
                    print(f"[{r + 1}] {pipeline:8s} [{res.verdict}] {path.stem}: {res.commands}  "
                          f"首个 {res.first_ms} ms / 全部 {res.all_ms} ms  「{res.text}」")
    finally:
        if vc.realtime is not None:
            await vc.realtime.close()
        await wait_background_closes()
        await vc.robot_agent.disconnect()
    return results


def summarize(results: list[Result]) -> str:
    lines = ["| 链路 | 条数 | 正确 | 首个动作中位数 (ms) | 全部动作中位数 (ms) |",
             "|---|---|---|---|---|"]
    for pipeline in ("stream", "realtime"):
        rs = [r for r in results if r.pipeline == pipeline]
        if not rs:
            continue
        checked = [r for r in rs if r.verdict != "—"]
        ok = sum(r.verdict == "OK" for r in checked)
        lines.append(f"| {pipeline} | {len(rs)} | {ok}/{len(checked)} | "
                     f"{median([r.first_ms for r in rs])} | {median([r.all_ms for r in rs])} |")
    lines += ["", "按级别（首个动作中位数 ms：stream / realtime）：", ""]
    for level in ("l1", "l2", "l3", "mixed"):
        by = {p: [r for r in results if r.pipeline == p and r.name.startswith(level)]
              for p in ("stream", "realtime")}
        if any(by.values()):
            lines.append(f"- {level}: {median([r.first_ms for r in by['stream']])} / "
                         f"{median([r.first_ms for r in by['realtime']])}"
                         f"（全部动作 {median([r.all_ms for r in by['stream']])} / "
                         f"{median([r.all_ms for r in by['realtime']])}）")
    bad = [r for r in results if r.verdict not in ("OK", "—")]
    if bad:
        lines += ["", "不正确的结果：", ""]
        lines += [f"- {r.pipeline} {r.name}: {r.commands}（{r.verdict}）「{r.text}」" for r in bad]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("files", nargs="*", help="WAV 文件 (24 kHz)，默认全部测试录音")
    parser.add_argument("--rounds", type=int, default=1, help="每条录音跑几轮")
    parser.add_argument("--only", choices=("stream", "realtime"), default=None)
    parser.add_argument("--model", default="gpt-6-luna", help="stream 链路的 LLM 模型")
    parser.add_argument("--realtime-model", default=REALTIME_MODEL)
    parser.add_argument("--log", default=None, help="耗时日志 JSONL 文件")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    paths = [Path(f) for f in args.files] or sorted(AUDIO_DIR.glob("*.wav"))
    if not paths:
        print("没有录音，先运行: python -m scripts.make_test_audio")
        sys.exit(2)
    results = asyncio.run(compare(paths, args.rounds, args.only, args.model,
                                  args.realtime_model, args.log, args.verbose))
    print()
    print(summarize(results))


if __name__ == "__main__":
    main()
