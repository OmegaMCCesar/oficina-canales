"""PUBLICADOR: sube a YouTube como privado con fecha programada.

Solo sube lo que el Director ya calendarizó (job.scheduled_for). Nunca publica inmediato.
"""
from __future__ import annotations

import logging

from ..config import ChannelConfig
from ..db import save_job
from ..models import VideoJob
from ..tools import youtube

log = logging.getLogger("publisher")


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    assert job.script and job.video_path and job.scheduled_for
    from pathlib import Path
    if not Path(job.video_path).exists():
        job.status = "scripted"          # el disco de Render se borró: se vuelve a producir
        job.scheduled_for = None
        job.thumbnail_paths, job.thumbnail_chosen = [], None
        log.warning("[%s] video perdido por reinicio; vuelve a producción: %s", cfg.id, job.script.title)
        return save_job(job)
    is_short = job.idea.format == "short"
    from .seo import build_description
    desc = build_description(job)
    if is_short and "#Shorts" not in desc:
        desc += "\n#Shorts"
    try:
        from ..config import settings
        if settings().PUBLISH_MODE == "scheduled":
            job.approved = True
        from datetime import datetime, timedelta, timezone
        auto = job.approved and job.scheduled_for and job.scheduled_for > datetime.now(timezone.utc) + timedelta(minutes=15)
        vid = youtube.upload_video(
            cfg.id, job.video_path, job.script.title, desc, job.script.tags,
            publish_at=job.scheduled_for if auto else None, thumbnail_path=job.thumbnail_chosen, is_short=is_short,
        )
        job.youtube_video_id = vid
        job.status = "scheduled"
        log.info("[%s] programado %s -> %s @ %s", cfg.id, job.script.title, vid, job.scheduled_for)
        save_job(job)
        from .showrunner import on_uploaded
        on_uploaded(cfg, job)
    except Exception as e:
        job.status = "failed"
        job.error = f"upload: {e}"
        log.exception("[%s] fallo al subir", cfg.id)
    return save_job(job)
