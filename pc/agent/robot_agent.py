import asyncio
import glob
import importlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional


class RobotAgent:
    """
    RobotAgent: 把自然语言或解析结果映射为受控的 skill 调用。

    依赖注入:
    - hub: 一个实现了移动接口的 SpikeHub 实例
    - llm_client: 一个 LLM 客户端，优先提供 `async def parse_skill(text: str, skills: list) -> dict`.
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

        # 尝试从仓库的 skills/ 目录加载 skill 定义并注册 runtime
        try:
            repo_root = Path(__file__).resolve().parents[2]
            skills_dir = repo_root / "skills"
            if skills_dir.exists():
                self.load_skills_from_dir(str(skills_dir))
        except Exception:
            # 加载失败不影响基本功能
            pass

    def _register_default_skills(self):
        self.register_skill("forward", self._skill_forward)
        self.register_skill("backward", self._skill_backward)
        self.register_skill("stop", self._skill_stop)
        self.register_skill("left", self._skill_left)
        self.register_skill("right", self._skill_right)

    def register_skill(
        self, name: str,
        func: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
    ):
        self.skills[name] = func

    def _parse_frontmatter(self, text: str) -> Dict[str, Any]:
        """简单解析 YAML frontmatter（只处理 key: value 的基本形式，和 list/json 形式）。"""
        res: Dict[str, Any] = {}
        if not text.startswith("---"):
            return res
        try:
            end = text.find('\n---', 3)
            if end == -1:
                return res
            fm = text[3:end].strip()
            for line in fm.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                if ':' not in line:
                    continue
                k, v = line.split(':', 1)
                k = k.strip()
                v = v.strip()
                # 尝试解析 json 列表或字典
                if v.startswith('[') or v.startswith('{'):
                    try:
                        res[k] = json.loads(v)
                        continue
                    except Exception:
                        pass
                # 逗号分割为列表
                if ',' in v:
                    res[k] = [p.strip() for p in v.split(',') if p.strip()]
                    continue
                # 布尔与数值
                if v.lower() in ('true', 'false'):
                    res[k] = v.lower() == 'true'
                    continue
                try:
                    if '.' in v:
                        res[k] = float(v)
                    else:
                        res[k] = int(v)
                    continue
                except Exception:
                    pass
                # 字符串去引号
                if (v.startswith('"') and v.endswith('"')) \
                        or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                res[k] = v
        except Exception:
            return {}
        return res

    def load_skills_from_dir(self, skills_dir: str):
        """扫描目录下的 `*.skill.md` 文件，读取 frontmatter 中的 `id` 和 `runtime` 字段并注册。

        runtime 格式支持 `module.path:callable_name`，例如 `tools.move_tools:execute`。
        """
        files = glob.glob(os.path.join(skills_dir, "*.skill.md"))
        for f in files:
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    raw = fh.read()
                meta = self._parse_frontmatter(raw)
                sid = meta.get('id') or meta.get('name')
                runtime = meta.get('runtime')
                if not sid or not runtime:
                    continue
                # runtime: module:attr
                if ':' in runtime:
                    module_name, attr = runtime.split(':', 1)
                elif '.' in runtime:
                    # fallback: module.attr
                    module_name, attr = runtime.rsplit('.', 1)
                else:
                    continue
                mod = importlib.import_module(module_name)
                fn = getattr(mod, attr, None)
                if fn is None:
                    continue
                wrapped = self._wrap_callable(fn)
                self.register_skill(sid, wrapped)
            except Exception:
                continue

    def _wrap_callable(self, fn: Callable) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
        """把可能的同步或异步函数包装成统一的 async callable(args)->dict。"""
        if inspect.iscoroutinefunction(fn):
            async def _async_call(args: Dict[str, Any]) -> Dict[str, Any]:
                return await fn(args)
            return _async_call

        async def _sync_call(args: Dict[str, Any]) -> Dict[str, Any]:
            return await self.loop.run_in_executor(None, lambda: fn(args))

        return _sync_call

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
                return {"error": f"LLM generate/parse failed: {e}",
                        "raw": raw if 'raw' in locals() else None}

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
