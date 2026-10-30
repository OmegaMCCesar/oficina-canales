"""ESTRATEGA SEO Y DE CRECIMIENTO.

Título (palabra clave al inicio + curiosidad), títulos alternos para probar, descripción
optimizada (las 2 primeras líneas son las que se ven), etiquetas, hashtags, comentario fijado,
playlist sugerida. Los capítulos (largos) se calculan al producir con los tiempos reales.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import save_job
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief, insights_block

log = logging.getLogger("seo")


class SeoOut(BaseModel):
    title: str                  # ≤ 60 caracteres, palabra clave al inicio
    alt_titles: list[str]       # 2 alternativas para prueba A/B en Studio
    description: str            # 2 primeras líneas con keyword + gancho; luego resumen útil
    tags: list[str]             # 12-20, de específicas a generales
    hashtags: list[str]         # 3, sin espacios, con '#'
    keywords: list[str]         # 3-5 búsquedas reales a las que apunta
    pinned_comment: str         # comentario para fijar: pregunta + recurso
    playlist: str               # nombre de playlist donde encaja
    notes: str


SYSTEM = """Eres el ESTRATEGA SEO de un canal de YouTube en español de México.
- Piensa en cómo BUSCA la gente cuando algo falla: "refrigerador no enfría abajo", "lavadora LG no centrifuga".
- title: ≤ 60 caracteres, la búsqueda principal al inicio, y un gancho honesto al final. Nada de clickbait falso.
- alt_titles: 2 variantes con otro ángulo (beneficio, número, pregunta) para probar en YouTube Studio.
- description: primera línea = búsqueda principal + promesa; segunda = para quién es. Después 3-5 líneas
  útiles con términos relacionados, advertencia de seguridad si aplica, y la pregunta para comentarios.
  NO inventes links. Termina con los hashtags.
- tags: 12-20, mezcla marcas, modelos genéricos, síntomas y sinónimos regionales (refri/refrigerador/nevera).
- Shorts: incluye #Shorts entre los hashtags.
- pinned_comment: corto, invita a responder y anuncia el siguiente video del tema."""


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    if not sc:
        return job
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
FORMATO: {job.idea.format}
IDEA: {job.idea.title} — {job.idea.angle}
INTENCIÓN DE BÚSQUEDA: {job.idea.search_intent}
TÍTULO DEL GUIONISTA: {sc.title}
GANCHO: {sc.hook_text or sc.hook}
PREGUNTA PARA COMENTARIOS: {sc.comment_question}
NARRACIÓN: {sc.body[:1800]}"""
    tags = list(dict.fromkeys((sc.tags or []) + ["reparacion", "linea blanca"]))
    fallback = SeoOut(title=sc.title[:60], alt_titles=[sc.title[:60]], description=sc.description or sc.title,
                      tags=tags, hashtags=["#Shorts"] if job.idea.format == "short" else ["#reparacion"],
                      keywords=[job.idea.title], pinned_comment=sc.comment_question or "¿Qué falla tienes? Te leo",
                      playlist="Diagnóstico rápido", notes="dry-run")
    try:
        out = ask_json(SYSTEM, user, SeoOut, fallback=fallback)
    except Exception as e:
        log.warning("[%s] SEO no disponible (%s); sigo con lo del guionista", cfg.id, e)
        return job
    sc.title = out.title[:100]
    sc.alt_titles = out.alt_titles[:3]
    sc.description = out.description
    sc.tags = [t.strip()[:60] for t in out.tags if t.strip()][:25]
    hs = [h if h.startswith("#") else "#" + h for h in out.hashtags]
    if job.idea.format == "short" and "#Shorts" not in hs:
        hs.append("#Shorts")
    sc.hashtags = hs[:5]
    sc.keywords = out.keywords
    sc.pinned_comment = out.pinned_comment
    sc.playlist = out.playlist
    sc.seo_notes = out.notes
    save_job(job)
    log.info("[%s] SEO: '%s'", cfg.id, sc.title)
    return job


def build_description(job: VideoJob) -> str:
    """Descripción final con capítulos (largos) y hashtags; la usa el Publicador."""
    sc = job.script
    parts = [sc.description.strip()]
    if job.idea.format == "long" and len(sc.chapters) >= 3:
        lines = []
        for ch in sc.chapters:
            m, s = divmod(int(ch["t"]), 60)
            lines.append(f"{m}:{s:02d} {ch['title']}")
        if not lines[0].startswith("0:00"):
            lines.insert(0, "0:00 Inicio")
        parts.append("Capítulos:\n" + "\n".join(lines))
    if sc.comment_question and sc.comment_question not in sc.description:
        parts.append(sc.comment_question)
    tags_line = " ".join(h for h in sc.hashtags if h not in sc.description)
    if tags_line:
        parts.append(tags_line)
    return "\n\n".join(p for p in parts if p)[:4900]
