import asyncio
import importlib
import json
import os
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx
import truststore
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from pc import constants
from pc.llm import intent_parser
from skills import ActionRule, load_skill_frontmatters, load_skill_registry

# 尝试复用仓库内的本地 ollama 客户端（若存在）
try:
    _ollama: Any = importlib.import_module("pc.llm.ollama_client")
    _HAS_OLLAMA = True
except Exception:
    _ollama = None
    _HAS_OLLAMA = False


load_dotenv(constants.project_root_path / ".env")

# 模型可能不支持的可选参数: 不支持时去掉后重试一次, 之后不再发送
_OPTIONAL_PARAMS = ("reasoning_effort", "max_completion_tokens")


class LLMUnavailableError(RuntimeError):
    """没有可用的 LLM (没有本地 ollama 客户端, 也没有 OPENAI_API_KEY)。"""


class LLMClient:
    """
    统一 LLM 客户端：
    优先调用仓库内 ollama 客户端；失败后回退 OpenAI；都不可用时抛出 LLMUnavailableError
    （不再把原文交回去让规则"猜"）。
    """

    def __init__(
        self,
        default_model: str = "gpt-5.4-mini",  # gpt-5.4 gpt-5.5
        timeout: int = 5,
        enable_ollama: bool = True,
        max_completion_tokens: int | None = 800,
        reasoning_effort: str | None = None,
    ) -> None:
        self.default_model = default_model
        self.timeout = timeout
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.enable_ollama = enable_ollama
        # 限制输出长度; 推理模型的推理 token 也计入这个上限, 所以不要设得太小
        self.max_completion_tokens = max_completion_tokens
        # 推理强度, 例如 "minimal" / "low"; None 表示不传这个参数
        self.reasoning_effort = reasoning_effort
        self._unsupported_params: set[str] = set()
        self._openai_http_client: Any = None
        self._openai_client: Any = None

        self.intent_instructions = build_intent_instructions()

    def build_intent_prompt(self, user_text: str, executed: list[str] | None = None) -> str:
        """单段文本形式的提示词（给只接受一段 prompt 的本地客户端用）。"""
        return f"{self.intent_instructions}\n\n{_user_content(user_text, executed)}"

    def build_intent_messages(
        self, user_text: str, executed: list[str] | None = None
    ) -> list[dict[str, str]]:
        """固定的说明放在 system 消息里（前缀不变，便于服务端缓存），用户的话单独放。"""
        return [
            {"role": "system", "content": self.intent_instructions},
            {"role": "user", "content": _user_content(user_text, executed)},
        ]

    @property
    def is_available(self) -> bool:
        return (self.enable_ollama and _HAS_OLLAMA) or bool(self.openai_api_key)

    async def generate(
        self,
        input_text: str,
        model: str | None = None,
        timeout: int | None = None,
        executed: list[str] | None = None,
    ) -> str:
        t = timeout if timeout is not None else self.timeout
        m = model or self.default_model

        # 1) 优先：本地 ollama 客户端
        if self.enable_ollama and _HAS_OLLAMA:
            try:
                prompt = self.build_intent_prompt(input_text, executed)
                text = await self._call_ollama(prompt, model=m, timeout=t)
                if text:
                    return text
            except Exception as e:
                print(f"[WARN] Ollama failed, fallback to OpenAI: {type(e).__name__}: {e}")

        # 2) 回退：OpenAI
        if self.openai_api_key:
            try:
                return await asyncio.wait_for(
                    self._call_openai(self.build_intent_messages(input_text, executed), model=m),
                    timeout=t,
                )
            except Exception as e:
                print(f"[WARN] OpenAI fallback failed: {type(e).__name__}: {e}")
                raise

        raise LLMUnavailableError("没有可用的 LLM（未配置 OPENAI_API_KEY，也没有本地模型）")

    async def generate_stream(
        self,
        input_text: str,
        executed: list[str] | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> AsyncIterator[dict]:
        """
        流式生成: LLM 每输出完整的一行 (一个动作), 立即产出解析后的 step,
        不等全部输出完。step 格式: {"action": "...", "args": {...}}。
        """
        t = timeout if timeout is not None else self.timeout
        m = model or self.default_model

        if not self.openai_api_key:
            # 没有 OpenAI 时退回非流式 (本地客户端), 都没有就抛 LLMUnavailableError
            text = await self.generate(input_text, model=m, timeout=t, executed=executed)
            for step in intent_parser.parse_intent(text)["steps"]:
                yield step
            return

        messages = self.build_intent_messages(input_text, executed)
        buffer = ""
        try:
            async with asyncio.timeout(t):
                stream = await self._create_completion(messages, m, stream=True)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    buffer += chunk.choices[0].delta.content or ""
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        for step in intent_parser.parse_line(line):
                            yield step
            for step in intent_parser.parse_line(buffer):
                yield step
        except TimeoutError as e:
            raise RuntimeError(f"LLM 超时（{t}s）") from e

    async def _call_ollama(self, prompt: str, model: str, timeout: int) -> str:
        for fn in ("generate", "complete", "call", "request"):
            if hasattr(_ollama, fn):
                fn_obj = getattr(_ollama, fn)
                res = fn_obj(prompt, model=model)
                if asyncio.iscoroutine(res):
                    return await asyncio.wait_for(res, timeout=timeout)
                return str(res)
        return ""

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            verify: Any
            try:
                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                verify = True  # 回退到默认 CA 验证
            self._openai_http_client = httpx.AsyncClient(
                verify=verify,
                timeout=float(self.timeout),
            )
            self._openai_client = AsyncOpenAI(
                api_key=self.openai_api_key,
                http_client=self._openai_http_client,
            )
        return self._openai_client

    def _request_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.max_completion_tokens is not None:
            options["max_completion_tokens"] = self.max_completion_tokens
        if self.reasoning_effort is not None:
            options["reasoning_effort"] = self.reasoning_effort
        for param in self._unsupported_params:
            options.pop(param, None)
        return options

    async def complete_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        model: str | None = None, timeout: float | None = None,
    ) -> Any:
        """带工具的一次对话补全 (任务代理用), 返回 choices[0].message。只支持 OpenAI。"""
        if not self.openai_api_key:
            raise LLMUnavailableError("任务代理需要 OPENAI_API_KEY")
        t = timeout if timeout is not None else self.timeout
        resp = await asyncio.wait_for(
            self._create_completion(messages, model or self.default_model, tools=tools,
                                    tool_choice="auto", parallel_tool_calls=False),
            timeout=t,
        )
        return resp.choices[0].message

    async def _create_completion(
        self, messages: list[dict[str, Any]], model: str, stream: bool = False, **extra: Any
    ) -> Any:
        client = self._get_openai_client()
        options = self._request_options()
        options.update(extra)
        if stream:
            options["stream"] = True
        try:
            return await client.chat.completions.create(model=model, messages=messages, **options)
        except BadRequestError as e:
            # 模型不支持某个可选参数: 去掉它重试一次, 以后也不再发送
            dropped = [p for p in _OPTIONAL_PARAMS if p in options and p in str(e)]
            if not dropped:
                raise
            for param in dropped:
                print(f"[WARN] 模型 {model} 不支持参数 {param}，已去掉")
                self._unsupported_params.add(param)
                options.pop(param)
            return await client.chat.completions.create(model=model, messages=messages, **options)

    async def _call_openai(self, messages: list[dict[str, str]], model: str) -> str:
        try:
            resp = await self._create_completion(messages, model)
            return resp.choices[0].message.content or ""
        except AuthenticationError as e:
            raise RuntimeError(f"OpenAI auth error (401): {e}") from e
        except RateLimitError as e:
            raise RuntimeError(f"OpenAI quota/rate error (429): {e}") from e
        except APIConnectionError as e:
            raise RuntimeError(f"OpenAI connection error: {e}") from e
        except APIStatusError as e:
            raise RuntimeError(f"OpenAI API status error ({e.status_code}): {e}") from e

    async def aclose(self) -> None:
        if self._openai_http_client is not None:
            await self._openai_http_client.aclose()


