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
    try:
        return schema.model_validate_json(resp.text)
    except Exception:
        # intento de rescate: a veces viene envuelto en ```json
        txt = resp.text.strip().strip("`")
        if txt.startswith("json"):
            txt = txt[4:]
        return schema(**json.loads(txt))


def generate_image(prompt: str, out_path: Path, aspect: str = "9:16") -> Path:
    """Imagen con Imagen (Gemini). En dry genera un placeholder con PIL."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if settings().MODE == "dry" or not settings().GEMINI_API_KEY:
        from PIL import Image, ImageDraw
        w, h = (1080, 1920) if aspect == "9:16" else (1920, 1080)
        img = Image.new("RGB", (w, h), (245, 200, 120))
        d = ImageDraw.Draw(img)
        d.rectangle([60, 60, w - 60, h - 60], outline=(120, 60, 20), width=12)
        d.text((100, 100), prompt[:200], fill=(60, 30, 10))
        img.save(out_path)
        return out_path

    from google.genai import types
    r = client().models.generate_images(
        model=settings().GEMINI_IMAGE_MODEL,
        prompt=prompt,
        config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio=aspect),
    )
    r.generated_images[0].image.save(str(out_path))
    return out_path
