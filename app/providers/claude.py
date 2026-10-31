"""Claude (API de Anthropic): el cerebro de pago de la oficina.

- Sonnet 5.5 ("smart"): Director (decisiones, revisión de animación, calendario) y Crítica de video (ve imágenes).
- Haiku 5.5 ("fast"): el resto de los agentes.
Respuesta tipada: se fuerza una herramienta con el JSON Schema del modelo pydantic (salida siempre válida).
Lleva la cuenta de tokens y costo estimado por día en Firestore (global/usage/claude-AAAA-MM-DD).
"""
from __future__ import annotations

import base64
import contextvars
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from . import key

log = logging.getLogger("providers.claude")
T = TypeVar("T", bound=BaseModel)

MODELS = {"smart": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"}
# USD por millón de tokens (entrada, salida). Haiku sube si el prompt pasa de 100K (aquí nunca pasa).
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
API = "https://api.anthropic.com/v1/messages"

tier: contextvars.ContextVar[str] = contextvars.ContextVar("claude_tier", default="fast")


class smart:
    """with smart(): ...  -> las llamadas dentro usan Sonnet (decisiones del Director, Crítica)."""
    def __enter__(self):
        self._tok = tier.set("smart")

    def __exit__(self, *a):
        tier.reset(self._tok)


def available() -> bool:
    return bool(key("ANTHROPIC_API_KEY"))


def _record(model: str, usage: dict) -> None:
    try:
        from ..db import store
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc_id = f"claude-{day}"
        d = store().get("global", "usage", doc_id) or {"id": doc_id, "day": day, "calls": 0, "in": 0, "out": 0, "usd": 0.0, "by_model": {}}
        i, o = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0), usage.get("output_tokens", 0)
        pin, pout = PRICES.get(model, (2.0, 10.0))
        cost = i / 1e6 * pin + o / 1e6 * pout
        d["calls"] += 1
        d["in"] += i
        d["out"] += o
        d["usd"] = round(d["usd"] + cost, 5)
        bm = d["by_model"].setdefault(model, {"calls": 0, "usd": 0.0})
        bm["calls"] += 1
        bm["usd"] = round(bm["usd"] + cost, 5)
        store().put("global", "usage", doc_id, d)
    except Exception as e:      # contar nunca debe tumbar un video
        log.info("no pude registrar el gasto: %s", e)


def _inline(schema: dict) -> dict:
    """Resuelve $ref/$defs del JSON Schema de pydantic para que la herramienta sea autocontenida."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


def ask(system: str, user: str, schema: Type[T], image: Path | None = None, model_tier: str | None = None,
        max_tokens: int = 8000) -> T:
    model = MODELS[model_tier or tier.get()]
    content: list = []
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(image.read_bytes()).decode()}})
    content.append({"type": "text", "text": user})
    body = {
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [{"name": "responder", "description": "Entrega tu respuesta final con esta estructura.",
                   "input_schema": _inline(schema.model_json_schema())}],
        "tool_choice": {"type": "tool", "name": "responder"},
    }
    r = httpx.post(API, json=body, timeout=180, headers={
        "x-api-key": key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:300]}")
    data = r.json()
    _record(model, data.get("usage", {}))
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if not block:
        raise RuntimeError("Claude no devolvió la estructura")
    return schema.model_validate(block["input"])


def usage_summary() -> dict:
    """Gasto de hoy y del mes (estimado con los precios de lista)."""
    from ..db import store
    now = datetime.now(timezone.utc)
    docs = [d for d in store().list("global", "usage") if d.get("day", "").startswith(now.strftime("%Y-%m"))]
    today = next((d for d in docs if d.get("day") == now.strftime("%Y-%m-%d")), {})
    return {"today_usd": round(today.get("usd", 0.0), 3), "month_usd": round(sum(d.get("usd", 0.0) for d in docs), 3),
            "month_calls": sum(d.get("calls", 0) for d in docs), "enabled": available()}
