"""
realtime 链路 (改动 voice-realtime-pipeline)。

FakeRealtimeServer 按实测的接口行为模拟服务端 (见 tests/fixtures/realtime/ 录制的事件):
- 每次回复只给一个函数调用, 回函数结果后再请求回复, 模型给出下一个;
- 提交音频后才有输入转写 (逐字的中间结果 + 最终结果);
- 回复可以被取消; 连接可以断开。
"""

import asyncio
import base64
import contextlib
import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from pc.agent.robot_agent import EXECUTED, REJECTED, RobotAgent
from pc.llm.realtime_tools import build_tools, tool_call_to_cmd
from pc.speech import vad as vad_mod
from pc.speech.realtime_session import RealtimeSession, RealtimeSessionError
from pc.utils.latency import CommandTimer
from pc.voice_to_robot import VoiceController, validate_launch
from skills import load_skill_registry

FIXTURES = Path(__file__).parent / "fixtures" / "realtime"
SPEECH = np.full(vad_mod.FRAME_SAMPLES, 1000, dtype=np.int16).tobytes()
SILENCE = bytes(vad_mod.FRAME_BYTES)
FORWARD_30 = {"transcript": "向前走30厘米", "calls": [("forward", '{"distance_cm":30}')]}


async def settle(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- 假服务端

class FakeRealtimeServer:
    """
    turns: 每句话 (每次提交) 的脚本 {"transcript": str, "calls": [(name, args_json)], "text": str}。
    vad=True 时模拟服务端 VAD: 非零音频算人声, 之后静音 silence_ms 就自动提交。
    hold=True 时回复先不发出, 等 release() (用来测试"转写先到, 模型回复后到")。
    """

    def __init__(self, turns=None, vad=False, silence_ms=700, fail_connect=0, hold=False,
                 transcript_first=True) -> None:
        self.script = list(turns or [])
        self.vad = vad
        self.silence_samples = silence_ms * 24
        self.fail_connect = fail_connect  # 前 N 次连接失败
        self.hold = hold
        self.transcript_first = transcript_first
        self.connects = 0
        self.appended = 0
        self.commits = 0
        self.responses = 0
        self.cancels = 0
        self.outputs: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.context: list[str] = []  # 对话上下文里的条目
        self.session_config = None
        self.conn: FakeConn | None = None
        self._held: list = []
        self._uncommitted = 0
        self._speaking = False
        self._silence = 0
        self._items = 0
        self._queue: list[dict] = []  # 已提交、还没回复完的句子
        self._active: str | None = None  # 正在生成的回复 id

    def connect(self, _api_key, _model):
        return FakeManager(self)

    # --- 客户端 -> 服务端 ---

    def on_append(self, audio_b64: str) -> None:
        self.appended += 1
        pcm = base64.b64decode(audio_b64)
        samples = len(pcm) // 2
        self._uncommitted += samples
        if not self.vad:
            return
        voiced = any(pcm)
        if voiced:
            if not self._speaking:
                self._speaking = True
                self.emit({"type": "input_audio_buffer.speech_started"})
            self._silence = 0
        elif self._speaking:
            self._silence += samples
            if self._silence >= self.silence_samples:
                self._speaking = False
                self.emit({"type": "input_audio_buffer.speech_stopped"})
                self._commit()

    def on_commit(self) -> None:
        self.commits += 1
        if self._uncommitted < 2400:  # 不到 100 ms
            self.emit({"type": "error", "error": {
                "message": "buffer too small", "code": "input_audio_buffer_commit_empty"}})
            return
        self._commit()

    def _commit(self) -> None:
        self._uncommitted = 0
        self._items += 1
        item_id = f"item_{self._items}"
        spec = dict(self.script.pop(0)) if self.script else {"transcript": "", "calls": []}
        spec["item_id"] = item_id
        spec["calls"] = list(spec.get("calls") or [])
        self._queue.append(spec)
        self.emit({"type": "input_audio_buffer.committed", "item_id": item_id})
        self._add_item(item_id)
        if self.transcript_first:
            self._transcribe(spec)

    def _add_item(self, item_id: str) -> None:
        self.context.append(item_id)
        self.emit({"type": "conversation.item.added", "item": {"id": item_id}})

    def on_delete(self, item_id: str) -> None:
        self.deleted.append(item_id)
        self.context.remove(item_id)
        self.emit({"type": "conversation.item.deleted", "item_id": item_id})

    def _transcribe(self, spec) -> None:
        text = ""
        for ch in spec.get("transcript", ""):
            text += ch
            self.emit({"type": "conversation.item.input_audio_transcription.delta",
                       "item_id": spec["item_id"], "delta": ch})
        self.emit({"type": "conversation.item.input_audio_transcription.completed",
                   "item_id": spec["item_id"], "transcript": text})

    def on_response_create(self) -> None:
        self.responses += 1
        rid = f"resp_{self.responses}"
        self._active = rid
        events = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}}]
        spec = self._queue[0] if self._queue else None
        if spec is not None and spec["calls"]:
            name, args = spec["calls"].pop(0)
            call_id = f"call_{self.responses}"
            self.context.append(f"fc_{call_id}")
            events.append({"type": "conversation.item.added", "item": {"id": f"fc_{call_id}"}})
            events.append({"type": "response.function_call_arguments.done", "response_id": rid,
                           "call_id": call_id, "name": name, "arguments": args})
            output = [{"type": "function_call", "name": name, "arguments": args,
                       "call_id": call_id}]
        else:
            text = spec.get("text", "") if spec else ""
            output = [{"type": "message", "text": text}] if text else []
            if spec is not None:
                self._queue.pop(0)
        events.append({"type": "response.done",
                       "response": {"id": rid, "status": "completed", "output": output}})
        if not self.transcript_first and spec is not None and not spec.get("_transcribed"):
            spec["_transcribed"] = True
            after = [events.pop()]
            self._emit_or_hold(events)
            self._transcribe(spec)
            self._emit_or_hold(after)
            return
        self._emit_or_hold(events)

    def _emit_or_hold(self, events) -> None:
        if self.hold:
            self._held.append(events)
        else:
            for e in events:
                self.emit(e)

    def release(self) -> None:
        held, self._held = self._held, []
        for events in held:
            for e in events:
                self.emit(e)

    def on_cancel(self) -> None:
        self.cancels += 1
        if self._held:
            # 回复还没发出: 只发"已创建"和"已取消"
            events = self._held.pop()
            rid = events[0]["response"]["id"]
            self.emit(events[0])
            self.emit({"type": "response.done",
                       "response": {"id": rid, "status": "cancelled", "output": []}})
            if self._queue:
                self._queue.pop(0)
        else:
            self.emit({"type": "error", "error": {"message": "no active response",
                                                  "code": "response_cancel_not_active"}})

    def on_item(self, item) -> None:
        if item.get("type") == "function_call_output":
            self.outputs.append((item["call_id"], item["output"]))
            self._add_item(f"out_{item['call_id']}")

    # --- 服务端 -> 客户端 ---

    def emit(self, event: dict) -> None:
        if self.conn is not None:
            self.conn.events.put_nowait(event)

    def disconnect(self) -> None:
        if self.conn is not None:
            self.conn.events.put_nowait(ConnectionError("connection closed"))


