"""
PC 端和 Spike Hub (Pybricks) 的 BLE 通信, 协议 v2 (见 hub_protocol.py)。

- send(cmd): 等 Hub 完成上一条命令并报告就绪, 再发送; 超时时间按上一条命令的预计时长计算
- wait_done(): 等最后一条命令的最终回复 (DONE / STOPPED / ERR)
- send_stop(): 插队急停, 不等就绪; 300 ms 内没有确认就发送 Pybricks"停止程序"命令 (硬急停)
- 持续运动 (不带数值的 forward/left 等) 期间自动发送心跳, Hub 超过 1 秒收不到就停车
- 连接时进行版本握手, Hub 程序版本不对就拒绝发送运动命令
- simulate=True: 不用 BLE, 在本地模拟 Hub 的 v2 协议 (回复同样经过解析器)
"""

import argparse
import asyncio
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from pc.spike_communication import hub_protocol as hp
from pc.spike_communication.motion_timing import estimate_duration_s, ready_timeout_s

# Pybricks BLE 服务的命令和事件
PYBRICKS_CMD_STOP_PROGRAM = b"\x00"
PYBRICKS_CMD_START_PROGRAM = b"\x01"
PYBRICKS_CMD_WRITE_STDIN = b"\x06"
PYBRICKS_EVT_STDOUT = 0x01


class HubTimeoutError(Exception):
    """等待 Hub 超时 (上一条命令没做完、Hub 程序没启动或已经停止)。"""


class HubVersionError(Exception):
    """Hub 上运行的程序不是协议 v2, 需要用 Pybricks 重新下载。"""


@dataclass
class _Pending:
    cmd: str
    future: asyncio.Future
    acked: bool = False


