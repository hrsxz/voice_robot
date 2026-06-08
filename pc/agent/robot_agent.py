import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional


class RobotAgent:
    """
    RobotAgent: 把自然语言或解析结果映射为受控的 skill 调用。

    依赖注入:
    - hub: 一个实现了移动接口的 SpikeHub 实例（需提供 async connect/disconnect/forward/backward/stop/turn_left/turn_right）
    - llm_client: 一个 LLM 客户端，优先提供 `async def parse_skill(text: str, skills: list) -> dict`，
      返回格式: {"skill": "forward", "args": {...}}。若没有 `parse_skill`，会尝试调用
      `generate` 并解析 JSON。

    示例:
        agent = RobotAgent(hub, llm_client)
        await agent.connect()
        res = await agent.handle_user_text("前进")
    """

    def __init__(self, hub: Any, llm_client: Any, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.hub = hub
        self.llm = llm_client
        self.loop = loop or asyncio.get_event_loop()

        # 技能注册表: name -> callable(args: dict) -> Awaitable[dict]
        self.skills: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
        self._register_default_skills()

    def _register_default_skills(self):
        self.register_skill("forward", self._skill_forward)
        self.register_skill("backward", self._skill_backward)
        self.register_skill("stop", self._skill_stop)
        self.register_skill("left", self._skill_left)
        self.register_skill("right", self._skill_right)

    def register_skill(self, name: str, func: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]):
        self.skills[name] = func

    async def connect(self):
        """Delegates to hub.connect()"""
        if hasattr(self.hub, "connect"):
            await self.hub.connect()

    async def disconnect(self):
        if hasattr(self.hub, "disconnect"):
            await self.hub.disconnect()

    async def handle_user_text(self, text: str, timeout: float = 10.0) -> Dict[str, Any]:
        """
        把用户文本发送给 LLM，得到 skill 决策并执行，返回执行结果字典。
        返回示例: {"skill":"forward","status":"ok","raw":{...}}
        """
        parsed = None

        # 首选接口: llm.parse_skill
        if hasattr(self.llm, "parse_skill"):
            try:
                coro = self.llm.parse_skill(text, skills=list(self.skills.keys()))
                parsed = await asyncio.wait_for(coro, timeout=timeout)
            except Exception as e:
                return {"error": f"LLM parse_skill failed: {e}"}
        else:
            # 回退: 让 llm.generate 返回 JSON，然后解析
            if not hasattr(self.llm, "generate"):
                return {"error": "LLM client has neither parse_skill nor generate method"}
            try:
                raw = await asyncio.wait_for(self.llm.generate(text), timeout=timeout)
                parsed = json.loads(raw)
            except Exception as e:
                return {"error": f"LLM generate/parse failed: {e}", "raw": raw if 'raw' in locals() else None}

        # 验证解析结果
        if not isinstance(parsed, dict) or "skill" not in parsed:
            return {"error": "Invalid parse result from LLM", "parsed": parsed}

        skill = parsed.get("skill")
        args = parsed.get("args", {}) or {}

        if skill not in self.skills:
            return {"error": "Unknown skill", "skill": skill}

        # 执行 skill
        try:
            result = await self.skills[skill](args)
            return {"skill": skill, "status": "ok", "result": result, "raw_parsed": parsed}
        except Exception as e:
            return {"skill": skill, "status": "error", "error": str(e), "raw_parsed": parsed}

    # ---------- 默认 skill 实现 ----------
    async def _skill_forward(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_hub()
        await self.hub.forward()
        return {"message": "moved forward"}

    async def _skill_backward(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_hub()
        await self.hub.backward()
        return {"message": "moved backward"}

    async def _skill_stop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_hub()
        await self.hub.stop()
        return {"message": "stopped"}

    async def _skill_left(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_hub()
        await self.hub.turn_left()
        return {"message": "turned left"}

    async def _skill_right(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_hub()
        await self.hub.turn_right()
        return {"message": "turned right"}

    async def _ensure_hub(self):
        if self.hub is None:
            raise RuntimeError("Hub not set")


__all__ = ["RobotAgent"]
