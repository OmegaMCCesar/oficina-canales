"""GERENTE DE MONETIZACIÓN: dinero desde el primer video, sin esperar al Programa de Socios.

1. Por video: detecta herramientas y refacciones que se mencionan y arma el bloque de
   "Herramientas de este video" con links de afiliado + aviso de afiliados + link al sitio de César.
   - Links que César pegó en el panel (Mercado Libre, Amazon, tiendas) se reutilizan siempre.
   - Si hay AMAZON_ASSOCIATE_TAG, arma links de búsqueda de Amazon México con la etiqueta.
   - Lo que no tenga link queda en "pendientes de enlazar" para que César lo genere una vez.
2. Por ciclo: mide el avance hacia monetizar (suscriptores, horas de largos, vistas de Shorts).
Nunca inventa links ni pone links sin comisión que distraigan.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from pydantic import BaseModel

from ..config import ChannelConfig, settings
from ..db import add_activity, save_job, store
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief

log = logging.getLogger("money")

DISCLOSURE = "Algunos enlaces son de afiliado: si compras, el canal recibe una pequeña comisión sin costo extra para ti. Así nos ayudas a seguir haciendo videos."
NEW_RULES = date(2027, 2, 1)


class Product(BaseModel):
    name: str        # "Multímetro digital con medición de capacitancia"
    query: str       # búsqueda corta para tienda: "multimetro capacitancia"
    kind: str = "herramienta"   # herramienta | refaccion | consumible


class ProductsOut(BaseModel):
    products: list[Product]


SYSTEM = """Eres el GERENTE DE MONETIZACIÓN de un canal de reparación de línea blanca. Del guion, saca
las herramientas, refacciones o consumibles que el espectador realmente necesitaría comprar para seguir
el video (máximo 4, solo los que se mencionan o son indispensables). Nada de productos genéricos que no
aporten. Si el video no necesita comprar nada, devuelve la lista vacía."""

KEYWORDS = {
    "multímetro": "multimetro digital capacitancia", "capacitor": "capacitor de arranque", "termostato": "termostato refrigerador",
    "bomba de desagüe": "bomba de desague lavadora", "resistencia": "resistencia deshielo refrigerador",
    "pinza amperimétrica": "pinza amperimetrica", "manómetro": "manometro refrigeracion", "termómetro": "termometro digital",
}


def _slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


def links(channel_id: str) -> list[dict]:
    return store().list(channel_id, "affiliate_links")


def pending(channel_id: str) -> list[dict]:
    return sorted(store().list(channel_id, "pending_products"), key=lambda d: -d.get("count", 0))


def save_link(channel_id: str, product: str, url: str) -> dict:
    doc = {"id": _slug(product), "product": product, "url": url, "created_at": datetime.now(timezone.utc).isoformat()}
    store().put(channel_id, "affiliate_links", doc["id"], doc)
    store().delete(channel_id, "pending_products", doc["id"])
    return doc


def _match(name: str, catalog: list[dict]) -> dict | None:
    s = set(_slug(name).split("-"))
    best, score = None, 0.0
    for d in catalog:
        c = set(d["id"].split("-"))
        j = len(s & c) / max(1, len(s | c))
        if j > score:
            best, score = d, j
    return best if score >= 0.34 else None


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    if not sc or cfg.production.kind != "generated":
        return job
    text = (sc.title + " " + sc.body).lower()
    fb = [Product(name=k.capitalize(), query=v) for k, v in KEYWORDS.items() if k in text][:4]
    try:
        out = ask_json(SYSTEM, f"{channel_brief(cfg)}\nTÍTULO: {sc.title}\nGUION: {sc.body[:2500]}",
                       ProductsOut, fallback=ProductsOut(products=fb))
        prods = out.products[:4]
    except Exception as e:
        log.warning("[%s] monetización sin productos (%s)", cfg.id, e)
        prods = fb
    catalog = links(cfg.id)
    tag = settings().AMAZON_ASSOCIATE_TAG.strip()
    items, missing = [], []
    for p in prods:
        hit = _match(p.name, catalog)
        if hit:
            items.append({"name": p.name, "query": p.query, "url": hit["url"], "kind": p.kind, "src": "catálogo"})
        elif tag:
            items.append({"name": p.name, "query": p.query, "kind": p.kind, "src": "amazon",
                          "url": f"https://www.amazon.com.mx/s?k={quote_plus(p.query)}&tag={quote_plus(tag)}"})
        else:
            items.append({"name": p.name, "query": p.query, "kind": p.kind, "url": "", "src": "pendiente"})
            missing.append(p)
    for p in missing:   # lista para que César genere el link una sola vez
        pid = _slug(p.name)
        d = store().get(cfg.id, "pending_products", pid) or {"id": pid, "product": p.name, "query": p.query, "count": 0, "videos": []}
        d["count"] += 1
        d["videos"] = (d["videos"] + [sc.title[:80]])[-5:]
        store().put(cfg.id, "pending_products", pid, d)
    lines = [f"🛠️ {i['name']}: {i['url']}" for i in items if i["url"]]
    block = []
    if lines:
        block += ["Herramientas y refacciones de este video:", *lines, DISCLOSURE]
    if cfg.site_url:
        block.append(f"{cfg.site_cta or '📚 Más guías en'} {cfg.site_url}")
    sc.products = items
    sc.affiliate_block = "\n".join(block)
    save_job(job)
    if missing:
        add_activity(cfg.id, "monetizacion", f"Faltan links de afiliado: {', '.join(p.name for p in missing)[:90]}", kind="ask")
    log.info("[%s] monetización: %d con link, %d pendientes", cfg.id, len(lines), len(missing))
    return job


def thresholds(today: date | None = None) -> dict:
    today = today or date.today()
    new = today >= NEW_RULES
    return {
        "fan": {"label": "Nivel 1: membresías, Super Thanks y YouTube Shopping", "subs": 500, "hours": 3000, "shorts": 3_000_000},
        "ypp": {"label": "Programa de Socios (anuncios)" + (" — reglas 2027" if new else " — reglas hasta 31 ene 2027"),
                "subs": 1000, "hours": 8000 if new else 4000, "shorts": 20_000_000 if new else 10_000_000},
        "changes_on": str(NEW_RULES), "new_rules": new,
        "after_change": {"subs": 1000, "hours": 8000, "shorts": 20_000_000},
    }


def progress(cfg: ChannelConfig) -> dict | None:
    from ..tools import youtube
    if settings().MODE == "live" and not youtube.is_authorized(cfg.id):
        return None
    try:
        p = youtube.channel_progress(cfg.id)
    except Exception as e:
        log.warning("[%s] progreso no disponible: %s", cfg.id, e)
        return None
    p["at"] = datetime.now(timezone.utc).isoformat()
    p["thresholds"] = thresholds()
    prev = store().get(cfg.id, "money", "progress") or {}
    store().put(cfg.id, "money", "progress", p)
    if prev.get("subs") is not None and p["subs"] > prev["subs"]:
        add_activity(cfg.id, "monetizacion", f"+{p['subs'] - prev['subs']} suscriptores · van {p['subs']}/500 para el nivel 1",
                     to="director", kind="ok")
    return p
