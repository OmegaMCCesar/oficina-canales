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
    # cada modelo de Groq tiene su propio límite por minuto: más modelos = más capacidad gratis
    ("groq-20b", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    ("groq-qwen", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "qwen/qwen3-32b"),
    ("groq-kimi", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "moonshotai/kimi-k2-instruct"),
]


class ProvidersDown(RuntimeError):
    """Ningún modelo de texto disponible ahora (cuotas): el trabajo debe esperar, no fallar."""


def _extract_json(txt: str) -> dict:
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1)
    start = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0] or [0])
    body = txt[start:]
    try:
        return json.loads(body, strict=False)          # tolera saltos de línea/tabs dentro de textos
    except json.JSONDecodeError:
        end = max(body.rfind("}"), body.rfind("]"))
        return json.loads(body[:end + 1], strict=False)


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
    msg = r.json()["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    return schema.model_validate(_extract_json(content))


def _gemini(system: str, user: str, schema: Type[T], video_path: str | None) -> T:
    from ..llm import gemini_json
    return gemini_json(system, user, schema, video_path)


def _cloudflare(system: str, user: str, schema: Type[T]) -> T:
    from . import cloudflare_post
    schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    data = cloudflare_post("@cf/meta/llama-3.3-70b-instruct-fp8-fast", {"max_tokens": 6000, "messages": [
        {"role": "system", "content": f"{system}\nResponde solo JSON con este schema: {schema_txt}"},
        {"role": "user", "content": user}]})
    res = data["result"]["response"]
    return schema.model_validate(res if isinstance(res, dict) else _extract_json(res))


def _cf_oss(system: str, user: str, schema: Type[T]) -> T:
    """gpt-oss-120b en Workers AI por su endpoint compatible con OpenAI (prueba los dos tokens)."""
    import os

    from . import cloudflare_tokens
    acc = key("CLOUDFLARE_ACCOUNT_ID")
    last = None
    for tok in cloudflare_tokens():
        os.environ["_CF_TOKEN_IN_USE"] = tok
        try:
            return _openai_compat("cf-oss", "_CF_TOKEN_IN_USE", f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1",
                                  "@cf/openai/gpt-oss-120b", system, user, schema)
        except RuntimeError as e:
            last = e
            if not str(e).startswith(("401", "403")):
                raise
        finally:
            os.environ.pop("_CF_TOKEN_IN_USE", None)
    raise last or RuntimeError("sin token de Cloudflare")


def ask(system: str, user: str, schema: Type[T], video_path: str | None = None) -> T:
    """Prueba proveedores en orden; si todos están en pausa corta (límite por minuto), espera y reintenta."""
    import time
    names: list[str] = []
    for attempt in range(4):
        try:
            return _ask_once(system, user, schema, video_path, names)
        except ProvidersDown:
            wait = health.wait_hint(names) if names else 0
            if not names or wait > 150 or attempt == 3:
                raise
            log.info("todos los modelos en pausa por límite por minuto; espero %.0fs", wait)
            time.sleep(wait + 1)
    raise ProvidersDown("Ningún proveedor de texto respondió")


def _ask_once(system: str, user: str, schema: Type[T], video_path: str | None, names: list[str]) -> T:
    tried: list[str] = []
    chain: list[tuple[str, callable]] = []
    if video_path and key("GEMINI_API_KEY"):
        chain.append(("gemini", lambda: _gemini(system, user, schema, video_path)))   # solo Gemini ve video
    if not video_path and key("ANTHROPIC_API_KEY"):
        from . import claude   # cerebro de pago: Sonnet (Director/Crítica) o Haiku (resto)
        chain.append((f"claude-{claude.tier.get()}", lambda: claude.ask(system, user, schema)))
    if not video_path:
        for name, env, base, model in OPENAI_COMPAT:
            if key(env):
                chain.append((name, lambda n=name, e=env, b=base, m=model: _openai_compat(n, e, b, m, system, user, schema)))
        if key("GEMINI_API_KEY"):   # Gemini después de los gratuitos: su cuota se guarda para ver videos
            chain.append(("gemini", lambda: _gemini(system, user, schema, None)))
        if key("CLOUDFLARE_ACCOUNT_ID") and key("CLOUDFLARE_API_TOKEN"):
            chain.append(("cf-oss", lambda: _cf_oss(system, user, schema)))
            chain.append(("cloudflare", lambda: _cloudflare(system, user, schema)))
    names[:] = [n for n, _ in chain]

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
    raise ProvidersDown(f"Ningún proveedor de texto respondió (probados: {tried or 'todos en pausa por cuota'})")
