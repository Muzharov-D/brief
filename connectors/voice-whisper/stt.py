"""Расшифровка речи на этом компьютере (faster-whisper, без интернета после первой загрузки модели).

Модель задаётся переменной BRIEFING_WHISPER_MODEL: small (по умолчанию, ~0,5 ГБ, быстро),
medium (точнее, ~1,5 ГБ, медленнее). Модель скачивается один раз при установке.
"""
import os

_model = None


def model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(os.environ.get("BRIEFING_WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    return _model


def segments(path, prompt=None):
    """[(начало_сек, конец_сек, текст)] для файла любого формата (ogg, wav, m4a, mp3)."""
    segs, _ = model().transcribe(str(path), language="ru", vad_filter=True, beam_size=5,
                                 initial_prompt=prompt)
    return [(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]


def text(path, prompt=None):
    return " ".join(t for _, _, t in segments(path, prompt))
