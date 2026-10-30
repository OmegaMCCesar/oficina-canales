"""Voz con respaldo: Edge TTS (gratis, voces es-MX, tiempos por palabra) > Gemini TTS > Cloudflare MeloTTS.

Devuelve el mp3 y la lista de palabras con su tiempo [(inicio_s, fin_s, palabra)] para los
subtítulos palabra por palabra. Si el proveedor no da tiempos, se estiman.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
from pathlib import Path

import httpx

from . import health, is_quota_error, key

log = logging.getLogger("providers.voice")

# Voces neurales de México en Edge
EDGE_VOICES = {"male": "es-MX-JorgeNeural", "female": "es-MX-DaliaNeural"}


def _edge(text: str, out: Path, voice: str, rate: str) -> list[tuple[float, float, str]]:
    import edge_tts

    words: list[tuple[float, float, str]] = []

    async def run():
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        with open(out, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    st = chunk["offset"] / 1e7
                    words.append((st, st + chunk["duration"] / 1e7, chunk["text"]))

    asyncio.run(run())
    if out.stat().st_size < 1000:
        raise RuntimeError("edge-tts devolvió audio vacío")
    return words


def _cloudflare_melo(text: str, out: Path) -> None:
    acc, tok = key("CLOUDFLARE_ACCOUNT_ID"), key("CLOUDFLARE_API_TOKEN")
    r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/myshell-ai/melotts",
                   headers={"Authorization": f"Bearer {tok}"}, json={"prompt": text, "lang": "es"}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    audio = r.json()["result"]["audio"]
    wav = out.with_suffix(".melo.wav")
    wav.write_bytes(base64.b64decode(audio))
    from ..tools.ffmpeg import FFMPEG
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libmp3lame", str(out)], check=True)
    wav.unlink(missing_ok=True)


def estimate_words(text: str, total: float) -> list[tuple[float, float, str]]:
    toks = text.split()
    if not toks:
        return []
    weights = [max(2, len(t)) for t in toks]
    per = total / sum(weights)
    out, t = [], 0.0
    for tok, w in zip(toks, weights):
        out.append((t, t + w * per, tok))
        t += w * per
    return out


def speak(text: str, out: Path, gender: str = "male", rate: str = "+8%") -> tuple[Path, list[tuple[float, float, str]]]:
    from ..tools.tts import duration, synthesize

    out.parent.mkdir(parents=True, exist_ok=True)
    chain = []
    chain.append(("edge-tts", lambda: _edge(text, out, EDGE_VOICES.get(gender, EDGE_VOICES["male"]), rate)))
    if key("GEMINI_API_KEY"):
        chain.append(("gemini-tts", lambda: (synthesize(text, out), [])[1]))
    if key("CLOUDFLARE_API_TOKEN"):
        chain.append(("melotts", lambda: (_cloudflare_melo(text, out), [])[1]))
    for name, fn in chain:
        if not health.ok(name):
            continue
        try:
            words = fn()
            health.success(name)
            if not words:
                words = estimate_words(text, duration(out))
            log.info("voz por %s (%d palabras con tiempo)", name, len(words))
            return out, words
        except Exception as e:
            health.failure(name, e, quota=is_quota_error(e))
            log.warning("voz: %s falló (%s)", name, str(e)[:160])
    raise RuntimeError("Ningún proveedor de voz respondió")
