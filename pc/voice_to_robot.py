import asyncio
import os
from pathlib import Path

from pc.agent import robot_agent
from pc.llm import audio_utils, intent_mapper, intent_parser, llm_client
from pc.speech.conversation_gate import ConversationGate
from pc.spike_communication.spikehub import SpikeHub
from pc.utils import utils


class VoiceController:
    def __init__(self, spike_simulation: bool = True):
        self.audio_client = audio_utils.AudioClient()
        self.conversation_gate = ConversationGate(timeout_seconds=20)
        self.llm_client = llm_client.LLMClient()
        self.spike = SpikeHub(simulate=spike_simulation)
        self.robot_agent = robot_agent.RobotAgent(hub=self.spike)

    async def transcribe_audio(self, wav_path: str) -> str:
        try:
            text = await self.audio_client.transcribe_openai(wav_path)
            if text.strip():
                print("OpenAI STT:", text)
                return text
        except Exception as exc:
            print(f"OpenAI STT failed, fallback to local: {exc}")

        try:
            text = await self.audio_client.transcribe_whisper(wav_path)
            print("Local STT:", text)
            return text
        except Exception as exc:
            print(f"Local STT failed: {exc}")
            return ""
    
    async def get_input_text(self, mode: str) -> str:
        if mode == "cli":                                                                                                                
            raw = await asyncio.to_thread(input, "> ")
            return utils.normalize_text(raw)

        # 保留原有按空格录音
        if mode in ("mic", "microphone"):
            wav_path = await self.audio_client.record_push_to_talk()

            try:
                text = await self.transcribe_audio(wav_path)
                return utils.normalize_text(text)
            finally:
                Path(wav_path).unlink(missing_ok=True)

        # 自动 VAD + 模糊唤醒 + 连续对话
        if mode == "wake":
            while True:
                skip_next_start_tone = False
                was_active = self.conversation_gate.is_active()

                if was_active:
                    wait_timeout = self.conversation_gate.remaining_seconds()
                    max_record_seconds = 30.0
                else:
                    wait_timeout = 30.0
                    # 环境音持续存在时，每 5 秒重新检测一次唤醒词
                    max_record_seconds = 10.0

                if wait_timeout <= 0:
                    self.conversation_gate.deactivate()
                    continue

                # 已唤醒时，提示用户可以说下一条命令
                if was_active:
                    if skip_next_start_tone:
                        skip_next_start_tone = False
                    else:
                        await self.audio_client.play_recording_start_tone()

                try:
                    wav_path = await self.audio_client.record_until_silence(
                        wait_timeout_seconds=wait_timeout,
                        max_record_seconds=max_record_seconds,
                    )
                except TimeoutError:
                    self.conversation_gate.deactivate()
                    print("连续对话已超时，请重新唤醒")
                    continue

                # 录音已经停止，立即播放结束提示音
                await self.audio_client.play_recording_stop_tone()

                try:
                    raw_text = await self.transcribe_audio(wav_path)
                finally:
                    Path(wav_path).unlink(missing_ok=True)

                print("识别文本:", raw_text)

                if not raw_text.strip():
                    continue

                # 录音在有效期内开始，即使转写耗时导致超时，也接受这一句话
                if was_active:
                    self.conversation_gate.renew()

                command, just_woke = self.conversation_gate.consume(raw_text)

                if just_woke:
                    # 唤醒成功提示音，同时表示可以开始说命令
                    await self.audio_client.play_recording_start_tone()
                    skip_next_start_tone = True
                    print("已唤醒，30 秒内可以连续对话")

                if command:
                    return utils.normalize_text(command)

        raise ValueError(f"unknown mode: {mode}")

    async def run(
        self,
        mode: str,
        llm_model: str | None,
        stt_device: str = "auto",
        run_once: bool = True,
    ) -> None:
        # Validate STT device and set environment variable before proceeding
        if stt_device not in {"auto", "cpu", "cuda"}:
            raise ValueError(f"unknown STT device: {stt_device}")

        os.environ["VOICE_ROBOT_STT_DEVICE"] = stt_device

        # step 0: connect to SpikeHub
        await self.robot_agent.connect()
        try:
            while True:
                # step 1: parse input text from mic or cli
                input_text = await self.get_input_text(mode)
                # input text: 前进30cm 左转60度，夹子60度
                print('input text:', input_text)

                # step 2 call LLM to generate intent JSON
                llm_out = await self.llm_client.generate(input_text, model=llm_model)
                # LLM output: {
                #   "steps":[
                #         {"action":"forward","params":{"distance_cm":30,"angle_deg":null}},
                #         {"action":"turn_left","params":{"distance_cm":null,"angle_deg":60}}
                #     ]
                # }
                print('LLM output:', llm_out)

                # step 3 parse intent from LLM output
                intent = intent_parser.parse_intent(llm_out)
                # parsed intent: {
                #   'steps': [
                #       {'action': 'forward', 'params': {'distance_cm': 30, 'angle_deg': None}},
                #       {'action': 'left', 'params'                                                 : {'distance_cm': None, 'angle_deg': 60}}
                #   ]
                # }
                print('parsed intent:', intent)

                # step 4 convert intent to sequence and execute
                seq = intent_mapper.intent_to_sequence(intent)
                # sequence: {
                #   'sequence': [
                #       {'cmd': 'forward 30'},
                #       {'cmd': 'left 60'}
                #   ]
                # }
                print('sequence:', seq)

                # step 5 execute the sequence of commands on the SpikeHub
                exec_result = await self.robot_agent.execute_sequence(seq)
                # Executed command: forward 30
                # Executed command: left 60
                print("execute result:", exec_result)
                # execute result: {
                #   'status': 'ok',
                #   'executed': ['forward 30', 'left 60'],
                #   'skipped': [],
                #   'errors': []
                # }

                if run_once:
                    break
        finally:
            await self.robot_agent.disconnect()


if __name__ == '__main__':
    # Initialize SpikeHub in simulation mode for testing
    voice_controller = VoiceController(spike_simulation=True)

    try:
        asyncio.run(
            voice_controller.run(
                mode='cli',  # mic cli wake
                stt_device="auto",  # Options: "auto", "cpu", "cuda"
                llm_model="gpt-6-luna",  # https://developers.openai.com/api/docs/models
                run_once=False,  # True for single command, False for continuous listening
            )
        )
    except Exception as exc:
        print('Error:', exc)
