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
    """JSON tipado desde el primer proveedor de texto disponible (Gemini, Groq, Cerebras, ...).

    En MODE=dry devuelve `fallback` sin llamar a nada.
    """
    if settings().MODE == "dry":
        if fallback is None:
            raise RuntimeError("MODE=dry requiere fallback para " + schema.__name__)
        return fallback
    from .providers import text
    return text.ask(system, user, schema, video_path)


def gemini_json(system: str, user: str, schema: Type[T], video_path: str | None = None) -> T:
    """Una llamada a Gemini (sin reintentos largos: el router cae al siguiente proveedor)."""
    from google.genai import types

    contents: list[Any] = []
    if video_path:
        import time
        up = client().files.upload(file=video_path)
        while up.state.name == "PROCESSING":
            time.sleep(3)
            up = client().files.get(name=up.name)
        contents.append(up)
    contents.append(user)

    import time
    last = None
    for attempt in range(2):  # un reintento corto para 503 pasajeros
        try:
            resp = client().models.generate_content(
                model=settings().GEMINI_MODEL, contents=contents,
                config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                                   response_schema=schema, temperature=0.7))
            break
        except Exception as e:
            last = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                raise
            time.sleep(8)
    else:
        raise RuntimeError(f"Gemini no respondió: {last}")
    try:
        return schema.model_validate_json(resp.text)
    except Exception:
        txt = resp.text.strip().strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
        return schema(**json.loads(txt))


def _card_image(text: str, out_path: Path, aspect: str) -> Path:
    """Respaldo sin IA: tarjeta con degradado cálido y el concepto de la escena."""
    from PIL import Image, ImageDraw

    from .tools.ffmpeg import font, wrap_lines

    w, h = (720, 1280) if aspect == "9:16" else (1280, 720)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    top, bot = (7, 16, 31), (8, 70, 110)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    fnt = font(52 if aspect == "9:16" else 44)
    concept = text.split(".")[0][:120]
    if not concept.strip():
        img.save(out_path)
        return out_path
    lines = wrap_lines(d, concept, fnt, int(w * 0.8))[:5]
    y = h * 0.32
    for l in lines:
        d.text(((w - d.textlength(l, font=fnt)) / 2, y), l, font=fnt, fill="white", stroke_width=3, stroke_fill=(20, 20, 40))
        y += 66
    img.save(out_path)
    return out_path


def card_image(text: str, out_path: Path, aspect: str = "9:16") -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return _card_image(text, out_path, aspect)


def gemini_image(prompt: str, out_path: Path, aspect: str = "9:16") -> Path:
    """Imagen con Gemini (requiere facturación activa; el nivel gratuito no incluye imágenes)."""
    from google.genai import types
    r = client().models.generate_content(
        model=settings().GEMINI_IMAGE_MODEL,
        contents=f"{prompt}. Formato {'vertical 9:16' if aspect == '9:16' else 'horizontal 16:9'}. Sin texto.",
        config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                           image_config=types.ImageConfig(aspect_ratio=aspect)))
    for part in r.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            out_path.write_bytes(part.inline_data.data)
            return out_path
    raise RuntimeError("respuesta sin imagen")


def generate_image(prompt: str, out_path: Path, aspect: str = "9:16", query: str = "",
                   sources: list[str] | None = None, used: set | None = None, card_text: str | None = None) -> Path:
    """Imagen por el router de visuales; en dry, tarjeta."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if settings().MODE == "dry":
        return _card_image(prompt if card_text is None else card_text, out_path, aspect)
    from .providers import visuals
    return visuals.image(prompt, query or prompt[:60], out_path, aspect == "9:16", used if used is not None else set(),
                         sources or ["flux", "pexels", "pixabay", "gemini"],
                         card_text=prompt if card_text is None else card_text)
