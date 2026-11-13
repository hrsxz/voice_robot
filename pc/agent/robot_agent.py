import asyncio
from dataclasses import dataclass
from typing import Any

from pc.tools import camera_tools, move_tools, sensor_tool
from skills import load_skill_registry

# 每条命令的结果状态 (有且只有一个)
PENDING = "pending"
EXECUTED = "executed"
REJECTED = "rejected"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
COMM_SKIPPED = "comm_skipped"
FAILED = "failed"

STATUS_LABELS = {
    PENDING: "等待执行",
    EXECUTED: "已执行",
    REJECTED: "被拒绝",
    CANCELLED: "被停止取消",
    INTERRUPTED: "被停止中断",
    COMM_SKIPPED: "因通信失败跳过",
    FAILED: "执行失败",
}


@dataclass
class CommandRecord:
    index: int
    cmd: str | None
    status: str = PENDING
    detail: str | None = None
    action: str | None = None
    value: Any = None
    gripper: tuple[int, int] | None = None  # Hub 回报的夹爪角度 (左, 右)

    @property
    def display(self) -> str:
        if self.action is None:
            return str(self.cmd)
        return self.action if self.value is None else f"{self.action} {self.value}"


class CommandBatch:
    """
    一句话产生的所有命令。命令可以陆续加入 (流式模式), 加完后 close();
    关闭并且所有命令都有了最终状态时, 这一批就结束了。
    """

    def __init__(self, timer: Any = None) -> None:
        self.timer = timer
        self.records: list[CommandRecord] = []
        self._closed = False
        self._done = asyncio.Event()

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    def close(self) -> None:
        self._closed = True
        self._update_done()

    def _update_done(self) -> None:
        if self._closed and all(r.status != PENDING for r in self.records):
            self._done.set()

    async def wait(self) -> None:
        await self._done.wait()

    @property
    def executed(self) -> list[str]:
        return [r.detail or r.display for r in self.records if r.status == EXECUTED]

    @property
    def gripper(self) -> tuple[int, int] | None:
        """这一批命令执行后最后一次回报的夹爪角度 (左, 右)。"""
        for record in reversed(self.records):
            if record.gripper is not None:
                return record.gripper
        return None

    def result(self) -> dict:
        """
        统一返回结构:
        {"status": "ok|partial|error", "executed": [...],
         "skipped": [{"index", "cmd", "reason"}], "errors": [{"index", "cmd", "error"}]}
        """
        executed = self.executed
        skipped = [
            {"index": r.index, "cmd": r.cmd,
             "reason": r.detail if r.status == REJECTED else STATUS_LABELS[r.status]}
            for r in self.records
            if r.status in (REJECTED, CANCELLED, INTERRUPTED, COMM_SKIPPED)
        ]
        errors = [
            {"index": r.index, "cmd": r.display, "error": r.detail}
            for r in self.records if r.status == FAILED
        ]
        if errors or skipped:
            status = "partial" if executed else "error"
        else:
            status = "ok"
        return {
            "status": status, "executed": executed, "skipped": skipped, "errors": errors,
            "gripper": self.gripper,
        }

    def summary_line(self) -> str:
        if not self.records:
            return "[执行] 没有命令"
        parts = []
        for r in self.records:
            text = f"{STATUS_LABELS[r.status]} {r.display}"
            if r.status in (REJECTED, FAILED) and r.detail:
                text += f"（{r.detail}）"
            parts.append(text)
        gripper = self.gripper
        if gripper is not None:
            parts.append(f"夹爪 左{gripper[0]}° 右{gripper[1]}°")
        return "[执行] " + " | ".join(parts)


