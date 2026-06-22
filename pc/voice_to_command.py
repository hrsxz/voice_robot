import argparse
import asyncio
import json
import re

from pc.llm.audio_utils import AudioClient
from pc.llm.intent_mapper import intent_to_sequence
from pc.llm.llm_client import generate
from pc.spike.spikehub import SpikeHub


def normalize_text(text: str) -> str:
    # 将输入文本转换为半角、去掉大部分符号、统一大小写，便于后续规则匹配和参数提取
    def _to_half_width(text: str) -> str:
        chars = []
        for ch in text:
            code = ord(ch)
            if code == 0x3000:
                code = 0x0020
            elif 0xFF01 <= code <= 0xFF5E:
                code -= 0xFEE0
            chars.append(chr(code))
        return ''.join(chars)

    if not text:
        return ''
    # 统一空格、全角数字和全角标点，便于后续规则匹配
    normalized = _to_half_width(str(text).strip()).lower()
    # 去掉大部分符号，但保留中文、英文、数字和空白
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]+", ' ', normalized)
    # 把连续空白压缩成一个空格，避免分词和正则提取受干扰
    normalized = re.sub(r"\s+", ' ', normalized)

    return normalized.strip()


class VoiceController:
    def __init__(self, spike: SpikeHub, audio_client: AudioClient | None = None):
        self.spike = spike
        self.audio_client = audio_client or AudioClient()

    async def execute_sequence(self, seq_spec: dict) -> None:
        seq = seq_spec.get('sequence', [])
        for item in seq:
            cmd = item.get('cmd')
            if not cmd:
                continue
            print('-> sending', cmd)
            await self.spike.send(cmd)

    async def get_input_text(
        self,
        mode: str,
        prompt_text: str | None,
        mic_duration: float | None,
    ) -> str:
        if prompt_text:
            return normalize_text(prompt_text)

        input_mode = (mode or 'cli').lower()
        if input_mode == 'cli':
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, input, '> ')
            return normalize_text(raw)

        if input_mode in ('mic', 'microphone'):
            wav_path = await self.audio_client.record(duration=mic_duration or 3.0)
            text = await self.audio_client.transcribe(wav_path)
            return normalize_text(text)

        raise ValueError('unknown mode: ' + input_mode)

    def parse_intent(self, llm_text: str) -> dict:
        # 优先按 JSON 解析，适配 LLM 规范化输出，例如：
        # {"action": "forward", "params": {"distance_cm": 30}}
        # 或 {"intent": "left", "arguments": {"angle_deg": 90}}
        try:
            obj = json.loads(llm_text)
            if isinstance(obj, dict):
                # 字段名：action，params
                action = obj.get('action')
                params = obj.get('params') or {}
                if action:
                    return {
                        'action': self._normalize_action(str(action)),
                        'params': params,
                    }
        except Exception:
            # LLM 输出不一定是合法 JSON，解析失败时回退到关键词规则匹配
            pass

        # 回退路径：直接从自然语言文本中提取意图
        # 这里不依赖严格格式，适合处理“向前走 20 cm”这类自由表达
        text = llm_text.strip().lower()
        # 停止命令优先单独处理，因为它通常不需要附带参数
        if any(key in text for key in ('停', '停止', 'stop')):
            return {'action': 'stop', 'params': {}}
        
        # 方向类命令会继续尝试提取距离、时间、角度等参数
        if any(key in text for key in ('前', '走前', 'forward', 'ahead')):
            return {'action': 'forward', 'params': self._extract_params(text)}
        if any(key in text for key in ('后', '后退', 'backward')):
            return {'action': 'backward', 'params': self._extract_params(text)}
        if any(key in text for key in ('左', '左转', 'turn left', 'left')):
            return {'action': 'left', 'params': self._extract_params(text)}
        if any(key in text for key in ('右', '右转', 'turn right', 'right')):
            return {'action': 'right', 'params': self._extract_params(text)}
        
        # 无法识别时保留原始输出，方便打印日志或后续排查
        return {'action': None, 'params': {'raw': llm_text}}

    async def run_once(
        self,
        mode: str,
        prompt_text: str | None,
        mic_duration: float | None,
        llm_model: str | None,
    ) -> None:
        text = await self.get_input_text(mode, prompt_text, mic_duration)
        print('input text:', text)

        llm_out = await generate(text, model=llm_model)
        print('LLM output:', llm_out)

        intent = self.parse_intent(llm_out)
        print('parsed intent:', intent)

        seq = intent_to_sequence(intent)
        print('sequence:', seq)

        await self.execute_sequence(seq)

    @staticmethod
    def _normalize_action(action: str) -> str | None:
        value = action.strip().lower()
        if value in ('forward', 'f', '前', '前进', 'go forward', 'ahead'):
            return 'forward'
        if value in ('backward', 'b', '后', '后退', 'back'):
            return 'backward'
        if value in ('left', 'l', '左', 'left turn', 'turn_left'):
            return 'left'
        if value in ('right', 'r', '右', 'right turn', 'turn_right'):
            return 'right'
        if value in ('stop', '停止', 'hold'):
            return 'stop'
        return None

    @staticmethod
    def _extract_params(text: str) -> dict:
        params: dict = {}
        match = re.search(r'(\d+(\.\d+)?)\s*(cm|m|米|deg|°|度)?', text)
        if not match:
            return params

        value = float(match.group(1))
        unit = (match.group(3) or '').lower()
        if unit == 'cm':
            params['distance_cm'] = int(value)
        elif unit in ('m', '米'):
            params['distance_cm'] = int(value * 100)
        elif unit in ('deg', '度', '°'):
            params['angle_deg'] = int(value)
        else:
            params['distance_cm'] = int(value)
        return params


def main() -> None:
    voice_controller = VoiceController(SpikeHub(simulate=True))

    try:
        asyncio.run(
            voice_controller.run_once(
                mode='cli',
                prompt_text="向前走30cm",
                mic_duration=None,
                llm_model=None,)
        )
    except Exception as exc:
        print('Error:', exc)


if __name__ == '__main__':
    main()