class FakeConn:
    def __init__(self, server: FakeRealtimeServer) -> None:
        self.server = server
        self.events: asyncio.Queue = asyncio.Queue()

        class Session:
            async def update(self, session):
                server.session_config = session

        class Buffer:
            async def append(self, audio):
                server.on_append(audio)

            async def commit(self):
                server.on_commit()

            async def clear(self):
                pass

        class Response:
            async def create(self):
                server.on_response_create()

            async def cancel(self):
                server.on_cancel()

        class Item:
            async def create(self, item):
                server.on_item(item)

            async def delete(self, item_id):
                server.on_delete(item_id)

        self.session = Session()
        self.input_audio_buffer = Buffer()
        self.response = Response()
        self.conversation = SimpleNamespace(item=Item())

    async def recv(self):
        item = await self.events.get()
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(**item)


class FakeManager:
    def __init__(self, server: FakeRealtimeServer) -> None:
        self.server = server

    async def __aenter__(self):
        self.server.connects += 1
        if self.server.fail_connect > 0:
            self.server.fail_connect -= 1
            raise ConnectionError("connect refused")
        self.server.conn = FakeConn(self.server)
        return self.server.conn

    async def __aexit__(self, *exc):
        return False


class ChunkSource:
    """假的按空格音频: 只在"按住"期间产出音频块。"""

    def __init__(self, n_chunks: int = 10, voiced: bool = True) -> None:
        chunk = np.full(960, 1000 if voiced else 0, dtype=np.int16)
        self.chunks = [chunk.tobytes()] * n_chunks
        self.audio = np.concatenate([chunk] * n_chunks) if n_chunks else np.zeros(0, np.int16)

    async def __aiter__(self):
        for c in self.chunks:
            yield c
            await asyncio.sleep(0)


class DummyHub:
    simulate = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


