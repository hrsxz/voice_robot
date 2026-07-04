# coding: utf-8

import asyncio

from pc.agent import robot_agent
from pc.llm import audio_utils, intent_mapper, intent_parser, llm_client
from pc.spike_communication.spikehub import SpikeHub
from pc.utils import utils


class VoiceController:
    def __init__(self, spike_simulation: bool = True):
        self.audio_client = audio_utils.AudioClient()
        self.llm_client = llm_client.LLMClient()
        self.spike = SpikeHub(simulate=spike_simulation)
        self.robot_agent = robot_agent.RobotAgent(hub=self.spike)

    async def get_input_text(self, mode: str,) -> str:
        if mode == 'cli':
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, input, '> ')
            return utils.normalize_text(raw)

        if mode in ("mic", "microphone"):
            wav_path = await self.audio_client.record_push_to_talk()
            # 同时尝试本地 whisper 和 OpenAI Whisper，取结果较好的一个
            out = {"whisper": None, "openai": None, "errors": {}}
            try:
                out["whisper"] = await self.audio_client.transcribe_whisper(wav_path)
                print('whisper result:', out["whisper"])
            except Exception as e:
                out["errors"]["whisper"] = str(e)
            try:
                out["openai"] = await self.audio_client.transcribe_openai(wav_path)
                print('openai result:', out["openai"])
            except Exception as e:
                out["errors"]["openai"] = str(e)

            # 选择结果较好的一个
            text = out["openai"] or out["whisper"] or ""
            return utils.normalize_text(text)

        raise ValueError('unknown mode: ' + mode)

    async def run(
        self,
        mode: str,
        llm_model: str | None,
        run_once: bool = True,
    ) -> None:
        # step 0: connect to SpikeHub
        await self.robot_agent.connect()
        try:
            while True:
                # step 1: parse input text from mic or cli
                input_text = await self.get_input_text(mode)
                # input text: 前进30cm 左转60度，夹子60度
                print('input text:', input_text)

                # step 2: call LLM to generate intent JSON
                llm_out = await self.llm_client.generate(input_text, model=llm_model)
                # LLM output: {
                #   "steps":[
                #       {"action":"forward","args":{"distance_cm":30}},
                #       {"action":"left","args":{"angle_deg":60}}
                # ]}
                print('LLM output:', llm_out)

                # step 3: parse intent from LLM output
                intent = intent_parser.parse_intent(llm_out)
                # parsed intent: {
                #   'steps': [
                #       {'action': 'forward',"args":{"distance_cm":30}},
                #       {'action': 'left',"args":{"angle_deg":60}}
                #   ]
                # }
                print('parsed intent:', intent)

                # step 4: convert intent to sequence and execute
                seq = intent_mapper.intent_to_sequence(intent)
                # sequence: {
                #   'sequence': [
                #       {'cmd': 'forward 30'},
                #       {'cmd': 'left 60'}
                #   ]
                # }
                print('sequence:', seq)

                # step 5: execute the sequence of commands on the SpikeHub
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
                mode='cli',  # mic cli
                llm_model="gpt-5.4-mini",  # # gpt-5.4-mini # gpt-5.4 gpt-5.5
                run_once=False,  # True for single command, False for continuous listening
            )
        )
    except Exception as exc:
        print('Error:', exc)
