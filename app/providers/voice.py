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


def _edge_once(text: str, out: Path, voice: str, rate: str) -> list[tuple[float, float, str]]:
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


def _chunks(text: str, limit: int = 1400) -> list[str]:
    """Parte el texto en bloques por oraciones (los guiones de 9 min son largos para una sola petición)."""
    import re
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?¿¡])\s+", text.strip()):
        if cur and len(cur) + len(sent) + 1 > limit:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts


def _edge(text: str, out: Path, voice: str, rate: str) -> list[tuple[float, float, str]]:
    """Edge TTS por bloques, con reintentos (a veces responde 'No audio was received' de forma pasajera)."""
    import re
    import time

    from ..tools.ffmpeg import FFMPEG
    from ..tools.tts import duration
    text = re.sub(r"[^\w\s.,;:¿?¡!%°()/\-'\"áéíóúüñÁÉÍÓÚÜÑ]", " ", text)   # sin emojis ni símbolos raros
    blocks = _chunks(text)
    files, words, offset = [], [], 0.0
    try:
        for i, block in enumerate(blocks):
            part = out.with_name(f"{out.stem}_p{i:02}.mp3")
            last = None
            for attempt in range(3):
                try:
                    ws = _edge_once(block, part, voice, rate)
                    break
                except Exception as e:
                    last = e
                    time.sleep(2 + attempt * 4)
            else:
                raise RuntimeError(f"edge-tts falló 3 veces: {last}")
            words += [(a + offset, b + offset, w) for a, b, w in ws]
            offset += duration(part) or (ws[-1][1] if ws else 0)
            files.append(part)
        if len(files) == 1:
            files[0].replace(out)
        else:
            lst = out.with_name(out.stem + "_lista.txt")
            lst.write_text("".join(f"file '{f.name}'\n" for f in files))
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", str(out)], check=True, cwd=str(out.parent))
            lst.unlink(missing_ok=True)
    finally:
        for f in files:
            f.unlink(missing_ok=True)
    return words


def _cloudflare_melo(text: str, out: Path) -> None:
    from . import cloudflare_post
    audio = cloudflare_post("@cf/myshell-ai/melotts", {"prompt": text, "lang": "es"})["result"]["audio"]
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