class SpikeHub:
    def __init__(
        self,
        hub_name: str = "Pybricks Hub",
        simulate: bool = False,
        sim_time_scale: float = 0.0,
        sim_unresponsive: bool = False,
        startup_timeout: float = 30.0,
        version_timeout: float = 2.0,
        stop_confirm_timeout: float = 0.3,
        heartbeat_interval: float = 0.3,
        verbose: bool = False,
    ):
        self.UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
        self.HUB_NAME = hub_name
        self.client = None
        self.simulate = bool(simulate)

        # simulate 模式: 模拟执行时长 = 预计时长 * sim_time_scale (0 表示立即完成);
        # 超时时间也按同样比例缩放, 相当于让时间"快进"。
        # sim_unresponsive=True: 版本握手之后 Hub 不再回复任何命令 (用于测试超时和硬急停)。
        self.sim_time_scale = float(sim_time_scale)
        self.sim_unresponsive = bool(sim_unresponsive)

        self.startup_timeout = startup_timeout
        self.version_timeout = version_timeout
        self.stop_confirm_timeout = stop_confirm_timeout
        self.heartbeat_interval = heartbeat_interval
        self.verbose = verbose

        self.last_cmd: str | None = None  # 用来估算下一次等待就绪的超时
        self.ready_event = asyncio.Event()  # Hub 发来 rdy 时 set()
        self.rx_buffer = ""
        self.loop: asyncio.AbstractEventLoop | None = None

        self.gripper: tuple[int, int] | None = None  # (左, 右), 0=放下 90=抬起; None=未知
        self.protocol_version: int | None = None
        self.protocol_ok = False
        self.program_running = False

        self._pending: _Pending | None = None
        self._version_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None

        # simulate 模式下模拟的 Hub 状态
        self._sim_task: asyncio.TimerHandle | None = None
        self._sim_task_cmd: str | None = None
        self._sim_gripper = [0, 0]

    # ---------- 连接 ----------

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        self.rx_buffer = ""
        self.ready_event.clear()

        if self.simulate:
            print("[SIM] SpikeHub simulation mode: connected (no BLE)")
            self.program_running = True
            self._sim_emit("rdy")
        else:
            print("Searching hub...")
            device = await BleakScanner.find_device_by_name(self.HUB_NAME)
            if device is None:
                raise Exception(f"Cannot find {self.HUB_NAME}")
            self.client = BleakClient(device)
            await self.client.connect()
            await self.client.start_notify(self.UUID, self.handle_rx)
            self.program_running = True
            print("Connected.")

        await self._handshake()

    async def _handshake(self) -> None:
        if not self.ready_event.is_set():
            if not self.simulate:
                print("等待 Hub 程序启动（请按 Hub 中间按钮启动程序）...")
            try:
                await asyncio.wait_for(self.ready_event.wait(), self.startup_timeout)
            except TimeoutError as exc:
                raise HubTimeoutError(
                    "没有收到 Hub 的就绪信号：请确认 Hub 上的程序已经启动"
                ) from exc

        self.ready_event.clear()
        self._version_event.clear()
        self.protocol_version = None
        await self._write("version")
        try:
            await asyncio.wait_for(self._version_event.wait(), self.version_timeout)
        except TimeoutError:
            pass

        self.protocol_ok = self.protocol_version == hp.PROTOCOL_VERSION
        if not self.protocol_ok:
            found = self.protocol_version if self.protocol_version is not None else "无回答"
            print(
                f"Hub 程序需要更新：需要协议版本 {hp.PROTOCOL_VERSION}，Hub 的回答是 {found}。"
                "请用 Pybricks 重新下载 spike_hub/control_by_llm.py。在此之前不会发送任何运动命令。"
            )

    async def disconnect(self):
        self._stop_heartbeat()
        if self.simulate:
            print("[SIM] SpikeHub simulation mode: disconnected")
            return
        if self.client is not None:
            try:
                await self.client.stop_notify(self.UUID)
            except Exception:
                pass
            await self.client.disconnect()
            self.client = None

    # ---------- 接收 ----------

    def handle_rx(self, _, data):
        """BLE 通知回调: 只处理 Pybricks 的 stdout 事件, 交给事件循环线程解析。"""
        if not data or data[0] != PYBRICKS_EVT_STDOUT:
            return
        text = bytes(data[1:]).decode("utf-8", errors="replace")
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._feed, text)
        else:
            self._feed(text)

    def _feed(self, text: str) -> None:
        """
        按行拆分 Hub 的输出 (处理 BLE 分包)。
        也兼容旧版 Hub 程序: 它的 "rdy" / "OK" 后面没有换行, 例如 "OKrdy"。
        """
        self.rx_buffer += text
        while True:
            newline = self.rx_buffer.find("\n")
            legacy_rdy = self.rx_buffer.find("rdy")
            if legacy_rdy != -1 and (newline == -1 or legacy_rdy < newline):
                head = self.rx_buffer[:legacy_rdy]
                self.rx_buffer = self.rx_buffer[legacy_rdy + 3:]
                if self.rx_buffer.startswith("\n"):
                    self.rx_buffer = self.rx_buffer[1:]
                if head.strip():
                    self._process(hp.parse_reply(head))
                self._process(hp.HubReply("RDY", raw="rdy"))
                continue
            if newline == -1:
                break
            line = self.rx_buffer[:newline]
            self.rx_buffer = self.rx_buffer[newline + 1:]
            if line.strip():
                self._process(hp.parse_reply(line))

        # 防止极端情况下缓冲无限增长
        if len(self.rx_buffer) > 4096:
            self.rx_buffer = self.rx_buffer[-1024:]

    def _process(self, reply: hp.HubReply) -> None:
        if (self.verbose or reply.kind in ("ERR", "STOPPED", "OTHER")) and reply.kind != "RDY":
            print("Hub:", reply.raw)

        if reply.kind == "RDY":
            self.ready_event.set()
        elif reply.kind == "VERSION":
            try:
                self.protocol_version = int(reply.cmd)
            except ValueError:
                self.protocol_version = None
            self._version_event.set()
        elif reply.kind == "ACK":
            if self._pending is not None and self._pending.cmd == reply.cmd:
                self._pending.acked = True
        elif reply.kind in ("DONE", "STOPPED", "ERR"):
            if reply.gripper is not None:
                self.gripper = reply.gripper
            pending = self._pending
            if pending is not None and not pending.future.done() and pending.cmd == reply.cmd:
                pending.future.set_result(reply)

    # ---------- 发送 ----------

    async def _write(self, line: str) -> None:
        async with self._write_lock:
            if self.simulate:
                self._sim_receive(line)
                return
            await self.client.write_gatt_char(
                self.UUID, PYBRICKS_CMD_WRITE_STDIN + (line + "\n").encode(), response=True
            )

    def _ready_timeout(self) -> float:
        timeout = ready_timeout_s(self.last_cmd)
        if self.simulate and self.sim_time_scale > 0:
            # 模拟执行按比例加速, 超时也缩小; 但留 1 秒下限, 否则电脑忙时事件循环稍慢就会误判超时
            timeout = max(timeout * self.sim_time_scale, 1.0)
        return timeout

    async def _wait_ready(self) -> None:
        """等 Hub 完成上一条命令并报告就绪; 超时时间按上一条命令的预计时长计算。"""
        timeout = self._ready_timeout()
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
        except TimeoutError as exc:
            waiting_for = self.last_cmd or "Hub 启动"
            raise HubTimeoutError(
                f"等待 Hub 完成 '{waiting_for}' 超时 ({timeout:.1f}s)"
            ) from exc

    async def wait_idle(self) -> bool:
        """等 Hub 完成最后一条命令。完成返回 True, 超时返回 False。"""
        try:
            await self._wait_ready()
            return True
        except HubTimeoutError:
            return False

    async def wait_done(self) -> hp.HubReply | None:
        """等最后一条命令的最终回复 (DONE / STOPPED / ERR); 超时返回 None。"""
        pending = self._pending
        if pending is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), self._ready_timeout())
        except TimeoutError:
            return None

    async def send(self, cmd: str):
        """
        发送一条普通命令: 先等 Hub 完成上一条命令并报告就绪, 再发送, 发送完立即返回。
        结果用 wait_done() 获取。超时抛出 HubTimeoutError, 协议版本不对抛出 HubVersionError。
        """
        action = cmd.split()[0] if cmd.split() else ""
        if not self.program_running:
            raise HubTimeoutError("Hub 程序没有在运行（可能刚刚硬急停过），请确认安全后重新启动")
        if not self.protocol_ok and action not in hp.CONTROL_COMMANDS:
            raise HubVersionError("Hub 程序版本不对（需要协议 v2），拒绝发送运动命令")

        await self._wait_ready()

        self.ready_event.clear()
        self.last_cmd = cmd
        self._stop_heartbeat()
        self._pending = _Pending(cmd, asyncio.get_running_loop().create_future())
        await self._write(cmd)
        if hp.is_continuous(cmd):
            self._start_heartbeat()

    async def send_stop(self) -> bool:
        """
        插队急停: 不等 Hub 就绪, 立即发送 stop, 正在执行的动作会被打断。
        stop_confirm_timeout (默认 300 ms) 内没有收到确认, 就发送 Pybricks"停止程序"命令。
        返回 True 表示软停止已确认, False 表示用了硬急停。
        """
        loop = asyncio.get_running_loop()
        running = self._pending if self._pending and not self._pending.future.done() else None
        target = running or _Pending("stop", loop.create_future())
        self._pending = target
        self._stop_heartbeat()
        self.ready_event.clear()
        await self._write("stop")
        try:
            await asyncio.wait_for(asyncio.shield(target.future), self.stop_confirm_timeout)
            return True
        except TimeoutError:
            print(f"软停止没有在 {self.stop_confirm_timeout * 1000:.0f} ms 内确认，改用硬急停")
            await self.hard_stop()
            return False

    async def hard_stop(self) -> None:
        """Pybricks"停止程序"命令: 由固件直接处理, 即使 Hub 程序卡住也有效, 电机随程序结束停止。"""
        print("发送 Pybricks 停止程序命令（硬急停）")
        self.program_running = False
        self.gripper = None
        self.ready_event.clear()
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_result(hp.HubReply("STOPPED", cmd=pending.cmd, reason="hard_stop"))
        self._stop_heartbeat()
        if self.simulate:
            self._sim_stop_program()
        else:
            await self.client.write_gatt_char(self.UUID, PYBRICKS_CMD_STOP_PROGRAM, response=True)
        print("Hub 程序已停止。确认安全后可以调用 restart_program()（或按 Hub 按钮）重新启动；"
              "重新启动时夹爪会先转回放下位置。")

    async def restart_program(self) -> None:
        """重新启动 Hub 程序。必须先让用户确认: 启动时 Hub 会把夹爪转到放下位置。"""
        self.ready_event.clear()
        self.gripper = None
        if self.simulate:
            self.program_running = True
            self._sim_gripper = [0, 0]
            self._sim_emit("rdy")
        else:
            await self.client.write_gatt_char(self.UUID, PYBRICKS_CMD_START_PROGRAM, response=True)
            self.program_running = True
        await self._handshake()

    # ---------- 心跳 ----------

    def _start_heartbeat(self) -> None:
        self._heartbeat_task = asyncio.get_running_loop().create_task(self._heartbeat())

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat(self) -> None:
        pending = self._pending
        while pending is not None and not pending.future.done():
            await asyncio.sleep(self.heartbeat_interval)
            if pending.future.done():
                break
            await self._write("hb")

    # ---------- simulate: 本地模拟 Hub 的 v2 协议 ----------

    def _sim_emit(self, line: str) -> None:
        asyncio.get_running_loop().call_soon(self._feed, line + "\n")

    def _sim_gripper_text(self) -> str:
        return f"gl={self._sim_gripper[0]} gr={self._sim_gripper[1]}"

    def _sim_receive(self, line: str) -> None:
        parts = line.split()
        action = parts[0] if parts else ""
        if not self.program_running or action in ("", "hb", "bye"):
            return
        if action == "version":
            self._sim_emit(f"VERSION {hp.PROTOCOL_VERSION}")
            self._sim_emit("rdy")
            return
        if self.sim_unresponsive:
            return

        if self._sim_task is not None:  # 忙碌: 只接受 stop
            if action == "stop":
                self._sim_task.cancel()
                cmd, self._sim_task, self._sim_task_cmd = self._sim_task_cmd, None, None
                self._sim_emit(f"STOPPED {cmd} {self._sim_gripper_text()}")
                self._sim_emit("rdy")
            else:
                self._sim_emit(f"ERR {line} busy")
            return

        error = hp.validate_command(line)
        if error is not None:
            self._sim_emit(f"ERR {line} {error}")
            self._sim_emit("rdy")
            return

        print(f"[SIM] <- {line}")
        self._sim_emit(f"ACK {line}")
        if action == "stop":
            self._sim_emit(f"DONE stop {self._sim_gripper_text()}")
            self._sim_emit("rdy")
            return

        loop = asyncio.get_running_loop()
        self._sim_task_cmd = line
        if hp.is_continuous(line):
            self._sim_task = loop.call_later(3600, lambda: None)  # 持续运动, 直到 stop
            return
        delay = estimate_duration_s(line) * self.sim_time_scale
        self._sim_task = loop.call_later(delay, self._sim_finish, line)

    def _sim_finish(self, line: str) -> None:
        parts = line.split()
        action = parts[0]
        value = int(parts[1]) if len(parts) > 1 else None
        targets = {
            "gripper_up": (90, 90), "gripper_down": (0, 0), "gripper_pos": (value, value),
            "gripper_left_up": (90, None), "gripper_left_down": (0, None),
            "gripper_left_pos": (value, None),
            "gripper_right_up": (None, 90), "gripper_right_down": (None, 0),
            "gripper_right_pos": (None, value),
        }
        if action in targets:
            for i, target in enumerate(targets[action]):
                if target is not None:
                    self._sim_gripper[i] = target
        self._sim_task = None
        self._sim_task_cmd = None
        self._sim_emit(f"DONE {line} {self._sim_gripper_text()}")
        self._sim_emit("rdy")

    def _sim_stop_program(self) -> None:
        if self._sim_task is not None:
            self._sim_task.cancel()
        self._sim_task = None
        self._sim_task_cmd = None


