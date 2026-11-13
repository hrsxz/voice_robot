"""
任务代理 (改动 task-layer 设计 D1、D4、D6–D10, 任务 2.1–2.6)。

一个目标 (用户的原话) -> LLM 工具调用的多轮循环:
    系统提示 (能力、规则、任务组、物体和目标表) + 开局世界摘要
    -> LLM 调用技能 -> 执行 -> 把结果 (附带状态有变化的物体) 交回 LLM -> ...
    -> LLM 不再调用工具, 给出总结。

- 上限 (D8): 每个任务最多 30 次技能调用、5 分钟 (按运行时的时钟; 模拟器里是模拟时间);
  达到上限就停止, 报告已送达和未送达的物体。
- 失败策略 (D8): 同一个物体的 move_object / pick 失败后最多再试 1 次; 第二次失败后代码拒绝再试,
  提示 LLM 向用户报告并用 ask_user 询问是否继续。
- 反问 (D7): ask_user 交给外部的 ask 回调 (终端 / 语音), 30 秒没有回答就取消任务。小车等待时不动。
- 停止 (D9): stop() 中止导航 (Hub 立即停), 取消代理的异步任务, 之后不再调用 LLM。
- 进度 (2.6): 每个技能开始和结束时调用 on_progress (终端显示; 可选语音播报由调用方做成非阻塞)。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pc.navigation.controller import FAILED, OK, STOPPED
from pc.task.missions import MissionRules
from pc.task.skills import (
    COLOR_LABELS,
    ZONE_COLORS,
    TaskSkills,
    object_label,
    target_label,
)
from pc.task.tools import build_task_tools, check_call, load_task_tools

MAX_SKILL_CALLS = 30
TIME_LIMIT_S = 300.0
ASK_TIMEOUT_S = 30.0
MAX_ATTEMPTS_PER_OBJECT = 2  # 第一次 + 再试 1 次
DEFAULT_TASK_MODEL = "gpt-5.5"

# 结果状态
DONE = "done"  # LLM 认为完成了 (结束时给出总结)
LIMIT = "limit"  # 达到步数或时间上限
CANCELLED = "cancelled"  # 反问没有回答
TASK_STOPPED = "stopped"
ERROR = "error"  # LLM 调用失败等

STATUS_LABELS = {DONE: "完成", LIMIT: "达到上限", CANCELLED: "已取消", TASK_STOPPED: "已停止",
                 ERROR: "出错"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class TaskLLM(Protocol):
    async def complete(self, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]]) -> LLMReply: ...


class OpenAITaskLLM:
    """用 LLMClient (OpenAI Chat Completions) 做任务代理的模型。"""

    def __init__(self, model: str = DEFAULT_TASK_MODEL, timeout: float = 60.0,
                 client: Any = None) -> None:
        from pc.llm.llm_client import LLMClient

        self.model = model
        self.timeout = timeout
        self.client = client or LLMClient(default_model=model, timeout=int(timeout),
                                          enable_ollama=False, max_completion_tokens=4000)

    async def complete(self, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]]) -> LLMReply:
        msg = await self.client.complete_with_tools(messages, tools, model=self.model,
                                                    timeout=self.timeout)
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "")
                 for c in (msg.tool_calls or [])]
        return LLMReply(msg.content, calls)

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass
class CallRecord:
    name: str
    args: dict[str, Any] | None
    status: str  # ok / failed / stopped / rejected (参数不合法或策略拒绝) / answered / timeout
    reason: str = ""


@dataclass
class TaskResult:
    goal: str
    status: str
    message: str  # 给用户看的总结
    calls: list[CallRecord] = field(default_factory=list)
    skill_calls: int = 0
    llm_rounds: int = 0
    elapsed_s: float = 0.0
    delivered: list[str] = field(default_factory=list)  # 涉及的物体里已送达的
    pending: list[str] = field(default_factory=list)  # 涉及的物体里还没送达的
    questions: list[str] = field(default_factory=list)


AskFn = Callable[[str, list[str]], Awaitable[str | None]]


class TaskAgent:
    def __init__(
        self,
        skills: TaskSkills,
        llm: TaskLLM,
        ask: AskFn | None = None,
        on_progress: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
        max_skill_calls: int = MAX_SKILL_CALLS,
        time_limit_s: float = TIME_LIMIT_S,
        ask_timeout_s: float = ASK_TIMEOUT_S,
    ) -> None:
        self.skills = skills
        self.llm = llm
        self.ask = ask
        self.on_progress = on_progress or (lambda _m: None)
        self.skills.on_progress = self.on_progress
        self.clock = clock or time.monotonic
        self.max_skill_calls = max_skill_calls
        self.time_limit_s = time_limit_s
        self.ask_timeout_s = ask_timeout_s
        self.specs = load_task_tools()
        self.tools = build_task_tools(skills.field, self.specs)
        self.system_prompt = build_system_prompt(skills)
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ---------------- 运行 / 停止 ----------------

    async def run(self, goal: str) -> TaskResult:
        """执行一个目标, 直到完成、失败、达到上限、被停止或取消。"""
        self._stopped = False
        self.skills.nav.clear_stop()
        result = TaskResult(goal=goal, status=ERROR, message="")
        self._task = asyncio.current_task()
        t0 = self.clock()
        try:
            await self._loop(goal, result, t0)
        except asyncio.CancelledError:
            if not self._stopped:
                raise
            result.status = TASK_STOPPED
            result.message = "已停止"
        finally:
            self._task = None
            result.elapsed_s = self.clock() - t0
            self._fill_progress(result)
        if result.status == TASK_STOPPED:
            self.on_progress("任务已停止")
        return result

    async def stop(self) -> None:
        """中止正在执行的技能 (Hub 立即停), 取消整个任务。"""
        self._stopped = True
        stop = self.skills.nav.request_stop()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        await stop

    @property
    def running(self) -> bool:
        return self._task is not None

    # ---------------- 循环 ----------------

    async def _loop(self, goal: str, result: TaskResult, t0: float) -> None:
        snap = await self.skills.manip.refresh()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": (
                f"用户的请求：{goal}\n\n开局世界状态：\n"
                + json.dumps(snap.summary(), ensure_ascii=False, separators=(",", ":")))},
        ]
        failures: dict[str, int] = {}
        max_rounds = self.max_skill_calls * 2 + 5  # 防止一直给不合法的调用
        while True:
            if result.llm_rounds >= max_rounds:
                self._finish_limit(result, f"LLM 往返超过 {max_rounds} 次")
                return
            result.llm_rounds += 1
            try:
                reply = await self.llm.complete(messages, self.tools)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result.status = ERROR
                result.message = f"任务代理调用 LLM 失败：{type(exc).__name__}: {exc}"
                return
            if not reply.tool_calls:
                result.status = DONE
                result.message = (reply.content or "").strip() or "完成"
                return
            messages.append(_assistant_message(reply))
            for call in reply.tool_calls:
                output = await self._handle_call(call, result, failures, t0)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(output, ensure_ascii=False)})
                if result.status in (LIMIT, CANCELLED):
                    return

    async def _handle_call(self, call: ToolCall, result: TaskResult,
                           failures: dict[str, int], t0: float) -> dict[str, Any]:
        checked = check_call(call.name, call.arguments, self.skills.field, self.specs)
        if not checked.ok:
            result.calls.append(CallRecord(call.name, None, "rejected", checked.error or ""))
            return {"status": "rejected", "reason": checked.error}
        args = checked.args or {}

        if call.name == "ask_user":
            return await self._ask(args, result)

        # 上限: 在发出下一个技能之前检查
        if result.skill_calls >= self.max_skill_calls:
            self._finish_limit(result, f"技能调用达到 {self.max_skill_calls} 次")
            return {"status": "rejected", "reason": "达到上限，任务停止"}
        if self.clock() - t0 >= self.time_limit_s:
            self._finish_limit(result, f"用时超过 {self.time_limit_s / 60:.0f} 分钟")
            return {"status": "rejected", "reason": "达到上限，任务停止"}

        oid = args.get("object") if call.name in ("move_object", "pick") else None
        if oid is not None and failures.get(oid, 0) >= MAX_ATTEMPTS_PER_OBJECT:
            reason = (f"{oid} 已经失败 {failures[oid]} 次，不要再试。"
                      "向用户报告失败原因，并用 ask_user 询问是否继续处理其他物体。")
            result.calls.append(CallRecord(call.name, args, "rejected", reason))
            return {"status": "rejected", "reason": reason}

        result.skill_calls += 1
        out = await self.skills.run(call.name, args)
        status = out.get("status", FAILED)
        rejected = bool((out.get("details") or {}).get("rejected"))
        result.calls.append(CallRecord(call.name, args, "rejected" if rejected else status,
                                       out.get("reason", "")))
        if status == STOPPED:
            # 不是 stop() 发起的停止 (例如 Hub 自己报告被打断): 也结束任务
            self._stopped = True
            raise asyncio.CancelledError
        if oid is not None and status == FAILED and not rejected:
            failures[oid] = failures.get(oid, 0) + 1
            left = MAX_ATTEMPTS_PER_OBJECT - failures[oid]
            out["hint"] = (f"{oid} 失败 {failures[oid]} 次，还可以再试 {left} 次" if left > 0 else
                           f"{oid} 已经失败 {failures[oid]} 次：不要再试，向用户报告原因，"
                           "用 ask_user 询问是否继续处理其他物体")
        return out

    async def _ask(self, args: dict[str, Any], result: TaskResult) -> dict[str, Any]:
        question = str(args.get("question", "")).strip()
        options = [str(o) for o in args.get("options") or []]
        result.questions.append(question)
        text = question + (f"（{' / '.join(options)}）" if options else "")
        self.on_progress(f"提问：{text}")
        answer: str | None = None
        if self.ask is not None:
            try:
                answer = await asyncio.wait_for(self.ask(question, options), self.ask_timeout_s)
            except TimeoutError:
                answer = None
        if answer is None or not answer.strip():
            result.calls.append(CallRecord("ask_user", args, "timeout"))
            result.status = CANCELLED
            result.message = f"{self.ask_timeout_s:.0f} 秒内没有回答，任务已取消"
            self.on_progress(result.message)
            return {"status": "timeout", "reason": "用户没有回答，任务取消"}
        self.on_progress(f"回答：{answer}")
        result.calls.append(CallRecord("ask_user", args, "answered", answer))
        return {"status": OK, "answer": answer}

    def _finish_limit(self, result: TaskResult, reason: str) -> None:
        result.status = LIMIT
        self._fill_progress(result)
        done = "、".join(object_label(o, self.skills.field) for o in result.delivered) or "无"
        todo = "、".join(object_label(o, self.skills.field) for o in result.pending) or "无"
        result.message = f"{reason}，任务停止。已送达：{done}；未送达：{todo}"
        self.on_progress(result.message)

    def _fill_progress(self, result: TaskResult) -> None:
        involved: list[str] = []
        for c in result.calls:
            oid = (c.args or {}).get("object")
            if oid and oid not in involved:
                involved.append(oid)
        result.delivered = [o for o in involved
                            if self.skills.world.object_state(o) == "delivered"]
        result.pending = [o for o in involved if o not in result.delivered]


def _assistant_message(reply: LLMReply) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": reply.content or None,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments or "{}"}}
            for c in reply.tool_calls
        ],
    }


# ---------------------------------------------------------------- 系统提示


def build_system_prompt(skills: TaskSkills) -> str:
    field = skills.field
    rules: MissionRules = skills.rules
    objects = "\n".join(
        f"- {oid}：{object_label(oid, field)}（规则目标 {skills.rule_targets[oid]}）"
        for oid in field.objects
    )
    targets = []
    for tid in field.targets:
        extra = ""
        if tid.startswith("zone:"):
            extra = f"（{ZONE_COLORS.get(tid[5:], '')}）"
        targets.append(f"- {tid}：{target_label(tid)}{extra}")
    colors = "、".join(f"{v}={k}" for k, v in COLOR_LABELS.items())
    return "\n".join([
        "你是乐高小车的任务代理。小车在 WRO Starter 场地上，能把场地上的物体夹起来送到区域或圆圈。",
        "你通过调用工具完成用户的目标：每次调用一个工具，看到结果后再决定下一步。",
        "",
        "场地物体（编号：中文名）：",
        objects,
        "",
        "目标（编号：中文名）：",
        *targets,
        f"颜色：{colors}。坐标单位厘米，原点在场地左上角（A 区那个角），x 向右、y 向下。",
        "",
        f"WRO 任务组（{rules.title}）：",
        rules.describe(),
        "",
        "规则：",
        "1. 用户明确说了目标（某个区域、圆圈或起终点区）时，move_object 的 target 用用户说的目标，"
        "即使和规则目标不同；用户只说了任务编号、\"送回家\"\"送到该去的地方\"时，不填 target"
        "（用规则目标）。",
        "2. 有歧义时必须先用 ask_user 提问，列出全部候选项，等回答后再动；不要自己猜。"
        "例如\"红色的\"可能指好几个红色物体；\"送到圆圈\"没说哪个圆圈且没有规则目标可用。"
        "问题和选项只用中文名（会读给用户听），不要带编号。",
        "3. 优先用 move_object（一次送一个物体）。go_to / pick / place 只用于失败恢复，"
        "或者用户的特殊要求（例如\"带到 B 区附近先别放下\"）。",
        "4. 一次只能夹一个物体。夹着物体时不能抓别的，也不能 park。",
        "5. 技能失败时看 reason：同一个物体最多再试 1 次；还是失败就向用户报告原因，"
        "并用 ask_user 询问是否继续处理其他物体。",
        "6. 不要调用 park，除非用户要求停车、要求完成任务 5，或者要求完成全部任务（最后停车）。",
        "7. 物体状态\"已送达\"并且目标正确的，不用再送。看不到（丢失）的物体向用户说明。",
        "8. 顺序：有物体在 D 区上方的方格（d1–d6）里时，先送走它们，再往 D 区里送东西"
        "（D 区里的物体会挡住抓方格里物体的路线）。",
        "9. 全部做完后，不再调用工具，用一两句中文总结结果（送达了什么、哪些没做成以及原因）。",
    ])


__all__ = [
    "ASK_TIMEOUT_S",
    "CANCELLED",
    "DEFAULT_TASK_MODEL",
    "DONE",
    "ERROR",
    "LIMIT",
    "MAX_SKILL_CALLS",
    "STATUS_LABELS",
    "TASK_STOPPED",
    "TIME_LIMIT_S",
    "CallRecord",
    "LLMReply",
    "OpenAITaskLLM",
    "TaskAgent",
    "TaskLLM",
    "TaskResult",
    "ToolCall",
    "build_system_prompt",
]
