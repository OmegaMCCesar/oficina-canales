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


def key(name: str) -> str:
    return os.environ.get(name, "").strip()


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
        # cuota agotada: 1 hora fuera; otro error: 2 minutos
        self.cool_until[name] = time.time() + (3600 if quota else 120)

    def report(self) -> dict:
        return {"usados": dict(self.used), "fallas": dict(self.fails), "ultimo_error": self.last_error}


health = Health()


def is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(t in s for t in ("429", "quota", "rate limit", "resource_exhausted", "exceeded", "insufficient"))
