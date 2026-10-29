"""Texto a voz con Google Cloud TTS. En dry genera silencio de la duración estimada."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import settings


def estimate_seconds(text: str, wpm: int = 150) -> float:
    return max(1.5, len(text.split()) / wpm * 60)


def synthesize(text: str, out_path: Path, voice: str = "es-US-Neural2-B", language: str = "es-US") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if settings().MODE == "dry":
        secs = estimate_seconds(text)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", f"{secs:.2f}", "-c:a", "libmp3lame", str(out_path)],
            check=True,
        )
        return out_path

    from google.cloud import texttospeech as t

    client = t.TextToSpeechClient()
    lang = "-".join(voice.split("-")[:2])
    resp = client.synthesize_speech(
        input=t.SynthesisInput(text=text),
        voice=t.VoiceSelectionParams(language_code=lang, name=voice),
        audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.MP3, speaking_rate=1.05),
    )
    out_path.write_bytes(resp.audio_content)
    return out_path


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out or 0)
