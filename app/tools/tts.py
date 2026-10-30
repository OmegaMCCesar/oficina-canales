"""Texto a voz.

- live: voz de Gemini (gemini-2.5-flash-preview-tts), usa la misma GEMINI_API_KEY.
- dry: silencio de la duración estimada (para probar sin APIs).
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import wave
from pathlib import Path

from ..config import settings
from .ffmpeg import FFMPEG

log = logging.getLogger("tts")

# Voces prebuilt de Gemini. Puck = cálida y clara; Fenrir = energética.
DEFAULT_VOICE = "Puck"


def estimate_seconds(text: str, wpm: int = 150) -> float:
    return max(1.5, len(text.split()) / wpm * 60)


def _gemini_voice(voice: str) -> str:
    # Los yaml traen nombres de Google Cloud TTS (es-US-Neural2-B); mapear a voces Gemini
    if re.match(r"^[A-Z][a-z]+$", voice or ""):
        return voice
    return "Fenrir" if voice.endswith("-A") else DEFAULT_VOICE


def synthesize(text: str, out_path: Path, voice: str = DEFAULT_VOICE, language: str = "es-MX",
               style: str = "") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    s = settings()
    if s.MODE == "dry" or not s.GEMINI_API_KEY:
        secs = estimate_seconds(text)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", f"{secs:.2f}", "-c:a", "libmp3lame", str(out_path)], check=True)
        return out_path

    from google.genai import types

    from ..llm import client

    prompt = f"{style or 'Habla en español de México, tono claro y amable, ritmo ágil'}: {text}"
    name = _gemini_voice(voice)
    configs = [
        # forma actual de la API (voice_config.voice)
        {"response_modalities": ["AUDIO"], "speech_config": {"voice_config": {"voice": name}}},
        # forma anterior (prebuilt_voice_config.voice_name)
        types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=name)))),
    ]
    last_err, data = None, None
    for attempt in range(4):
        cfg = configs[0] if attempt % 2 == 0 else configs[1]
        try:
            resp = client().models.generate_content(model=s.GEMINI_TTS_MODEL, contents=prompt, config=cfg)
            data = resp.candidates[0].content.parts[0].inline_data.data
            break
        except Exception as e:  # límite del nivel gratuito o forma de config no aceptada
            last_err = e
            log.warning("TTS falló (%s); reintento", str(e)[:200])
            time.sleep(10 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"TTS de Gemini falló: {last_err}")

    wav_path = out_path.with_suffix(".wav")
    if data[:4] == b"RIFF":          # WAV completo
        wav_path.write_bytes(data)
    else:                            # PCM crudo 24 kHz mono 16 bits
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(data)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "128k",
                    str(out_path)], check=True)
    wav_path.unlink(missing_ok=True)
    return out_path


def duration(path: Path) -> float:
    """Duración en segundos leyendo la salida de ffmpeg (no depende de ffprobe)."""
    r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        return 0.0
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)