# ----- CLI / demo runner -----
async def interactive_mode(spike: SpikeHub):
    print("尝试连接 Spike Hub...")
    await spike.connect()
    print("连接成功。输入命令（例如 forward 30、stop、version），输入 'exit' 退出。")
    print("调试命令：hardstop = 发送 Pybricks 停止程序命令（硬急停），"
          "restart = 重新启动 Hub 程序。")
    try:
        while True:
            cmd = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            cmd = cmd.strip()
            if not cmd:
                continue
            if cmd.lower() in ("exit", "quit"):
                break
            if cmd == "stop":
                t0 = time.perf_counter()
                confirmed = await spike.send_stop()
                elapsed_ms = (time.perf_counter() - t0) * 1000
                result = "已确认" if confirmed else "未确认，已硬急停"
                print(f"停止{result}，用时 {elapsed_ms:.0f} ms")
                continue
            if cmd == "hardstop":
                await spike.hard_stop()
                continue
            if cmd == "restart":
                await spike.restart_program()
                continue
            try:
                await spike.send(cmd)
            except (HubTimeoutError, HubVersionError) as exc:
                print("发送失败:", exc)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await spike.disconnect()
        print("已断开连接。")


def main():
    parser = argparse.ArgumentParser(
        description="SpikeHub PC-side runner (interactive)")
    parser.add_argument("--simulate", action="store_true",
                        help="启用 simulation 模式（不使用 BLE）")
    args = parser.parse_args()

    spike = SpikeHub(simulate=args.simulate, verbose=True)
    asyncio.run(interactive_mode(spike))


if __name__ == "__main__":
    main()
