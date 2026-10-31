"""Modelos que VEN imágenes (gratis primero): revisan cuadros de los videos ya renderizados.

Orden: Groq (Llama 4 Scout) > GitHub Models (gpt-4.1-mini) > OpenRouter (Gemma 3 :free) > Gemini > Cloudflare (Llama 3.2 Vision).
Entrada: una imagen (hoja de cuadros) + texto. Salida: JSON tipado.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from . import cloudflare_post, health, is_quota_error, key
from .text import ProvidersDown, _extract_json

log = logging.getLogger("providers.vision")
T = TypeVar("T", bound=BaseModel)

OPENAI_VISION = [
    ("v-groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "meta-llama/llama-4-scout-17b-16e-instruct"),
    ("v-github", "GITHUB_MODELS_TOKEN", "https://models.github.ai/inference", "openai/gpt-4.1-mini"),
    ("v-openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemma-3-27b-it:free"),
    ("v-mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "pixtral-12b-latest"),
]


def _schema_hint(schema) -> str:
    return ("Responde ÚNICAMENTE con un objeto JSON válido que cumpla este JSON Schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False))


def _openai(env, base, model, system, user, img_b64, schema):
    body = {"model": model, "temperature": 0.4, "messages": [
        {"role": "system", "content": f"{system}\n\n{_schema_hint(schema)}"},
        {"role": "user", "content": [{"type": "text", "text": user},
                                     {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}]}]}
    r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key(env)}"}, json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return schema.model_validate(_extract_json(r.json()["choices"][0]["message"]["content"] or ""))


def _gemini(system, user, img: bytes, schema):
    from google.genai import types

    from ..config import settings
    from ..llm import client
    resp = client().models.generate_content(
        model=settings().GEMINI_MODEL, contents=[types.Part.from_bytes(data=img, mime_type="image/jpeg"), user],
        config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                           response_schema=schema, temperature=0.4))
    return schema.model_validate_json(resp.text)


def _cloudflare(system, user, img: bytes, schema):
    model = "@cf/meta/llama-3.2-11b-vision-instruct"
    payload = {"messages": [{"role": "system", "content": f"{system}\n{_schema_hint(schema)}"}, {"role": "user", "content": user}],
               "image": list(img), "max_tokens": 1500}
    try:
        data = cloudflare_post(model, payload)
    except Exception as e:
        if "agree" not in str(e).lower():
            raise
        cloudflare_post(model, {"prompt": "agree"})      # el modelo pide aceptar su licencia una vez
        data = cloudflare_post(model, payload)
    res = data["result"]["response"]
    return schema.model_validate(res if isinstance(res, dict) else _extract_json(res))


def ask(system: str, user: str, image: Path, schema: Type[T]) -> T:
    img = image.read_bytes()
    b64 = base64.b64encode(img).decode()
    chain = []
    if key("ANTHROPIC_API_KEY"):
        from . import claude
        chain.append(("v-claude", lambda: claude.ask(system, user, schema, image=image, model_tier="smart")))
    chain += [(n, (lambda e=e, b=b, m=m: _openai(e, b, m, system, user, b64, schema))) for n, e, b, m in OPENAI_VISION if key(e)]
    if key("GEMINI_API_KEY"):
        chain.append(("v-gemini", lambda: _gemini(system, user, img, schema)))
    if key("CLOUDFLARE_API_TOKEN"):
        chain.append(("v-cloudflare", lambda: _cloudflare(system, user, img if len(img) < 400_000 else img, schema)))
    tried = []
    for name, fn in chain:
        if not health.ok(name):
            continue
        tried.append(name)
        try:
            out = fn()
            health.success(name)
            log.info("visión resuelta por %s", name)
            return out
        except Exception as e:
            health.failure(name, e, quota=is_quota_error(e))
            log.warning("visión: %s falló (%s)", name, str(e)[:160])
    raise ProvidersDown(f"Ningún modelo con visión respondió (probados: {tried or 'ninguno disponible'})")