def make_controller(server: FakeRealtimeServer, tmp: str, input_mode: str = "ptt"):
    vc = VoiceController(spike_simulation=True, upload_log_path=str(Path(tmp) / "uploads.jsonl"))
    hub = DummyHub()
    vc.spike = hub
    vc.robot_agent = RobotAgent(hub)
    vc.audio_client.api_key = "test"
    vc.audio_client.transcribe_openai = AsyncMock(return_value="")
    vc.audio_client.transcribe_whisper = AsyncMock(return_value="")
    vc.audio_client.start_tone = lambda kind: 0.3
    vc.realtime_connect = server.connect
    vc.pipeline = "realtime"
    vc._input_mode = input_mode
    vc.upload_guard.set_mode(input_mode, str(Path(tmp) / "uploads.jsonl"))
    vc.realtime = vc._make_realtime(input_mode)
    vc._temp_wav_path = lambda: str(Path(tmp) / "seg.wav")
    return vc, hub


# ---------------------------------------------------------------- 1.1 工具定义

class ToolDefinitionTest(unittest.TestCase):
    def test_every_skill_action_has_a_tool_with_matching_ranges(self):
        registry = load_skill_registry()
        tools = {t["name"]: t for t in build_tools(registry)}
        self.assertEqual(set(tools), set(registry.list_actions()))
        for action, rule in registry.actions.items():
            with self.subTest(action=action):
                params = tools[action]["parameters"]
                self.assertFalse(params["additionalProperties"])
                self.assertTrue(tools[action]["description"])
                if rule.value_type == "none":
                    self.assertEqual(params["properties"], {})
                    self.assertEqual(params["required"], [])
                    continue
                prop = params["properties"][rule.arg_key]
                self.assertEqual(params["required"], [rule.arg_key])
                if rule.value_type == "int":
                    self.assertEqual(prop["type"], "integer")
                    self.assertEqual(prop["minimum"], rule.min_value)
                    self.assertEqual(prop["maximum"], rule.max_value)
                else:
                    self.assertEqual(prop["enum"], list(rule.allowed))

    def test_examples(self):
        tools = {t["name"]: t for t in build_tools()}
        self.assertEqual(tools["forward"]["parameters"]["properties"],
                         {"distance_cm": {"type": "integer", "minimum": 0, "maximum": 10000}})
        angle = tools["gripper_left_pos"]["parameters"]["properties"]["angle_deg"]
        self.assertEqual(angle["maximum"], 90)

    def test_valid_calls(self):
        self.assertEqual(tool_call_to_cmd("forward", '{"distance_cm": 30}').cmd, "forward 30")
        spaced = '{  \n "distance_cm": 50 \n} \n'  # 实测模型输出的格式
        self.assertEqual(tool_call_to_cmd("forward", spaced).cmd, "forward 50")
        self.assertEqual(tool_call_to_cmd("left", {"angle_deg": 90.0}).cmd, "left 90")
        self.assertEqual(tool_call_to_cmd("gripper_up", "{}").cmd, "gripper_up")
        self.assertEqual(tool_call_to_cmd("gripper_up", "").cmd, "gripper_up")
        self.assertEqual(tool_call_to_cmd("camera", '{"mode": "Photo"}').cmd, "camera photo")

    def test_invalid_calls_are_rejected_with_reason(self):
        cases = {
            ("forward", '{"distance_cm": 20000}'): "超过最大值",
            ("forward", '{"distance_cm": -5}'): "小于最小值",
            ("forward", '{"distance_cm": "abc"}'): "必须是整数",
            ("forward", '{"distance_cm": 2.5}'): "必须是整数",
            ("forward", '{"distance_cm": true}'): "必须是整数",
            ("forward", "{}"): "缺少参数",
            ("forward", '{"distance_cm": 3, "speed": 1}'): "多余的参数",
            ("forward", '{"distance_cm": 3'): "JSON",
            ("gripper_up", '{"angle_deg": 3}'): "不需要参数",
            ("fly", "{}"): "未知的动作",
            ("camera", '{"mode": "panorama"}'): "不在可选值内",
            ("gripper_pos", '{"angle_deg": 120}'): "超过最大值",
        }
        for (name, args), reason in cases.items():
            with self.subTest(name=name, args=args):
                result = tool_call_to_cmd(name, args)
                self.assertFalse(result.ok)
                self.assertIn(reason, result.error)


# ---------------------------------------------------------------- 1.2 会话客户端

class RecordingEvents:
    def __init__(self) -> None:
        self.log: list[tuple] = []

    def __getattr__(self, name):
        if not name.startswith("on_"):
            raise AttributeError(name)

        async def record(*args):
            self.log.append((name, *args))
        return record


