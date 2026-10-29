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
    is_short = job.idea.format == "short"
    desc = job.script.description
    if is_short and "#Shorts" not in desc:
        desc += "\n#Shorts"
    try:
        vid = youtube.upload_video(
            cfg.id, job.video_path, job.script.title, desc, job.script.tags,
            publish_at=job.scheduled_for, thumbnail_path=job.thumbnail_chosen, is_short=is_short,
        )
        job.youtube_video_id = vid
        job.status = "scheduled"
        log.info("[%s] programado %s -> %s @ %s", cfg.id, job.script.title, vid, job.scheduled_for)
    except Exception as e:
        job.status = "failed"
        job.error = f"upload: {e}"
        log.exception("[%s] fallo al subir", cfg.id)
    return save_job(job)
