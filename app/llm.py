"""Wrapper de Gemini. Todos los agentes piden JSON tipado.

En MODE=dry no se llama a la API: cada agente trae un `fallback` plausible
para que el ciclo completo se pueda ejecutar y probar sin credenciales.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from .config import settings

log = logging.getLogger("llm")
T = TypeVar("T", bound=BaseModel)

_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings().GEMINI_API_KEY)
    return _client


def ask_json(system: str, user: str, schema: Type[T], fallback: T | None = None,
             video_path: str | None = None) -> T:
    """Llama a Gemini pidiendo JSON que valide contra `schema`.

    video_path: opcional, sube un video (gaming) para que el modelo lo analice.
    """
    if settings().MODE == "dry" or not settings().GEMINI_API_KEY:
        if fallback is None:
            raise RuntimeError("MODE=dry requiere fallback para " + schema.__name__)
        log.info("[dry] %s -> fallback", schema.__name__)
        return fallback

    from google.genai import types

    contents: list[Any] = []
    if video_path:
        up = client().files.upload(file=video_path)
        # esperar a que procese
        import time
        while up.state.name == "PROCESSING":
            time.sleep(3)
            up = client().files.get(name=up.name)
        contents.append(up)
    contents.append(user)

    import time
    last = None
    for attempt in range(4):
        try:
            resp = client().models.generate_content(
                model=settings().GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.7,
                ),
            )
            break
        except Exception as e:  # 429 del nivel gratuito, 503 de sobrecarga
            last = e
            log.warning("Gemini %s falló (%s); reintento", schema.__name__, str(e)[:160])
            time.sleep(15 * (attempt + 1))
    else:
        raise RuntimeError(f"Gemini no respondió: {last}")
    try:
        return schema.model_validate_json(resp.text)
    except Exception:
        # intento de rescate: a veces viene envuelto en ```json
        txt = resp.text.strip().strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
        return schema(**json.loads(txt))


def _card_image(text: str, out_path: Path, aspect: str) -> Path:
    """Respaldo sin IA: tarjeta con degradado cálido y el concepto de la escena."""
    from PIL import Image, ImageDraw

    from .tools.ffmpeg import font, wrap_lines

    w, h = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    top, bot = (32, 44, 78), (242, 166, 90)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    fnt = font(76 if aspect == "9:16" else 64)
    concept = text.split(".")[0][:120]
    lines = wrap_lines(d, concept, fnt, int(w * 0.8))[:5]
    y = h * 0.32
    for l in lines:
        d.text(((w - d.textlength(l, font=fnt)) / 2, y), l, font=fnt, fill="white", stroke_width=3, stroke_fill=(20, 20, 40))
        y += 96
    img.save(out_path)
    return out_path


def generate_image(prompt: str, out_path: Path, aspect: str = "9:16") -> Path:
    """Imagen con Gemini (flash-image). Si falla o no hay cuota, tarjeta de respaldo."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if settings().MODE == "dry" or not settings().GEMINI_API_KEY:
        return _card_image(prompt, out_path, aspect)

    from google.genai import types
    try:
        r = client().models.generate_content(
            model=settings().GEMINI_IMAGE_MODEL,
            contents=f"{prompt}. Formato {'vertical 9:16' if aspect == '9:16' else 'horizontal 16:9'}. Sin texto.",
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect),
            ),
        )
        for part in r.candidates[0].content.parts:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                out_path.write_bytes(part.inline_data.data)
                return out_path
        raise RuntimeError("respuesta sin imagen")
    except Exception as e:
        log.warning("imagen IA no disponible (%s); uso tarjeta de respaldo", str(e)[:200])
        return _card_image(prompt, out_path, aspect)
