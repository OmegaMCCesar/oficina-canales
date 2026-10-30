"""Texto/JSON con respaldo entre proveedores gratuitos."""
from __future__ import annotations

import json
import logging
import re
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from . import health, is_quota_error, key

log = logging.getLogger("providers.text")
T = TypeVar("T", bound=BaseModel)

# Proveedores con API compatible con OpenAI: (nombre, env de la llave, url base, modelo)
OPENAI_COMPAT = [
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free"),
    ("github", "GITHUB_MODELS_TOKEN", "https://models.github.ai/inference", "openai/gpt-4.1-mini"),
    ("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-medium-latest"),
    ("groq-llama", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
]


def _extract_json(txt: str) -> dict:
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1)
    start = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0] or [0])
    return json.loads(txt[start:])


def _openai_compat(name: str, env: str, base: str, model: str, system: str, user: str, schema: Type[T]) -> T:
    schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    sys = (f"{system}\n\nResponde ÚNICAMENTE con un objeto JSON válido (sin texto extra) que cumpla este "
           f"JSON Schema:\n{schema_txt}")
    headers = {"Authorization": f"Bearer {key(env)}", "Content-Type": "application/json"}
    if name == "openrouter":
        headers.update({"HTTP-Referer": "https://oficina-canales.onrender.com", "X-Title": "Oficina de Canales"})
    body = {"model": model, "temperature": 0.7,
            "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    r = httpx.post(f"{base}/chat/completions", headers=headers, json=body, timeout=120)
    if r.status_code == 400 and "response_format" in r.text:
        body.pop("response_format")
        r = httpx.post(f"{base}/chat/completions", headers=headers, json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    content = r.json()["choices"][0]["message"]["content"]
    return schema.model_validate(_extract_json(content))


def _gemini(system: str, user: str, schema: Type[T], video_path: str | None) -> T:
    from ..llm import gemini_json
    return gemini_json(system, user, schema, video_path)


def _cloudflare(system: str, user: str, schema: Type[T]) -> T:
    acc, tok = key("CLOUDFLARE_ACCOUNT_ID"), key("CLOUDFLARE_API_TOKEN")
    schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=120,
                   json={"messages": [{"role": "system", "content": f"{system}\nResponde solo JSON con este schema: {schema_txt}"},
                                      {"role": "user", "content": user}], "max_tokens": 4000})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    res = r.json()["result"]["response"]
    return schema.model_validate(res if isinstance(res, dict) else _extract_json(res))


def ask(system: str, user: str, schema: Type[T], video_path: str | None = None) -> T:
    """Prueba proveedores en orden. Video (gaming) solo lo entiende Gemini."""
    tried: list[str] = []
    chain: list[tuple[str, callable]] = []
    if key("GEMINI_API_KEY"):
        chain.append(("gemini", lambda: _gemini(system, user, schema, video_path)))
    if not video_path:
        for name, env, base, model in OPENAI_COMPAT:
            if key(env):
                chain.append((name, lambda n=name, e=env, b=base, m=model: _openai_compat(n, e, b, m, system, user, schema)))
        if key("CLOUDFLARE_ACCOUNT_ID") and key("CLOUDFLARE_API_TOKEN"):
            chain.append(("cloudflare", lambda: _cloudflare(system, user, schema)))

    for name, fn in chain:
        if not health.ok(name):
            continue
        tried.append(name)
        try:
            out = fn()
            health.success(name)
            if len(tried) > 1:
                log.info("texto resuelto por %s (tras %s)", name, tried[:-1])
            return out
        except Exception as e:
            health.failure(name, e, quota=is_quota_error(e))
            log.warning("texto: %s falló (%s); paso al siguiente", name, str(e)[:160])
    raise RuntimeError(f"Ningún proveedor de texto respondió (probados: {tried or 'ninguno con llave'})")
