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
from .db import jobs, log_run, save_job

log = logging.getLogger("orchestrator")

MAX_PRODUCE_PER_RUN = 4   # tope de costo por ciclo


def _step(job, fn, cfg):
    try:
        return fn(cfg, job)
    except Exception as e:
        job.status = "failed"
        job.error = f"{fn.__module__}: {e}"
        save_job(job)
        log.error("[%s] %s falló en job %s\n%s", cfg.id, fn.__module__, job.id, traceback.format_exc())
        return job


def run_channel(channel_id: str, publish: bool = True) -> dict:
    cfg = get_channel(channel_id)
    report: dict = {"channel": channel_id}

    a = analyst.run(cfg)
    report["analyst"] = a.summary if a else "sin métricas"

    report["ideas"] = len(researcher.run(cfg))

    d = director.run(cfg)
    report["approved"] = len(d.approved_idea_ids)
    report["requests_to_human"] = d.requests_to_human

    produced = 0
    for job in jobs(cfg.id, "approved") + jobs(cfg.id, "scripted") + jobs(cfg.id, "produced"):
        if produced >= MAX_PRODUCE_PER_RUN:
            break
        if job.status == "approved":
            job = _step(job, writer.run, cfg)
        if job.status == "scripted":
            job = _step(job, producers.run, cfg)
        if job.status == "produced":
            job = _step(job, thumbnails.run, cfg)
            produced += 1
    report["produced"] = produced

    uploaded = []
    if publish:
        for job in director.assign_slots(cfg):
            job = publisher.run(cfg, job)
            if job.status == "scheduled":
                uploaded.append({"title": job.script.title, "at": job.scheduled_for.isoformat(),
                                 "video_id": job.youtube_video_id})
    report["scheduled"] = uploaded
    report["failed"] = [{"id": j.id, "error": j.error} for j in jobs(cfg.id, "failed")][:5]

    log_run(cfg.id, report)
    log.info("[%s] ciclo terminado: %s", cfg.id, report)
    return report


def run_all(publish: bool = True) -> list[dict]:
    return [run_channel(cid, publish) for cid in load_channels()]