def load_fixture(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class SessionFixtureTest(unittest.IsolatedAsyncioTestCase):
    async def replay(self, name: str) -> list[tuple]:
        events = RecordingEvents()
        session = RealtimeSession(events, "x", [])
        for e in load_fixture(name):
            await session._handle(SimpleNamespace(**e))
        return events.log

    async def test_recorded_function_calls(self):
        log = await self.replay("l2_forward_right")
        calls = [(n, json.loads(a)) for (kind, _r, _c, n, a) in
                 [e for e in log if e[0] == "on_function_call"]]
        self.assertEqual(calls, [("forward", {"distance_cm": 50}), ("right", {"angle_deg": 90})])
        # 每次回复一个调用, 最后一次回复没有调用 (模型说"完成")
        done = [e for e in log if e[0] == "on_response_done"]
        self.assertEqual([e[4] for e in done], [1, 1, 0])
        self.assertTrue(all(e[2] == "completed" for e in done))

    async def test_recorded_transcript_partials_accumulate(self):
        log = await self.replay("l1_forward_30")
        partials = [e[2] for e in log if e[0] == "on_transcript_delta"]
        self.assertTrue(partials[0])
        for a, b in itertools.pairwise(partials):
            self.assertTrue(b.startswith(a))
        final = [e[2] for e in log if e[0] == "on_transcript_done"]
        self.assertEqual(len(final), 1)
        self.assertIn("厘米", final[0])

    async def test_recorded_stop_transcript_arrives_before_model_call(self):
        events = load_fixture("l1_stop")
        first_stop_text = next(e["t_ms"] for e in events
                               if e["type"].endswith("transcription.delta") and "停" in e["delta"])
        first_call = next(e["t_ms"] for e in events
                          if e["type"] == "response.function_call_arguments.done")
        self.assertLess(first_stop_text, first_call)

    async def test_disconnect_reconnects(self):
        server = FakeRealtimeServer()
        events = RecordingEvents()
        session = RealtimeSession(events, "x", [], connect=server.connect)
        await session.start()
        server.disconnect()
        await asyncio.sleep(0.3)
        self.assertEqual(server.connects, 2)
        self.assertIn(("on_reconnected",), events.log)
        self.assertTrue(session.is_open)
        self.assertIsNotNone(server.session_config)  # 重连后重新配置会话
        await session.close()

    async def test_reconnect_failure_reports(self):
        server = FakeRealtimeServer()
        events = RecordingEvents()
        session = RealtimeSession(events, "x", [], connect=server.connect)
        await session.start()
        server.fail_connect = 5
        server.disconnect()
        await asyncio.sleep(0.4)
        failed = [e for e in events.log if e[0] == "on_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("重连失败", failed[0][1])
        self.assertFalse(session.is_open)
        with self.assertRaises(RealtimeSessionError):
            await session.send_audio(b"\x00\x00")
        await session.close()

    async def test_start_failure_raises(self):
        server = FakeRealtimeServer(fail_connect=1)
        session = RealtimeSession(RecordingEvents(), "x", [], connect=server.connect)
        with self.assertRaisesRegex(RealtimeSessionError, "无法建立"):
            await session.start()

    async def test_session_config_text_only_with_tools_and_transcription(self):
        server = FakeRealtimeServer()
        tools = build_tools()
        session = RealtimeSession(RecordingEvents(), "说明", tools, connect=server.connect)
        await session.start()
        cfg = server.session_config
        self.assertEqual(cfg["output_modalities"], ["text"])
        self.assertEqual(len(cfg["tools"]), len(tools))
        self.assertIsNone(cfg["audio"]["input"]["turn_detection"])
        self.assertEqual(cfg["audio"]["input"]["transcription"]["language"], "zh")
        await session.close()


# ---------------------------------------------------------------- 2.1 / 3.x 按空格

class PttRealtimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def run_turn(self, vc, source=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            turn = await vc.process_realtime(source or ChunkSource())
            if turn is not None:
                await asyncio.wait_for(turn.wait(), 2)
                await turn.batch.wait()
            await vc._drain_reports()
        return turn, out.getvalue()

    async def test_no_audio_is_sent_without_pressing(self):
        server = FakeRealtimeServer()
        vc, _hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        await asyncio.sleep(0.05)
        self.assertEqual(server.appended, 0)
        self.assertEqual(server.commits, 0)
        await vc.realtime.close()

    async def test_audio_only_while_pressed_then_commit(self):
        server = FakeRealtimeServer([FORWARD_30])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        turn, out = await self.run_turn(vc, ChunkSource(n_chunks=12))
        self.assertEqual(server.appended, 12)
        self.assertEqual(server.commits, 1)
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertEqual(turn.timer.route, "realtime")
        self.assertIn("Realtime 调用: forward 30", out)
        await vc.realtime.close()

    async def test_multiple_calls_in_order_with_function_results(self):
        calls = [("forward", '{"distance_cm":50}'), ("right", '{"angle_deg":90}'),
                 ("gripper_up", "{}")]
        server = FakeRealtimeServer([{"transcript": "前进50厘米然后右转再抬起夹爪", "calls": calls,
                                      "text": "完成"}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        turn, _ = await self.run_turn(vc)
        self.assertEqual(hub.commands, ["forward 50", "right 90", "gripper_up"])
        self.assertEqual([o for _, o in server.outputs],
                         [f"已入队（这句话第 {i} 个动作）" for i in (1, 2, 3)])
        self.assertEqual(server.responses, 4)  # 3 个调用 + 最后一次没有调用
        self.assertEqual(turn.batch.executed, ["forward 50", "right 90", "gripper_up"])
        await vc.realtime.close()

    async def test_first_call_is_enqueued_before_second_arrives(self):
        server = FakeRealtimeServer([{"transcript": "前进30再左转90",
                                      "calls": [("forward", '{"distance_cm":30}'),
                                                ("left", '{"angle_deg":90}')]}], hold=True)
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        with contextlib.redirect_stdout(io.StringIO()):
            turn = await vc.process_realtime(ChunkSource())
            await settle()
            server.release()  # 只放出第一次回复
            await settle()
            self.assertEqual(hub.commands, ["forward 30"])
            self.assertFalse(turn.done.is_set())
            for _ in range(3):
                server.release()
                await settle()
            await asyncio.wait_for(turn.wait(), 2)
            await turn.batch.wait()
        self.assertEqual(hub.commands, ["forward 30", "left 90"])
        await vc.realtime.close()

    async def test_invalid_call_is_rejected_and_not_sent(self):
        server = FakeRealtimeServer([{"transcript": "前进两百米", "calls": [
            ("forward", '{"distance_cm":20000}'), ("fly", "{}"), ("left", '{"angle_deg":90}')]}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        turn, out = await self.run_turn(vc)
        self.assertEqual(hub.commands, ["left 90"])
        outputs = [o for _, o in server.outputs]
        self.assertTrue(outputs[0].startswith("被拒绝：") and "超过最大值" in outputs[0])
        self.assertIn("未知的动作", outputs[1])
        self.assertEqual(outputs[2], "已入队（这句话第 1 个动作）")
        statuses = [r.status for r in turn.batch.records]
        self.assertEqual(statuses, [REJECTED, REJECTED, EXECUTED])
        self.assertIn("被拒绝", out)
        await vc.realtime.close()

    async def test_stop_in_transcript_stops_before_model_replies(self):
        # 模型回复被扣住: 转写里的"停"先到, 立即急停并取消回复; 之后不再执行任何调用
        server = FakeRealtimeServer(
            [{"transcript": "停", "calls": [("forward", '{"distance_cm":30}')]}], hold=True)
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        with contextlib.redirect_stdout(io.StringIO()):
            turn = await vc.process_realtime(ChunkSource())
            await asyncio.wait_for(turn.wait(), 2)
            self.assertEqual(hub.commands, ["stop"])
            self.assertEqual(server.cancels, 1)
            server.release()
            await settle()
            await vc._drain_reports()
        self.assertEqual(hub.commands, ["stop"])
        self.assertEqual(turn.timer.route, "stop")
        self.assertEqual(turn.timer.commands, ["stop"])
        await vc.realtime.close()

    async def test_stop_while_previous_sentence_still_generating(self):
        # 第一句"走正方形"的调用还在生成, 第二句说"停": 第一句剩下的调用全部作废
        square = [("forward", '{"distance_cm":30}'), ("left", '{"angle_deg":90}')] * 4
        server = FakeRealtimeServer([{"transcript": "走一个正方形", "calls": square},
                                     {"transcript": "停下", "calls": []}], hold=True)
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        with contextlib.redirect_stdout(io.StringIO()):
            first = await vc.process_realtime(ChunkSource())
            await settle()
            server.release()
            await settle()
            server.release()
            await settle()
            self.assertEqual(hub.commands, ["forward 30", "left 90"])
            second = await vc.process_realtime(ChunkSource())
            await asyncio.wait_for(second.wait(), 2)
            server.release()
            await settle()
            await asyncio.wait_for(first.wait(), 2)
            for _ in range(10):
                server.release()
                await settle()
            await vc._drain_reports()
        self.assertEqual(hub.commands, ["forward 30", "left 90", "stop"])
        await vc.realtime.close()

    async def test_each_sentence_starts_with_empty_context(self):
        # 每句话独立: 开始处理第二句之前, 第一句的音频、函数调用和结果都从上下文里删掉
        calls = [("forward", '{"distance_cm":30}'), ("left", '{"angle_deg":90}')]
        server = FakeRealtimeServer([{"transcript": "前进再左转", "calls": calls},
                                     {"transcript": "后退十厘米",
                                      "calls": [("backward", '{"distance_cm":10}')]}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        await self.run_turn(vc)
        first_items = list(server.context)
        self.assertEqual(len(first_items), 5)  # 音频 + 2 个调用 + 2 个结果
        await self.run_turn(vc)
        self.assertEqual(server.deleted, first_items)
        self.assertEqual(hub.commands, ["forward 30", "left 90", "backward 10"])
        await vc.realtime.close()

    async def test_keep_context_option(self):
        server = FakeRealtimeServer([FORWARD_30, FORWARD_30])
        vc, _hub = make_controller(server, self.tmp.name)
        vc.realtime.keep_context = True
        await vc.realtime.open()
        await self.run_turn(vc)
        await self.run_turn(vc)
        self.assertEqual(server.deleted, [])
        await vc.realtime.close()

    async def test_stop_reported_by_both_paths_is_sent_once(self):
        # 转写和本地识别先后报告同一个"停": 只发一次 stop
        server = FakeRealtimeServer()
        vc, hub = make_controller(server, self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            await vc.realtime.stop(None, "识别到：停")
            await vc.realtime.stop(None, "本地识别：急停")
        self.assertEqual(hub.commands, ["stop"])

    async def test_local_stop_then_transcript_stop_is_sent_once(self):
        # 本地先报 (还没有对应的句子), 转写后报: 这句话记为急停, 回复作废, stop 只发一次
        server = FakeRealtimeServer(
            [{"transcript": "急停", "calls": [("forward", '{"distance_cm":30}')]}], hold=True)
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        with contextlib.redirect_stdout(io.StringIO()):
            await vc.realtime.stop(None, "本地识别：急停")
            turn = await vc.process_realtime(ChunkSource())
            await settle()
            server.release()
            await asyncio.wait_for(turn.wait(), 2)
            await settle()
        self.assertEqual(hub.commands, ["stop"])
        self.assertEqual(turn.timer.commands, ["stop"])
        await vc.realtime.close()

    async def test_model_calling_stop_is_an_emergency_stop(self):
        server = FakeRealtimeServer([{"transcript": "", "calls": [("stop", "{}")]}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        turn, _ = await self.run_turn(vc)
        self.assertEqual(hub.commands, ["stop"])
        self.assertEqual(turn.timer.route, "stop")
        await vc.realtime.close()

    async def test_prompt_echo_transcript_does_not_stop(self):
        from pc.llm.audio_utils import STT_PROMPT
        server = FakeRealtimeServer([{"transcript": STT_PROMPT, "calls": []}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        await self.run_turn(vc)
        self.assertEqual(hub.commands, [])
        await vc.realtime.close()

    async def test_too_short_audio(self):
        server = FakeRealtimeServer()
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            turn = await vc.process_realtime(ChunkSource(n_chunks=1))  # 40 ms
            await settle()
        self.assertTrue(turn.done.is_set())
        self.assertIn("录音太短", out.getvalue())
        self.assertEqual(hub.commands, [])
        await vc.realtime.close()

    async def test_model_text_reply_without_call(self):
        server = FakeRealtimeServer(
            [{"transcript": "唱首歌", "calls": [], "text": "我只能控制小车"}])
        vc, hub = make_controller(server, self.tmp.name)
        await vc.realtime.open()
        _, out = await self.run_turn(vc)
        self.assertEqual(hub.commands, [])
        self.assertIn("无法识别（模型：我只能控制小车）", out)
        await vc.realtime.close()

    async def test_runaway_model_is_capped(self):
        server = FakeRealtimeServer(
            [{"transcript": "转圈", "calls": [("left", '{"angle_deg":10}')] * 40}])
        vc, hub = make_controller(server, self.tmp.name)
        vc.realtime.max_calls = 5
        await vc.realtime.open()
        await self.run_turn(vc)
        self.assertEqual(len(hub.commands), 5)
        await vc.realtime.close()

    # --- 3.3 失败回退 ---

    async def test_connect_failure_at_start_switches_to_stream(self):
        server = FakeRealtimeServer(fail_connect=1)
        vc, _hub = make_controller(server, self.tmp.name)
        vc.pipeline = "realtime"
        vc.audio_client.record_push_to_talk = AsyncMock(side_effect=AssertionError)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                await vc.realtime.open()
            except RealtimeSessionError as exc:
                vc._switch_to_stream(str(exc))
        self.assertEqual(vc.pipeline, "stream")
        self.assertIn("realtime 链路不可用：无法建立 realtime 会话", out.getvalue())
        self.assertIn("已切换到 stream 链路", out.getvalue())

    async def test_run_falls_back_when_session_cannot_start(self):
        # 用 run() 走一遍: realtime 连不上 -> 切换到 stream, 这一句用流式识别处理
        server = FakeRealtimeServer(fail_connect=1)
        vc, hub = make_controller(server, self.tmp.name)
        streamed = []

        async def fake_stream(source, llm_model=None, **_kw):
            streamed.append(source)

        vc.process_stream = fake_stream
        import pc.voice_to_robot as vtr
        orig = vtr.PushToTalkSource
        vtr.PushToTalkSource = ChunkSource
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                await vc.run(input_mode="ptt", pipeline="realtime", run_once=True)
        finally:
            vtr.PushToTalkSource = orig
        self.assertEqual(len(streamed), 1)
        self.assertEqual(vc.pipeline, "stream")
        self.assertIn("已切换到 stream 链路", out.getvalue())
        self.assertEqual(hub.commands, [])

    async def test_disconnect_midway_falls_back_with_whole_sentence(self):
        server = FakeRealtimeServer()
        vc, hub = make_controller(server, self.tmp.name)
        vc.audio_client.transcribe_openai = AsyncMock(return_value="向前走30厘米")
        await vc.realtime.open()
        server.fail_connect = 5
        server.disconnect()
        await asyncio.sleep(0.4)  # 重连失败 -> 切换到 stream
        self.assertEqual(vc.pipeline, "stream")
        # 正好在断开时说的一句话: 用完整录音整句识别
        vc.pipeline = "realtime"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            turn = await vc.process_realtime(ChunkSource())
            await vc._drain_reports()
            await asyncio.sleep(0.05)
        self.assertIsNone(turn)
        self.assertEqual(vc.pipeline, "stream")
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertIn("整句识别", out.getvalue())

    # --- 3.4 耗时记录 ---

    async def test_latency_record_has_same_fields_as_stream(self):
        server = FakeRealtimeServer([FORWARD_30])
        vc, _hub = make_controller(server, self.tmp.name)
        log = Path(self.tmp.name) / "latency.jsonl"
        vc.latency_logger.path = log
        await vc.realtime.open()
        await self.run_turn(vc)
        record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        stream_record = CommandTimer(input_mode="mic-stream", route="fast_path").to_record()
        self.assertEqual(set(record), set(stream_record))
        self.assertEqual(record["route"], "realtime")
        self.assertEqual(record["input_mode"], "ptt-realtime")
        self.assertEqual(record["commands"], ["forward 30"])
        self.assertEqual(record["text"], "向前走30厘米")
        self.assertIsNotNone(record["first_action_ms"]["enqueued"])
        self.assertIsNotNone(record["durations_ms"]["route"])
        await vc.realtime.close()


# ---------------------------------------------------------------- 2.2 唤醒词模式

def frames(seconds: float, voiced: bool) -> list[bytes]:
    return [SPEECH if voiced else SILENCE] * round(seconds * 1000 / vad_mod.FRAME_MS)


async def play(fs: list[bytes]):
    for f in fs:
        yield f
        for _ in range(3):
            await asyncio.sleep(0)


class FakeLocalTranscriber:
    def __init__(self, texts) -> None:
        self.texts = list(texts)

    def load(self) -> None:
        pass

    def transcribe(self, _audio16k) -> str:
        return self.texts.pop(0) if self.texts else ""


class WakeRealtimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, local_texts, turns=None):
        server = FakeRealtimeServer(turns, vad=True)
        vc, hub = make_controller(server, self.tmp.name, input_mode="wake")
        vc.local_transcriber = FakeLocalTranscriber(local_texts)
        # 对话窗口按音频时间计时 (模拟时钟), 不受测试运行速度影响
        holder = {}
        gate = vc.conversation_gate
        gate._clock = lambda: holder["loop"].audio_time
        orig_run = vc.run_wake

        async def run_wake(fs, vad):
            # WakeLoop 在 run_wake 里创建, 创建后把它交给模拟时钟
            import pc.voice_to_robot as vtr
            real_cls = vtr.WakeLoop

            def make_loop(*a, **kw):
                loop = real_cls(*a, **kw)
                holder["loop"] = loop
                return loop

            vtr.WakeLoop = make_loop
            try:
                await orig_run(fs, vad=vad)
            finally:
                vtr.WakeLoop = real_cls

        return vc, hub, server, run_wake

    async def run_frames(self, run_wake, fs, vc):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await run_wake(play(fs), vad=any)
            await vc._drain_reports()
        return out.getvalue()

    async def test_no_session_before_wake(self):
        vc, hub, server, run_wake = self.make(["今天天气不错", "我们去吃饭吧"])
        chat = frames(1.5, True) + frames(1, False) + frames(1.5, True) + frames(1, False)
        await self.run_frames(run_wake, chat, vc)
        self.assertEqual(server.connects, 0)
        self.assertEqual(server.appended, 0)
        self.assertEqual(vc.upload_guard.records, [])
        self.assertEqual(hub.commands, [])

    async def test_session_opens_after_wake_and_commands_run(self):
        turns = [{"transcript": "小蓝小蓝", "calls": []},
                 {"transcript": "前进30厘米", "calls": [("forward", '{"distance_cm":30}')]},
                 {"transcript": "左转90度", "calls": [("left", '{"angle_deg":90}')]}]
        vc, hub, server, run_wake = self.make(["小蓝小蓝"], turns)
        fs = (frames(1.0, True) + frames(1.0, False) + frames(1.0, True) + frames(1.5, False)
              + frames(1.0, True) + frames(1.5, False))
        out = await self.run_frames(run_wake, fs, vc)
        self.assertEqual(server.connects, 1)
        self.assertEqual(hub.commands, ["forward 30", "left 90"])
        self.assertEqual(len(vc.upload_guard.records), 1)
        self.assertEqual(vc.upload_guard.records[0]["kind"], "realtime")
        self.assertTrue(vc.upload_guard.records[0]["window_open"])
        self.assertIn("Realtime 调用: forward 30", out)
        # 唤醒词所在的片段之前的音频没有上传: 上传的帧数 ≈ 唤醒后的全部帧
        self.assertLessEqual(server.appended, len(fs))

    async def test_session_closes_after_20s_idle_and_no_more_upload(self):
        turns = [{"transcript": "小蓝小蓝前进30厘米", "calls": [("forward", '{"distance_cm":30}')]}]
        vc, hub, server, run_wake = self.make(["小蓝小蓝"], turns)
        before = frames(1.0, True) + frames(1.0, False)
        idle = frames(21.0, False)
        after = frames(1.0, True) + frames(1.0, False)
        await self.run_frames(run_wake, before + idle + after, vc)
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertFalse(vc.conversation_gate.is_active())
        # 20 秒窗口 + 唤醒那句话: 21 秒静音之后的音频没有上传
        uploaded_s = server.appended * vad_mod.FRAME_MS / 1000
        self.assertLess(uploaded_s, 1.0 + 1.0 + 20.5)
        self.assertGreater(uploaded_s, 20.0)
        self.assertEqual(server.connects, 1)

    async def test_exit_phrase_closes_session(self):
        turns = [{"transcript": "小蓝小蓝", "calls": []},
                 {"transcript": "结束对话", "calls": []}]
        vc, hub, server, run_wake = self.make(["小蓝小蓝", "前进"], turns)
        fs = (frames(1.0, True) + frames(1.0, False) + frames(1.0, True) + frames(1.5, False)
              + frames(1.0, True) + frames(1.0, False))
        out = await self.run_frames(run_wake, fs, vc)
        self.assertIn("已结束对话", out)
        self.assertFalse(vc.conversation_gate.is_active())
        self.assertEqual(hub.commands, [])
        uploaded_s = server.appended * vad_mod.FRAME_MS / 1000
        self.assertLess(uploaded_s, 5.0)  # 最后一句 (要重新唤醒) 没有上传

    async def test_local_stop_in_window(self):
        turns = [{"transcript": "小蓝小蓝", "calls": []}]
        vc, hub, _server, run_wake = self.make(["小蓝小蓝", "急停"], turns)
        fs = frames(1.0, True) + frames(1.0, False) + frames(0.6, True) + frames(1.0, False)
        await self.run_frames(run_wake, fs, vc)
        self.assertIn("stop", hub.commands)

    async def test_connect_failure_after_wake_falls_back_to_stream(self):
        turns = []
        vc, hub, server, run_wake = self.make(["小蓝小蓝"], turns)
        server.fail_connect = 1
        vc.audio_client.transcribe_openai = AsyncMock(return_value="小蓝小蓝，前进30厘米")
        out = await self.run_frames(run_wake, frames(2.0, True) + frames(1.0, False), vc)
        self.assertEqual(vc.pipeline, "stream")
        self.assertIn("已切换到 stream 链路", out)
        self.assertEqual(hub.commands, ["forward 30"])


class LaunchTest(unittest.TestCase):
    def test_realtime_is_available_for_ptt_and_wake(self):
        self.assertEqual(validate_launch("ptt", "realtime"), "ptt")
        self.assertEqual(validate_launch("wake", "realtime"), "wake")

    def test_realtime_needs_api_key(self):
        with self.assertRaisesRegex(Exception, "OPENAI_API_KEY"):
            validate_launch("ptt", "realtime", has_api_key=False)


if __name__ == "__main__":
    unittest.main()
