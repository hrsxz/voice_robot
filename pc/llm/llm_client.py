import asyncio
import importlib
import os
import ssl
from pathlib import Path
from typing import Any

import httpx
import truststore
from dotenv import load_dotenv
from openai import (APIConnectionError, APIStatusError, AsyncOpenAI,
                    AuthenticationError, RateLimitError)

from pc import constants

# 尝试复用仓库内的本地 ollama 客户端（若存在）
try:
    _ollama: Any = importlib.import_module("pc.llm.ollama_client")
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

        self.skills_context = self._load_skills_context(constants.project_root_path / "skills")

    def _load_skills_context(self, skills_dir: Path) -> str:
        if not skills_dir.exists() or not skills_dir.is_dir():
            return ""

        blocks: list[str] = []
        for p in sorted(skills_dir.glob("*.skill.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue

            fm = self._extract_frontmatter(text)
            if fm:
                blocks.append(f"[{p.name}]\n{fm}")

        return "\n\n".join(blocks)

    @staticmethod
    def _extract_frontmatter(md_text: str) -> str:
        s = (md_text or "").lstrip()
        if not s.startswith("---"):
            return ""
        parts = s.split("---", 2)
        if len(parts) < 3:
            return ""
        return parts[1].strip()

    def build_intent_prompt(self, user_text: str) -> str:
        """
        基于 skills frontmatter 组装意图提示词。
        说明:
        - 全局规则来自 skills/base.skill.md
        - 动作与参数约束来自各技能文件
        """
        if self.skills_context:
            return (
                "你是机器人动作解析器。请严格遵循以下技能定义与约束，"
                "把用户输入转换为合法 JSON。\n"
                "只允许输出一个 JSON 对象，不要 markdown，不要解释。\n\n"
                "可用技能定义（来自 skills 目录 frontmatter）:\n"
                f"{self.skills_context}\n\n"
                f"用户输入: {user_text}"
            )

        # 兜底: 没有 skills 时仍强制 JSON-only 输出。
        return (
            "你是机器人动作解析器。"
            "只输出合法 JSON，格式为 {'steps':[{'action':'...','args':{...}}]}，"
            "不要 markdown，不要解释。\n"
            f"用户输入: {user_text}"
        )

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
        try:
            resp = await self._openai_client.chat.completions.create(
                model=model,  # gpt-5.4-mini # gpt-5.4 gpt-5.5
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