def build_intent_instructions() -> str:
    """
    从 skills 生成紧凑的意图解析说明：输出格式 + 全局规则 + 动作表 + 少量示例。
    只包含 LLM 需要的信息，不再把完整的 frontmatter（schema、allOf、output_schema）塞进去。
    """
    registry = load_skill_registry()
    frontmatters = load_skill_frontmatters()
    base = frontmatters.get("intent_base", {})

    lines = [
        "你是机器人动作解析器。把用户的话转换成机器人动作，每行输出一个动作。",
        '每行的格式：{"action":"<动作>","args":{...}}',
        "",
        "规则：",
    ]
    lines += [f"- {rule}" for rule in base.get("hard_rules") or []]

    lines += ["", "可用动作（动作名: args）："]
    by_skill: dict[str, list[ActionRule]] = {}
    for rule in registry.actions.values():
        by_skill.setdefault(rule.skill_id, []).append(rule)
    for skill_id, rules in by_skill.items():
        fm = frontmatters.get(skill_id, {})
        triggers = "、".join(str(t) for t in fm.get("triggers") or [])
        header = f"[{fm.get('description', skill_id)}]"
        if triggers:
            header += f" 触发词：{triggers}"
        lines.append(header)
        lines += [f"- {rule.action}: {_describe_args(rule)}" for rule in rules]

    handoff = frontmatters.get("task", {}).get("handoff")
    if handoff:
        # 目标级请求交给任务层 (改动 task-layer 设计 D1): 只输出这一行, 不自己拆成动作
        key = handoff.get("arg_key", "goal")
        desc = " ".join(str(handoff.get("description", "")).split())
        lines += [f"[交给任务层] {desc}只输出这一行，不要再输出其他动作。",
                  f'- {handoff.get("name", "run_task")}: {{"{key}": 用户的原话}}']

    lines += ["", "示例："]
    for nl, output in _collect_examples(frontmatters):
        lines.append(nl if nl.startswith("已执行") else f"用户：{nl}")
        lines.append(f"输出：\n{output}")

    return "\n".join(lines)


