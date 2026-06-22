import asyncio
import os

# 尝试复用仓库内的本地 ollama 客户端（若存在）
try:
    from llm import ollama_client as _ollama
    _HAS_OLLAMA = True
except Exception:
    _ollama = None
    _HAS_OLLAMA = False


async def generate(prompt: str, model: str | None = None, timeout: int = 30) -> str:
    """
    将 prompt 发送给首选 LLM 并返回纯文本结果。
    优先：仓库内的 llm.ollama_client；回退：OpenAI；否则回传原始 prompt 便于离线调试。
    """
    if _HAS_OLLAMA:
        for fn in ("generate", "complete", "call", "request"):
            if hasattr(_ollama, fn):
                fn_obj = getattr(_ollama, fn)
                res = fn_obj(prompt, model=model)
                if asyncio.iscoroutine(res):
                    return await asyncio.wait_for(res, timeout=timeout)
                return str(res)

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI

            def _call_openai() -> str:
                client = OpenAI(api_key=openai_key)
                resp = client.chat.completions.create(
                    model=model or "gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                return resp.choices[0].message.content or ""

            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _call_openai)
        except Exception as e:
            print(f"[WARN] OpenAI fallback failed, using raw prompt: {e}")
            return prompt

    return prompt
