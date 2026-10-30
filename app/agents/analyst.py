"""ANALISTA: cierra el loop de aprendizaje.

1. Marca como 'published' los programados cuya fecha ya pasó.
2. Recolecta métricas 24h / 7d / 30d de cada video publicado.
3. Con las métricas acumuladas, escribe INSIGHTS en la memoria del canal:
   qué temas/ganchos/miniaturas/horarios funcionan. El Director los lee en el siguiente ciclo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import insights as stored_insights, jobs, metrics as stored_metrics, save_insight, save_job, save_metrics, store
from ..llm import ask_json
from ..models import Insight
from ..tools import youtube
from .base import channel_brief

log = logging.getLogger("analyst")


class InsightOut(BaseModel):
    kind: str
    statement: str
    evidence: str
    confidence: float
    action: str


class AnalystOut(BaseModel):
    insights: list[InsightOut]
    retire_insight_ids: list[str] = []     # insights viejos que ya no se sostienen
    summary: str


SYSTEM = """Eres el ANALISTA de un canal de YouTube. Recibes métricas por video y la memoria actual.
Tu salida son INSIGHTS accionables para el Director: patrones sobre temas, ganchos, miniaturas,
horarios y formatos. Reglas:
- Un insight necesita al menos 2 videos de evidencia; con 1 video, confianza ≤ 0.4.
- Retención (avg_view_pct) y CTR pesan más que vistas totales.
- Si un insight anterior se contradice con datos nuevos, pídelo retirar.
- Máximo 6 insights nuevos por ciclo. kind ∈ topic|hook|thumbnail|timing|format|general."""


def _collect(cfg: ChannelConfig) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    # estado REAL en YouTube (privado, programado o ya público, aunque César lo haya publicado a mano)
    sched = [j for j in jobs(cfg.id, "scheduled") if j.youtube_video_id]
    try:
        st = youtube.video_status(cfg.id, [j.youtube_video_id for j in sched])
    except Exception as e:
        log.warning("[%s] no pude leer el estado de los videos: %s", cfg.id, e)
        st = {}
    for job in sched:
        s = st.get(job.youtube_video_id)
        if s and s.get("privacy") == "public":
            job.status = "published"
            pa = s.get("published_at")
            job.published_at = datetime.fromisoformat(pa.replace("Z", "+00:00")) if pa else now
            save_job(job)
    for job in jobs(cfg.id, "published"):
        if not job.youtube_video_id or not job.published_at:
            continue
        if job.script and job.script.pinned_comment and not job.comment_posted:
            if youtube.post_comment(cfg.id, job.youtube_video_id, job.script.pinned_comment):
                job.comment_posted = True
                save_job(job)
        age = now - job.published_at
        existing = {m.window for m in stored_metrics(cfg.id) if m.youtube_video_id == job.youtube_video_id}
        for window, delta in (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))):
            if age >= delta and window not in existing:
                m = youtube.video_metrics(cfg.id, job.youtube_video_id, window, job.script.title if job.script else "")
                m.job_id = job.id
                save_metrics(m)
                n += 1
    return n


def run(cfg: ChannelConfig) -> AnalystOut | None:
    collected = _collect(cfg)
    ms = stored_metrics(cfg.id)
    if not ms:
        log.info("[%s] sin métricas aún", cfg.id)
        return None
    current = stored_insights(cfg.id)
    jobs_by_id = {j.id: j for j in jobs(cfg.id)}

    rows = []
    for m in sorted(ms, key=lambda x: (x.youtube_video_id, x.window)):
        j = jobs_by_id.get(m.job_id or "")
        hook = j.script.hook if j and j.script else ""
        thumb = (j.thumbnail_chosen or "").split("/")[-1] if j else ""
        fmt = j.idea.format if j else "?"
        hour = j.published_at.astimezone().hour if j and j.published_at else "?"
        rows.append(f"- [{m.window}] {fmt} | {m.title} | vistas={m.views} ctr={m.ctr}% ret={m.avg_view_pct}% "
                    f"subs=+{m.subs_gained} | hora={hour} | gancho='{hook[:60]}' | thumb={thumb}")

    user = f"""{channel_brief(cfg)}

MÉTRICAS ({len(ms)} filas, {collected} nuevas este ciclo):
{chr(10).join(rows)}

INSIGHTS ACTUALES EN MEMORIA:
{chr(10).join(f'- id={i.id} [{i.kind}] {i.statement} (conf {i.confidence})' for i in current) or '- (ninguno)'}
"""
    top = max(ms, key=lambda x: x.avg_view_pct)
    fallback = AnalystOut(insights=[InsightOut(
        kind="topic", statement=f"'{top.title}' tuvo la mejor retención ({top.avg_view_pct}%)",
        evidence=top.youtube_video_id, confidence=0.4, action="Programar una variante o secuela",
    )], summary="dry-run")
    out = ask_json(SYSTEM, user, AnalystOut, fallback=fallback)

    for rid in out.retire_insight_ids:
        store().delete(cfg.id, "insights", rid)
    for i in out.insights[:6]:
        kind = i.kind if i.kind in ("topic", "hook", "thumbnail", "timing", "format", "general") else "general"
        save_insight(Insight(channel_id=cfg.id, kind=kind, statement=i.statement, evidence=i.evidence,
                             confidence=max(0, min(1, i.confidence)), action=i.action))
    log.info("[%s] Analista: %d insights nuevos. %s", cfg.id, len(out.insights), out.summary)
    return out
