"""Router de proveedores de IA.

Cada capacidad (texto, imagen, voz, stock, música) tiene una lista ordenada de proveedores.
Se usa el primero que tenga llave configurada y responda; si falla o se acaba su cuota
gratuita, cae al siguiente. Así podemos sumar todos los niveles gratuitos posibles.

Llaves (todas opcionales; variables de entorno en Render):
  GEMINI_API_KEY           texto (Google AI Studio)
  GROQ_API_KEY             texto + Whisper (console.groq.com)
  CEREBRAS_API_KEY         texto (cloud.cerebras.ai)
  OPENROUTER_API_KEY       texto, modelos :free (openrouter.ai)
  GITHUB_MODELS_TOKEN      texto (github.com/settings/tokens, permiso models:read)
  MISTRAL_API_KEY          texto (console.mistral.ai)
  CLOUDFLARE_ACCOUNT_ID +
  CLOUDFLARE_API_TOKEN     imágenes FLUX + texto Llama (dash.cloudflare.com, Workers AI)
  PEXELS_API_KEY           fotos y videos de stock (pexels.com/api)
  PIXABAY_API_KEY          fotos y videos de stock (pixabay.com/api/docs)
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

log = logging.getLogger("providers")


_cache: dict[str, str] = {}


def key(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v and name == "CLOUDFLARE_ACCOUNT_ID" and os.environ.get("CLOUDFLARE_API_TOKEN"):
        v = cloudflare_account_id()
    return v


def cloudflare_tokens() -> list[str]:
    """Token principal y alterno: si uno no tiene permiso de Workers AI, se prueba el otro."""
    return [t for t in (key("CLOUDFLARE_API_TOKEN"), key("CLOUDFLARE_API_TOKEN_ALT")) if t]


def cloudflare_post(model: str, payload: dict, timeout: int = 120):
    import httpx
    acc = key("CLOUDFLARE_ACCOUNT_ID")
    last = None
    for tok in cloudflare_tokens():
        r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}",
                       headers={"Authorization": f"Bearer {tok}"}, json=payload, timeout=timeout)
        if r.status_code in (401, 403):
            last = RuntimeError(f"{r.status_code} {r.text[:160]}")
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        return r.json()
    raise last or RuntimeError("sin token de Cloudflare")


def cloudflare_account_id() -> str:
    """Si solo hay token, pregunta a Cloudflare a qué cuenta pertenece (y lo recuerda)."""
    if "cf_acc" in _cache:
        return _cache["cf_acc"]
    import httpx
    try:
        r = httpx.get("https://api.cloudflare.com/client/v4/accounts",
                      headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN'].strip()}"}, timeout=20)
        res = r.json().get("result") or []
        _cache["cf_acc"] = res[0]["id"] if res else ""
    except Exception as e:
        log.warning("no pude obtener el Account ID de Cloudflare: %s", e)
        _cache["cf_acc"] = ""
    return _cache["cf_acc"]


class Health:
    """Recuerda qué proveedor falló hace poco para no insistir (enfriamiento)."""

    def __init__(self) -> None:
        self.cool_until: dict[str, float] = defaultdict(float)
        self.used: dict[str, int] = defaultdict(int)
        self.fails: dict[str, int] = defaultdict(int)
        self.last_error: dict[str, str] = {}

    def ok(self, name: str) -> bool:
        return time.time() >= self.cool_until[name]

    def success(self, name: str) -> None:
        self.used[name] += 1

    def failure(self, name: str, err: Exception, quota: bool = False) -> None:
        self.fails[name] += 1
        self.last_error[name] = str(err)[:200]
        self.cool_until[name] = time.time() + cooldown_for(err, quota)

    def wait_hint(self, names: list[str]) -> float:
        """Segundos hasta que vuelva el primero de estos proveedores."""
        now = time.time()
        return max(0.0, min((self.cool_until[n] for n in names), default=now) - now)

    def report(self) -> dict:
        return {"usados": dict(self.used), "fallas": dict(self.fails), "ultimo_error": self.last_error}


health = Health()


def cooldown_for(err: Exception, quota: bool) -> float:
    """Límite por minuto: esperar lo que pide el proveedor. Cuota del día: 1 hora. Modelo inexistente: 1 día."""
    import re
    s = str(err).lower()
    if "model_not_found" in s or "does not exist" in s or "no such model" in s or ("404" in s and "model" in s):
        return 86400
    if "credit balance" in s or "billing" in s:
        return 3 * 3600     # sin saldo en Anthropic: los modelos gratis toman el relevo
    if "request too large" in s or "context length" in s or "maximum context" in s:
        return 3600     # este prompt nunca cabrá en ese modelo
    if not quota:
        return 90
    if any(t in s for t in ("perday", "per day", "tokens per day", "requests per day", "(tpd)", "(rpd)", "daily")):
        return 3600
    m = re.search(r"(?:try again in|retry in|retrydelay['\":\s]+)\s*(?:(\d+)m)?\s*([\d.]+)\s*s", s)
    if m:
        return min(150.0, (int(m.group(1) or 0) * 60) + float(m.group(2)) + 2)
    if any(t in s for t in ("per minute", "(tpm)", "(rpm)", "rate limit")):
        return 60
    return 900


def is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(t in s for t in ("429", "quota", "rate limit", "resource_exhausted", "exceeded", "insufficient",
                                 "overloaded", "529", "credit balance"))
