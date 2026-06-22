"""Local Whisper STT wrapper using `openai-whisper`.

Usage:
    from pc.llm import whisper_stt
    text = whisper_stt.transcribe('path/to/file.wav')

Notes:
 - Requires `pip install -U openai-whisper` and `ffmpeg` available in PATH.
 - For better performance on CPU/GPU consider `faster-whisper` (not implemented here).
"""
from typing import Optional

try:
    import whisper
except Exception as e:
    raise ImportError(
        'openai-whisper is not installed. Run: pip install -U '
        'openai-whisper and ensure ffmpeg is available'
    ) from e

# Load model lazily to avoid long startup when module is imported
_model: Optional[whisper.Whisper] = None


def _get_model(name: str = "small") -> whisper.Whisper:
    global _model
    if _model is None:
        _model = whisper.load_model(name)
    return _model


def transcribe(wav_path: str, model_name: str = "small") -> str:
    """Transcribe a WAV/MP3 file and return text.

    Args:
        wav_path: path to audio file
        model_name: whisper model name (tiny, base, small, medium, large)

    Returns:
        Transcribed text.
    """
    model = _get_model(model_name)
    result = model.transcribe(wav_path)
    return result.get("text", "")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m pc.llm.whisper_stt <audio.wav>")
        raise SystemExit(1)
    path = sys.argv[1]
    print(transcribe(path))