class RobotAgent:
    """
    单链路执行 Agent, 所有命令 (快速通道 / LLM / 以后的 Realtime) 都从这里下发:
    命令先按技能规则校验, 合法的进入执行队列, 由后台任务按入队顺序逐条执行。
    运动命令发出后要等 Hub 完成, 才取下一条; 这样"停"可以取消所有还没发出的命令。
    """

    COMM_SKIP_REASON = STATUS_LABELS[COMM_SKIPPED]

    def __init__(self, hub: Any):
        self.hub = hub
        self.action_rules = load_skill_registry().actions
        self._queue: asyncio.Queue[tuple[CommandBatch, CommandRecord]] | None = None
        self._worker: asyncio.Task | None = None
        self._send_lock: asyncio.Lock | None = None

    async def connect(self) -> None:
        if hasattr(self.hub, "connect"):
            await self.hub.connect()

    async def disconnect(self) -> None:
        await self.close()
        if hasattr(self.hub, "disconnect"):
            await self.hub.disconnect()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # ---------- 提交命令 ----------

    def new_batch(self, timer: Any = None) -> CommandBatch:
        return CommandBatch(timer)

    def enqueue(self, batch: CommandBatch, raw_cmd: Any) -> CommandRecord:
        """校验一条命令, 合法的放进执行队列; 不合法的直接标为被拒绝。"""
        self._ensure_worker()
        assert self._queue is not None

        index = len(batch.records)
        item = raw_cmd if isinstance(raw_cmd, dict) else {"cmd": raw_cmd}
        record = CommandRecord(index=index, cmd=item.get("cmd") if isinstance(item, dict) else None)
        batch.records.append(record)

        ok, normalized, reason = self._normalize_sequence_item(item)
        if not ok:
            print(f"Skipped command: {record.cmd}, reason: {reason}")
            record.status = REJECTED
            record.detail = reason
            batch._update_done()
            return record

        record.action = normalized["action"]
        record.value = normalized["value"]
        self._queue.put_nowait((batch, record))
        return record

    def reject(self, batch: CommandBatch, cmd: str, reason: str) -> CommandRecord:
        """记录一条在进入队列之前就被拒绝的命令 (例如 realtime 链路里不合法的函数调用)。"""
        record = CommandRecord(index=len(batch.records), cmd=cmd, status=REJECTED, detail=reason)
        batch.records.append(record)
        batch._update_done()
        return record

    def submit(self, seq_spec: dict, timer: Any = None) -> CommandBatch:
        """把一整个序列放进队列, 立即返回 (不等执行完)。"""
        batch = self.new_batch(timer)
        seq = seq_spec.get("sequence") if isinstance(seq_spec, dict) else None
        if not isinstance(seq, list):
            batch.records.append(
                CommandRecord(index=-1, cmd=None, status=FAILED, detail="sequence is not list")
            )
        else:
            for item in seq:
                self.enqueue(batch, item)
        batch.close()
        return batch

    async def execute_sequence(self, seq_spec: dict, timer: Any = None) -> dict:
        """
        统一执行入口，直接消费 intent_mapper 输出:
        {"sequence": [{"cmd": "forward 30"}, {"cmd": "left 90"}, {"cmd": "stop"}]}
        入队并等待全部执行完, 返回 CommandBatch.result() 的结构。

        timer: 可选的耗时记录 (pc.utils.latency.CommandTimer), 第一条命令发给 Hub 时打 ble_sent。
        参数非法的命令只跳过它自己; Hub 通信失败时跳过队列里剩下的所有命令。
        """
        batch = self.submit(seq_spec, timer)
        await batch.wait()
        return batch.result()

    # ---------- 停止 ----------

    @property
    def gripper_state(self) -> tuple[int, int] | None:
        """最新的夹爪状态 (左, 右), 0=放下 90=抬起; None 表示未知 (例如 Hub 程序刚重新启动)。"""
        return getattr(self.hub, "gripper", None)

    async def stop(self) -> list[CommandRecord]:
        """
        清空队列里所有还没发出的命令 (标为被停止取消), 然后立即发送 stop。
        Hub 支持插队急停 (协议 v2) 时, 正在执行的动作会被打断 (标为被停止中断)。
        """
        cancelled = self._drain_queue(CANCELLED)
        send_stop = getattr(self.hub, "send_stop", None)
        if send_stop is not None:
            await send_stop()  # 不等就绪, 也不排在执行任务后面
            return cancelled
        async with self._get_send_lock():
            out = await self._dispatch(route="move", action="stop", value=None)
        if out.get("status") != "ok":
            print(f"stop 发送失败: {out.get('detail')}")
        return cancelled

    # ---------- 内部 ----------

    def _get_send_lock(self) -> asyncio.Lock:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        return self._send_lock

    def _ensure_worker(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run_worker())

    def _drain_queue(self, status: str, detail: str | None = None) -> list[CommandRecord]:
        drained: list[CommandRecord] = []
        if self._queue is None:
            return drained
        while not self._queue.empty():
            batch, record = self._queue.get_nowait()
            if record.status == PENDING:
                record.status = status
                record.detail = detail
                drained.append(record)
                batch._update_done()
        return drained

    async def _run_worker(self) -> None:
        assert self._queue is not None
        while True:
            batch, record = await self._queue.get()
            if record.status != PENDING:
                continue
            try:
                await self._execute_record(batch, record)
            finally:
                batch._update_done()

    async def _execute_record(self, batch: CommandBatch, record: CommandRecord) -> None:
        rule = self.action_rules[record.action]
        route = rule.route
        try:
            async with self._get_send_lock():
                exec_out = await self._dispatch(
                    route=route, action=record.action, value=record.value
                )
        except Exception as exc:
            record.status = FAILED
            record.detail = str(exc)
            return

        if exec_out.get("status") != "ok":
            record.status = FAILED
            record.detail = exec_out.get("detail", "tool error")
            if exec_out.get("error_type") == "comm":
                self._on_comm_failure(batch, record.detail)
            return

        record.status = EXECUTED
        record.detail = exec_out.get("detail") or record.display
        if route != "move":
            return
        if batch.timer is not None:
            batch.timer.mark("ble_sent")

        # 等 Hub 完成这个动作再取下一条, 这样"停"能取消后面还没发出的命令
        wait_done = getattr(self.hub, "wait_done", None)
        if wait_done is not None:
            reply = await wait_done()  # 协议 v2: DONE / STOPPED / ERR
            if reply is None:
                self._on_comm_failure(batch, f"等待 Hub 完成 '{record.display}' 超时")
                return
            record.gripper = reply.gripper
            if reply.kind == "ERR":
                record.status = REJECTED
                record.detail = f"Hub：{reply.reason_text}"
            elif reply.kind == "STOPPED":
                record.status = INTERRUPTED
                record.detail = reply.reason or "stop"
            return

        wait_idle = getattr(self.hub, "wait_idle", None)
        if wait_idle is not None and not await wait_idle():
            self._on_comm_failure(batch, f"等待 Hub 完成 '{record.display}' 超时")

    def _on_comm_failure(self, batch: CommandBatch, detail: str) -> None:
        if batch.timer is not None:
            batch.timer.fail("ble", detail)
        self._drain_queue(COMM_SKIPPED)

    async def _dispatch(self, route: str, action: str, value: Any) -> dict:
        if route == "move":
            return await move_tools.execute({"hub": self.hub, "action": action, "value": value})
        if route == "camera":
            return await camera_tools.execute({"mode": value or "photo", "dry_run": True})
        if route == "sensor":
            return await sensor_tool.execute({"name": value or "distance", "dry_run": True})
        return {"status": "error", "detail": f"unknown route: {route}"}

    def _normalize_sequence_item(self, item: Any) -> tuple[bool, dict, str]:
        if not isinstance(item, dict):
            return False, {}, "item is not dict"

        raw_cmd = item.get("cmd")
        if not isinstance(raw_cmd, str):
            return False, {}, "cmd is not string"

        cmd = raw_cmd.strip()
        if not cmd:
            return False, {}, "cmd is empty"

        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        raw_value = parts[1].strip() if len(parts) == 2 else None

        if action not in self.action_rules:
            return False, {}, "unknown action"

        rule = self.action_rules[action]
        value_type = rule.value_type

        if value_type == "none":
            if raw_value is not None:
                return False, {}, "action does not accept value"
            return True, {"action": action, "value": None}, ""

        if raw_value is None:
            return False, {}, "missing value"

        if value_type == "int":
            value = self._parse_int(raw_value)
            if value is None:
                return False, {}, "value is not integer"
            if rule.min_value is not None and value < rule.min_value:
                return False, {}, "value below min"
            if rule.max_value is not None and value > rule.max_value:
                return False, {}, "value above max"
            return True, {"action": action, "value": value}, ""

        if value_type == "str":
            str_value = raw_value.lower()
            allowed = rule.allowed
            if allowed and str_value not in allowed:
                return False, {}, f"value not allowed: {str_value}"
            return True, {"action": action, "value": str_value}, ""

        return False, {}, "unknown value_type"

    @staticmethod
    def _parse_int(text: str) -> int | None:
        try:
            return int(float(text))
        except Exception:
            return None