def _describe_args(rule: ActionRule) -> str:
    if rule.value_type == "none":
        return "{}"
    if rule.value_type == "int":
        return f'{{"{rule.arg_key}": 整数 {rule.min_value}–{rule.max_value}}}'
    if rule.value_type == "str":
        allowed = " | ".join(str(a) for a in rule.allowed)
        return f'{{"{rule.arg_key}": {allowed}}}'
    return "{}"


def _user_content(user_text: str, executed: list[str] | None) -> str:
    if executed:
        return f"已执行：{', '.join(executed)}\n用户：{user_text}"
    return user_text


def _collect_examples(frontmatters: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    examples: list[tuple[str, str]] = []
    for skill_id in ["intent_base"] + [k for k in frontmatters if k != "intent_base"]:
        for ex in frontmatters.get(skill_id, {}).get("examples") or []:
            nl = ex.get("nl")
            output = ex.get("output") or ex.get("output_json")
            if nl and output:
                examples.append((str(nl), _render_example_output(str(output))))
    return examples


def _render_example_output(text: str) -> str:
    """示例输出统一显示成每行一个动作 (技能文件里的旧 {"steps": [...]} 示例也转换过来)。"""
    steps = intent_parser.parse_intent(text)["steps"]
    if not steps:
        return " ".join(text.split())
    return "\n".join(
        json.dumps(step, ensure_ascii=False, separators=(",", ":")) for step in steps
    )
