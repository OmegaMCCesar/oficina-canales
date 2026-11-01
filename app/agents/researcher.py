"""INVESTIGADOR: minería de temas.

Fuentes: seeds del canal, comentarios recientes, memoria del canal (insights),
y en gaming, los archivos nuevos en la carpeta de partidas.
Entrega ideas con score 0-10.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from ..config import ChannelConfig, settings
from ..db import ideas as stored_ideas, jobs, save_idea
from ..llm import ask_json
from ..models import Idea
from ..tools import youtube
from .base import channel_brief, insights_block

log = logging.getLogger("researcher")


class IdeaOut(BaseModel):
    title: str
    angle: str
    format: str = "short"
    search_intent: str = ""
    score: float
    source_media: str | None = None


class ResearchOut(BaseModel):
    ideas: list[IdeaOut]
    trend_notes: str = ""


SYSTEM = """Eres el INVESTIGADOR de un canal de YouTube. Tu trabajo es proponer temas
que la audiencia realmente quiere ver, no temas genéricos. Puntúas cada idea de 0 a 10
considerando: intención de búsqueda, cuánto encaja con lo que ya funcionó en el canal
(memoria), novedad frente a lo ya publicado, y facilidad de producción.
Devuelve entre 6 y 10 ideas. Nunca repitas títulos ya publicados o en cola."""


def _gaming_inbox_files(cfg: ChannelConfig) -> list[str]:
    inbox = Path(settings().GAMING_INBOX)
    if not inbox.exists():
        return []
    from collections import Counter
    # una partida buena da para varios videos (shorts de distintas jugadas + un largo); los rechazados no cuentan
    used = Counter(j.idea.source_media for j in jobs(cfg.id)
                   if j.idea.source_media and j.status not in ("rejected", "failed"))
    files = [p for p in sorted(inbox.iterdir()) if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".3gp")]
    return [str(p) for p in files if used[str(p)] < 3]


def run(cfg: ChannelConfig) -> list[Idea]:
    comments = youtube.recent_comments(cfg.id)
    published = [j.idea.title for j in jobs(cfg.id) if j.status in ("scheduled", "published")]
    queued = [j.idea.title for j in jobs(cfg.id) if j.status not in ("rejected", "failed", "published")]
    pending_ideas = [i.title for i in stored_ideas(cfg.id)]
    from . import scout
    inbox = _gaming_inbox_files(cfg) if cfg.production.kind == "gameplay" else []
    # solo partidas que el Analista ya vio y en las que encontró jugadas
    inbox = [p for p in inbox if (scout.report(cfg.id, p) or {}).get("moments")]
    if cfg.production.kind == "gameplay":
        from .base import content_mode
        if content_mode(cfg.id) == "wiki":
            inbox = []

    user = f"""{channel_brief(cfg)}

{insights_block(cfg.id)}

SEMILLAS DE TEMAS: {cfg.topic_seeds}
COMENTARIOS RECIENTES DE LA AUDIENCIA:
{chr(10).join('- ' + c for c in comments) or '- (ninguno)'}
YA PUBLICADO: {published[-20:]}
EN COLA: {queued}
IDEAS PENDIENTES (no las repitas): {pending_ideas[-20:]}
"""
    if cfg.production.kind == "gameplay":
        user += f"""
PARTIDAS GRABADAS DISPONIBLES (cada idea DEBE copiar EXACTA una de estas rutas en source_media; no inventes partidas).
Debajo de cada una está lo que el Analista VIO en ella: el héroe, el resultado y las jugadas del título deben salir
de ahí (si el resultado no es visible, no lo pongas en el título). Con una partida buena propón 1-3 shorts (una
jugada cada uno) y, si dura 8+ min con varias jugadas y aún no tiene su largo, 1 largo (format "long") de
PARTIDA COMPLETA editada y comentada («Partida completa con <héroe>: …»):
{chr(10).join(f"- {p}{chr(10)}  {scout.brief(scout.report(cfg.id, p))}{chr(10)}  ya usadas: {[j.idea.title for j in jobs(cfg.id) if j.idea.source_media == p and j.status not in ('rejected', 'failed')]}" for p in inbox) or '(ninguna: no propongas ideas de partidas; devuelve la lista vacía)'}
"""

    fallback = ResearchOut(ideas=[
        IdeaOut(title=s, angle="Problema frecuente con alta intención de búsqueda", score=7.5 + (i % 3) * 0.5,
                format="short" if i % 3 else "long", search_intent="alta",
                source_media=(inbox[i % len(inbox)] if inbox else None))
        for i, s in enumerate(cfg.topic_seeds[:6])
    ], trend_notes="dry-run")

    out = ask_json(SYSTEM, user, ResearchOut, fallback=fallback)
    result: list[Idea] = []
    real = set(inbox)
    for io in out.ideas:
        if any(b.lower() in io.title.lower() for b in cfg.banned_topics):
            continue
        if cfg.production.kind == "gameplay":
            if io.source_media not in real:
                io.source_media = None          # la IA no puede inventar partidas
            if not io.source_media:
                continue                        # sin grabación real no es idea de gameplay (los héroes van aparte)
        idea = Idea(channel_id=cfg.id, title=io.title, angle=io.angle,
                    format="long" if io.format == "long" else "short",
                    search_intent=io.search_intent, score=io.score, source_media=io.source_media)
        save_idea(idea)
        result.append(idea)
    log.info("[%s] %d ideas nuevas. %s", cfg.id, len(result), out.trend_notes)
    return result
