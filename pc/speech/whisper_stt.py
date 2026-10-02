"""Local Whisper STT wrapper using `openai-whisper`.

Usage:
    from pc.llm import whisper_stt
    text = whisper_stt.transcribe('path/to/file.wav')

Notes:
 - Requires `pip install -U openai-whisper` and `ffmpeg` available in PATH.
 - For better performance on CPU/GPU consider `faster-whisper` (not implemented here).
"""
import os

import ctranslate2
from faster_whisper import WhisperModel

_model: WhisperModel | None = None
_model_device = ""

COMMAND_PROMPT = (
    "机器人控制命令：你好机器人，小智机器人，前进，后退，"
    "左转，右转，停止，左夹爪抬起，左夹爪放下，"
    "右夹爪抬起，右夹爪放下，沿左侧巡线，沿右侧巡线。"
)


def _create_cpu_model() -> WhisperModel:
    global _model_device

    _model_device = "cpu"
    print("[STT] CPU: small/int8")

    return WhisperModel(
        "small",
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, (os.cpu_count() or 4) - 1),
        num_workers=1,
    )


def _create_cuda_model() -> WhisperModel:
    global _model_device

    _model_device = "cuda"
    print("[STT] GPU: turbo/int8_float16")

    return WhisperModel(
        "turbo",
        device="cuda",
        compute_type="int8_float16",
        num_workers=1,
    )


def _get_model() -> WhisperModel:
    global _model

    if _model is not None:
        return _model

    requested_device = os.getenv("VOICE_ROBOT_STT_DEVICE", "auto").lower()

    if requested_device != "cpu" and ctranslate2.get_cuda_device_count() > 0:
        try:
            _model = _create_cuda_model()
            return _model
        except Exception as exc:
            print(f"[STT] GPU initialization failed: {exc}")

    _model = _create_cpu_model()
    return _model


def _run_transcription(model: WhisperModel, wav_path: str) -> str:
    segments, _ = model.transcribe(
        wav_path,
        language="zh",
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 600},
        condition_on_previous_text=False,
        initial_prompt=COMMAND_PROMPT,
    )

    return "".join(segment.text for segment in segments).strip()


def transcribe(wav_path: str) -> str:
    global _model

    model = _get_model()

    try:
        return _run_transcription(model, wav_path)
    except Exception as exc:
        if _model_device != "cuda":
            raise

        print(f"[STT] GPU inference failed, switching to CPU: {exc}")
        _model = _create_cpu_model()
        return _run_transcription(_model, wav_path)
