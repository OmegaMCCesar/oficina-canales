"""Ciclo diario de la oficina para un canal.

Orden:
  1. Analista   -> métricas + insights (memoria)
  2. Investigador -> ideas nuevas (lee memoria)
  3. Director   -> aprueba ideas -> VideoJobs
  4. Guionista  -> guion de cada job aprobado
  5. Productor  -> mp4
  6. Miniaturas -> 2 variantes, elige 1
  7. Director   -> asigna horario respetando cadencia
  8. Publicador -> sube privado + programado
"""
from __future__ import annotations

import logging
import traceback

from . import producers
from .agents import analyst, director, publisher, researcher, thumbnails, writer
from .config import get_channel, load_channels
from .db import add_activity as act, jobs, log_run, save_job

log = logging.getLogger("orchestrator")

MAX_PRODUCE_PER_RUN = 2   # tope por ciclo (cuidar cuota gratuita de Gemini)


def _step(job, fn, cfg):
    try:
        return fn(cfg, job)
    except Exception as e:
        job.status = "failed"
        job.error = f"{fn.__module__}: {e}"
        save_job(job)
        log.error("[%s] %s falló en job %s\n%s", cfg.id, fn.__module__, job.id, traceback.format_exc())
        return job


def _keepalive(stop):
    """Render gratis apaga el servidor tras 15 min sin visitas: durante el ciclo nos visitamos solos."""
    import httpx

    from .config import settings
    url = settings().PUBLIC_URL.rstrip("/") + "/health"
    while not stop.wait(240):
        try:
            httpx.get(url, timeout=20)
        except Exception:
            pass


def run_channel(channel_id: str, publish: bool = True) -> dict:
    """Ejecuta el ciclo y SIEMPRE deja registro (aunque falle) para que el panel lo muestre."""
    import threading
    stop = threading.Event()
    threading.Thread(target=_keepalive, args=(stop,), daemon=True).start()
    try:
        return _run_logged(channel_id, publish)
    finally:
        stop.set()


def _run_logged(channel_id: str, publish: bool) -> dict:
    from datetime import datetime, timezone
    started = datetime.now(timezone.utc).isoformat()
    rid = started[:19].replace(":", "")
    log_run(channel_id, {"channel": channel_id, "status": "running", "started": started}, rid)
    try:
        report = _run_channel(channel_id, publish)
        report.update(status="ok", started=started, finished=datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.error("[%s] ciclo falló\n%s", channel_id, traceback.format_exc())
        act(channel_id, "director", f"El ciclo falló: {str(e)[:120]}", kind="bad")
        report = {"channel": channel_id, "status": "failed", "error": str(e)[:400], "started": started,
                  "finished": datetime.now(timezone.utc).isoformat()}
    log_run(channel_id, report, rid)
    return report


def _run_channel(channel_id: str, publish: bool = True) -> dict:
    cfg = get_channel(channel_id)
    report: dict = {"channel": channel_id}

    act(cfg.id, "director", "¡Arranca el ciclo, equipo!", kind="ok")
    act(cfg.id, "analista", "Revisando métricas de los videos publicados…")
    a = analyst.run(cfg)
    report["analyst"] = a.summary if a else "sin métricas"
    if a and a.insights:
        act(cfg.id, "analista", f"Aprendí: {a.insights[0].statement}", to="director", kind="ok")
    else:
        act(cfg.id, "analista", "Aún no hay métricas para aprender")

    act(cfg.id, "investigador", "Buscando temas: comentarios, tendencias y memoria del canal…")
    new_ideas = researcher.run(cfg)
    report["ideas"] = len(new_ideas)
    best = max(new_ideas, key=lambda i: i.score).title if new_ideas else "ninguna"
    act(cfg.id, "investigador", f"{len(new_ideas)} ideas. La mejor: {best}", to="director")

    act(cfg.id, "director", "Revisando ideas y la cola de videos…")
    d = director.run(cfg)
    report["approved"] = len(d.approved_idea_ids)
    report["requests_to_human"] = d.requests_to_human
    if d.requests_to_human:
        act(cfg.id, "director", "César: " + d.requests_to_human[0], kind="ask")

    produced = 0
    for job in jobs(cfg.id, "approved") + jobs(cfg.id, "scripted") + jobs(cfg.id, "produced"):
        if produced >= MAX_PRODUCE_PER_RUN:
            break
        title = job.idea.title[:60]
        if job.status == "approved":
            act(cfg.id, "director", f"Aprobada: {title}", to="guionista")
            act(cfg.id, "guionista", f"Escribiendo guion: {title}")
            job = _step(job, writer.run, cfg)
            if job.status == "scripted":
                act(cfg.id, "guionista", f"Guion listo ({len(job.script.scenes or job.script.highlights)} partes)", to="productor")
        if job.status == "scripted":
            act(cfg.id, "productor", f"Generando imágenes, voz y video: {title}")
            job = _step(job, producers.run, cfg)
            if job.status == "produced":
                act(cfg.id, "productor", "Video renderizado", to="miniaturas")
        if job.status == "produced":
            act(cfg.id, "miniaturas", f"Diseñando 2 miniaturas: {title}")
            job = _step(job, thumbnails.run, cfg)
            if job.status == "thumbnailed":
                act(cfg.id, "miniaturas", "Miniatura elegida", to="director")
                produced += 1
        if job.status == "failed":
            act(cfg.id, "director", f"Falló: {title} ({(job.error or '')[:80]})", kind="bad")
    report["produced"] = produced

    uploaded = []
    if publish:
        for job in director.assign_slots(cfg):
            act(cfg.id, "director", f"A YouTube: {job.script.title[:60]}", to="publicador")
            act(cfg.id, "publicador", "Subiendo video como privado…")
            job = publisher.run(cfg, job)
            if job.status == "scheduled":
                act(cfg.id, "publicador", "Subido a YouTube Studio (privado)", to="portal", kind="ok")
            else:
                act(cfg.id, "publicador", f"No se pudo subir: {(job.error or '')[:80]}", kind="bad")
            if job.status == "scheduled":
                uploaded.append({"title": job.script.title, "at": job.scheduled_for.isoformat(),
                                 "video_id": job.youtube_video_id})
    report["scheduled"] = uploaded
    report["failed"] = [{"id": j.id, "error": j.error} for j in jobs(cfg.id, "failed")][:5]

    act(cfg.id, "director", f"Ciclo terminado: {produced} video(s) listos", kind="ok")
    log.info("[%s] ciclo terminado: %s", cfg.id, report)
    return report


def run_all(publish: bool = True) -> list[dict]:
    return [run_channel(cid, publish) for cid in load_channels()]
