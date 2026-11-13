"""
在模拟器里用真实 LLM 运行任务代理 (改动 task-layer 任务 4.1)。不需要摄像头和小车。

用法 (需要 OPENAI_API_KEY):
    python -m scripts.sim_task "把黄色小人送回家"
    python -m scripts.sim_task "完成任务1" --show                 # 实时俯视图
    python -m scripts.sim_task "把红色的送到D区" --answer 红色服务机器人   # 自动回答反问
    python -m scripts.sim_task "把红色的送到D区"                   # 反问时在终端输入回答
    python -m scripts.sim_task --measure --runs 3                 # 4.1 的三条命令各跑几次, 输出表格

默认布局 wro2025_demo、规则 wro2025 (missions.yaml)、带 5% 运动误差。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

from pc.task.agent import DEFAULT_TASK_MODEL, STATUS_LABELS, OpenAITaskLLM, TaskResult
from pc.task.runtime import TaskRuntime

MEASURE_GOALS = {
    "完成任务1": ["yellow_person", "green_person", "red_person"],
    "把服务机器人都送到圆圈上": ["green_robot", "red_robot", "blue_robot", "yellow_robot"],
    "把红色的送到D区": ["red_robot"],  # 反问时回答"红色服务机器人"
}
MEASURE_ANSWER = "红色服务机器人"


class Wall:
    """LLM 的真实耗时 (模拟时间不包括它)。"""

    def __init__(self, llm: OpenAITaskLLM) -> None:
        self.llm = llm
        self.seconds = 0.0

    async def complete(self, messages, tools):
        t0 = time.perf_counter()
        try:
            return await self.llm.complete(messages, tools)
        finally:
            self.seconds += time.perf_counter() - t0


def make_ask(answers: list[str], interactive: bool):
    queue = list(answers)

    async def ask(question: str, options: list[str]) -> str | None:
        if queue:
            a = queue.pop(0)
            print(f"  （自动回答）{a}")
            return a
        if not interactive:
            return None
        return (await asyncio.to_thread(input, "回答> ")).strip() or None

    return ask


async def run_goal(goal: str, args, seed: int, answers: list[str], interactive: bool,
                   quiet: bool = False) -> tuple[TaskResult, TaskRuntime, float]:
    llm = Wall(OpenAITaskLLM(args.model))
    progress = (lambda _m: None) if quiet else (lambda m: print(f"[任务] {m}", flush=True))
    rt = TaskRuntime.simulated(llm, layout=args.layout, seed=seed, noise=not args.no_noise,
                               ruleset=args.rules, on_progress=progress, show=args.show,
                               time_scale=args.speed if args.show else 0.0)
    try:
        result = await rt.run(goal, ask=make_ask(answers, interactive))
    finally:
        await llm.llm.aclose()
    return result, rt, llm.seconds


def print_result(result: TaskResult, llm_s: float) -> None:
    print(f"\n结果：{STATUS_LABELS.get(result.status, result.status)}：{result.message}")
    print(f"技能调用 {result.skill_calls} 次，LLM {result.llm_rounds} 轮（{llm_s:.0f} s），"
          f"模拟用时 {result.elapsed_s:.0f} s")
    for c in result.calls:
        args = ", ".join(f"{k}={v}" for k, v in (c.args or {}).items())
        extra = f"  {c.reason}" if c.reason and c.status != "ok" else ""
        print(f"  {c.name}({args}) -> {c.status}{extra}")
    if result.questions:
        print("反问：" + " | ".join(result.questions))


async def measure(args) -> int:
    rows = []
    for goal, expected in MEASURE_GOALS.items():
        for i in range(args.runs):
            seed = args.seed + i
            result, rt, llm_s = await run_goal(goal, args, seed, [MEASURE_ANSWER], False,
                                               quiet=True)
            world = rt.skills.world
            delivered = [o for o in expected if world.object_state(o) == "delivered"]
            asked = bool(result.questions)
            others = sorted({(c.args or {}).get("object") for c in result.calls
                             if (c.args or {}).get("object") not in (None, *expected)})
            rows.append((goal, seed, result, delivered, expected, asked, llm_s, others))
            print(f"{goal} 种子 {seed}: {STATUS_LABELS[result.status]}，送达 "
                  f"{len(delivered)}/{len(expected)}，技能 {result.skill_calls}，"
                  f"LLM {result.llm_rounds} 轮 {llm_s:.0f} s，模拟 {result.elapsed_s:.0f} s，"
                  f"反问 {'是' if asked else '否'}"
                  + (f"，动了别的物体 {others}" if others else ""), flush=True)
            if result.status != "done" or len(delivered) < len(expected):
                print("   " + result.message)

    print(f"\n模型 {args.model}，布局 {args.layout}，误差 {'无' if args.no_noise else '5%'}\n")
    print("| 命令 | 次数 | 送达 | 技能调用（中位数） | LLM 轮数（中位数） | LLM 耗时（中位数） "
          "| 模拟用时（中位数） | 反问 |")
    print("|---|---|---|---|---|---|---|---|")
    for goal in MEASURE_GOALS:
        rs = [r for r in rows if r[0] == goal]
        d = sum(len(r[3]) for r in rs)
        t = sum(len(r[4]) for r in rs)
        print(f"| {goal} | {len(rs)} | {d}/{t} | "
              f"{statistics.median(r[2].skill_calls for r in rs):.0f} | "
              f"{statistics.median(r[2].llm_rounds for r in rs):.0f} | "
              f"{statistics.median(r[6] for r in rs):.0f} s | "
              f"{statistics.median(r[2].elapsed_s for r in rs):.0f} s | "
              f"{sum(r[5] for r in rs)}/{len(rs)} |")
    return 0


async def main_async(args) -> int:
    if args.measure:
        return await measure(args)
    if not args.goal:
        print("请给出目标，例如：python -m scripts.sim_task \"把黄色小人送回家\"")
        return 2
    result, rt, llm_s = await run_goal(args.goal, args, args.seed, args.answer, True)
    print_result(result, llm_s)
    if args.save and rt.sim is not None:
        from pc.sim.viewer import save
        print(f"俯视图：{save(rt.sim, args.save)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m scripts.sim_task", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("goal", nargs="?", help="目标（一句话）")
    p.add_argument("--model", default=DEFAULT_TASK_MODEL)
    p.add_argument("--layout", default="wro2025_demo", help="布局名或 random")
    p.add_argument("--rules", default=None, help="任务组规则（默认 wro2025）")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--no-noise", action="store_true")
    p.add_argument("--answer", action="append", default=[],
                   help="反问时依次自动回答（可以给多次）")
    p.add_argument("--show", action="store_true", help="实时俯视图")
    p.add_argument("--speed", type=float, default=0.3, help="--show 时的时间倍率")
    p.add_argument("--save", default=None, help="结束后保存俯视图 PNG")
    p.add_argument("--measure", action="store_true", help="任务 4.1 的测量")
    p.add_argument("--runs", type=int, default=3)
    args = p.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
