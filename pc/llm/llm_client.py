import asyncio
import os
import ssl
from typing import Any

import httpx
import truststore
from dotenv import load_dotenv
from openai import (APIConnectionError, APIStatusError, AsyncOpenAI,
                    AuthenticationError, RateLimitError)

from pc import constants

# 尝试复用仓库内的本地 ollama 客户端（若存在）
try:
    from llm import ollama_client as _ollama
    _HAS_OLLAMA = True
except Exception:
    _ollama = None
    _HAS_OLLAMA = False


load_dotenv(constants.project_root_path / ".env")


class LLMClient:
    """
    统一 LLM 客户端：
    优先调用仓库内 ollama 客户端；失败后回退 OpenAI；再失败回退原始 prompt。
    """

    def __init__(
        self,
        default_model: str = "gpt-5.4-mini",  # gpt-5.4 gpt-5.5
        timeout: int = 5,
        enable_ollama: bool = True,
    ) -> None:
        self.default_model = default_model
        self.timeout = timeout
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.enable_ollama = enable_ollama
        self._openai_http_client: Any = None
        self._openai_client: Any = None

    def build_intent_prompt(self, user_text: str) -> str:
        """
        把自然语言指令转换为严格 JSON。
        只允许输出一个 JSON 对象，不要 markdown，不要解释。
        支持多步骤动作。
        """
        schema = """
            你是机器人动作解析器。把用户输入转换为 JSON。

            只允许输出如下结构（且必须是合法 JSON）：
            {
                "steps": [
                {
                    "action": "forward|backward|left|right|stop",
                    "params": {
                    "distance_cm": number|null,
                    "angle_deg": number|null
                    }
                }
                ]
            }

            规则：
            1) 只输出 JSON，不要代码块，不要解释文本。
            2) steps 必须是数组，按执行顺序排列。
            3) action 只能是: forward, backward, turn_left, turn_right, stop, gripper_up,
            gripper_down, gripper_pos, straightforward, straightbackward, face_to.
            4) 无法识别时返回: {"steps": []}
            5) distance 统一为 cm；angle 统一为 deg。
            6) forward/backward 仅使用 distance_cm，angle_deg 设为 null。
            7) turn_left/turn_right 仅使用 angle_deg，distance_cm 设为 null。
            8) face_to 仅使用 angle_deg，distance_cm 设为 null。
            9) stop 时两个参数都为 null。
            10) gripper_up/gripper_down 两个参数都为 null。
            11) gripper_pos 仅使用 angle_deg，distance_cm 设为 null。
            12) straightforward/straightbackward 仅使用 distance_cm，angle_deg 设为 null。
            13) 若用户说“先A再B”，必须输出两个 step，不要合并为一个 step。

            示例：
            用户输入: 向前走30cm，之后左转90度, gripper_down
            输出:
            {"steps":[
                {"action":"forward","params":{"distance_cm":30,"angle_deg":null}},
                {"action":"turn_left","params":{"distance_cm":null,"angle_deg":90}},
                {"action":"gripper_down","params":{"distance_cm":null,"angle_deg":null}}
            ]}
        """
        return f"{schema}\n用户输入: {user_text}"

    async def generate(
        self,
        input_text: str,
        model: str | None = None,
        timeout: int | None = None,
    ) -> str:
        t = timeout if timeout is not None else self.timeout
        m = model or self.default_model

        llm_prompt = self.build_intent_prompt(input_text)

        # 1) 优先：本地 ollama 客户端
        if self.enable_ollama and _HAS_OLLAMA:
            try:
                text = await self._call_ollama(llm_prompt, model=m, timeout=t)
                if text:
                    return text
            except Exception as e:
                print(f"[WARN] Ollama failed, fallback to OpenAI: {type(e).__name__}: {e}")

        # 2) 回退：OpenAI
        if self.openai_api_key:
            try:
                return await asyncio.wait_for(self._call_openai(llm_prompt, model=m), timeout=t)
            except Exception as e:
                print(f"[WARN] OpenAI fallback failed: {type(e).__name__}: {e}")
                raise

        # 3) 离线调试回退
        return input_text

    async def _call_ollama(self, prompt: str, model: str, timeout: int) -> str:
        for fn in ("generate", "complete", "call", "request"):
            if hasattr(_ollama, fn):
                fn_obj = getattr(_ollama, fn)
                res = fn_obj(prompt, model=model)
                if asyncio.iscoroutine(res):
                    return await asyncio.wait_for(res, timeout=timeout)
                return str(res)
        return ""

    async def _call_openai(self, prompt: str, model: str) -> str:
        if self._openai_http_client is None:
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
        try:
            resp = await self._openai_client.chat.completions.create(
                model="gpt-5.4-mini", # gpt-5.4-mini # gpt-5.4 gpt-5.5
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
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
            self._openai_http_client = None
            self._openai_client = None
