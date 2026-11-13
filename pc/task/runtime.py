"""
任务层的运行环境: 把场地、布局、导航、抓取、技能和任务代理组装起来, 并管理"同一时间只执行一个任务"。

现在只有模拟器环境 (摄像头到货前): TaskRuntime.simulated(...)。
接实车时换成真实的位姿 / 物体 / Hub 接口 (pc/navigation/ports.py 的适配器), 其余不变。

    runtime = TaskRuntime.simulated(llm=OpenAITaskLLM())   # 默认布局 wro2025_demo
    task = runtime.start("把黄色小人送回家", ask=..., on_done=...)
    await runtime.stop()     # 喊"停"
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pc.field.layout import Layout, load_layout, random_layout
from pc.field.model import FieldModel, load_field
from pc.sim.mission import build
from pc.sim.simulator import SimConfig, Simulator
from pc.task.agent import AskFn, TaskAgent, TaskLLM, TaskResult
from pc.task.missions import MissionRules, load_missions
from pc.task.skills import TaskSkills

NOISY = dict(distance_error=0.05, turn_error=0.05, pose_noise_cm=0.3, heading_noise_deg=1.0,
             object_noise_cm=0.3)


class TaskBusyError(RuntimeError):
    pass


class TaskRuntime:
    def __init__(self, skills: TaskSkills, llm: TaskLLM, clock: Callable[[], float],
                 sim: Simulator | None = None, on_progress: Callable[[str], None] = print,
                 **agent_options: Any) -> None:
        self.skills = skills
        self.llm = llm
        self.clock = clock
        self.sim = sim
        self.on_progress = on_progress
        self.agent_options = agent_options
        self.agent: TaskAgent | None = None
        self._task: asyncio.Task | None = None

    @classmethod
    def simulated(
        cls, llm: TaskLLM, layout: str = "wro2025_demo", seed: int = 0, noise: bool = True,
        time_scale: float = 0.0, ruleset: str | None = None, field: FieldModel | None = None,
        on_progress: Callable[[str], None] = print, nav_log: Callable[[str], None] | None = None,
        show: bool = False, **agent_options: Any,
    ) -> TaskRuntime:
        """模拟器环境。show=True 时打开实时俯视图 (OpenCV 窗口, 按 q 关闭)。"""
        field = field or load_field()
        lay: Layout = (random_layout(field, seed) if layout == "random"
                       else load_layout(layout, field))
        config = SimConfig(seed=seed, time_scale=time_scale, **(NOISY if noise else {}))
        sim, _world, _nav, manip = build(field, lay, config,
                                         log=nav_log or (lambda _m: None))
        if show:
            attach_live_view(sim)
        rules: MissionRules = load_missions(ruleset, field)
        skills = TaskSkills(manip, rules, on_progress)
        return cls(skills, llm, sim.clock, sim=sim, on_progress=on_progress, **agent_options)

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, goal: str, ask: AskFn | None = None,
              on_done: Callable[[TaskResult], Awaitable[None] | None] | None = None
              ) -> asyncio.Task:
        """在后台开始一个任务 (不等它完成)。已经有任务在执行时抛出 TaskBusyError。"""
        if self.busy:
            raise TaskBusyError("已经有任务在执行，先说\"停\"")
        self.agent = TaskAgent(self.skills, self.llm, ask=ask, on_progress=self.on_progress,
                               clock=self.clock, **self.agent_options)
        agent = self.agent

        async def run() -> TaskResult:
            result = await agent.run(goal)
            if on_done is not None:
                out = on_done(result)
                if asyncio.iscoroutine(out):
                    await out
            return result

        self._task = asyncio.get_running_loop().create_task(run())
        return self._task

    async def run(self, goal: str, ask: AskFn | None = None) -> TaskResult:
        return await self.start(goal, ask)

    async def stop(self) -> bool:
        """停止正在执行的任务; 返回 True 表示确实停了一个任务。"""
        if not self.busy or self.agent is None:
            return False
        await self.agent.stop()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return True


def attach_live_view(sim: Simulator) -> Any:
    """小车每移动一步刷新一次俯视图。"""
    from pc.sim.viewer import LiveView

    view = LiveView(sim)
    orig = sim._move_robot

    def moved(new, direction):
        orig(new, direction)
        view.update()

    sim._move_robot = moved  # type: ignore[method-assign]
    view.update(force=True)
    return view


__all__ = ["NOISY", "TaskBusyError", "TaskRuntime", "attach_live_view"]
