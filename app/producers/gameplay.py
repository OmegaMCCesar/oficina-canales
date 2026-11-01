"""PRODUCTOR-EDITOR para canal gaming: corta highlights de la partida, rótulos y locución."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import MEDIA_DIR, ChannelConfig
from ..db import save_job
from ..models import VideoJob
from ..tools import ffmpeg, tts

log = logging.getLogger("producer.gameplay")


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    if not (job.script and job.script.highlights and job.idea.source_media):
        raise ValueError("job de gameplay sin guion, highlights o partida fuente")
    src = Path(job.idea.source_media)
    if not src.exists():
        job.status, job.error = "failed", f"no existe {src}"
        return save_job(job)

    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    portrait = job.idea.format == "short"

    clips: list[Path] = []
    for k, h in enumerate(job.script.highlights):
        raw = ffmpeg.cut(src, h["start"], h["end"], folder / f"hl_{k:02}_raw.mp4", portrait, h.get("caption", ""))
        if h.get("voiceover"):
            from ..providers.voice import speak   # voz mexicana (edge) como el resto de los videos
            vo, _ = speak(h["voiceover"], folder / f"hl_{k:02}.mp3", cfg.production.voice_gender, cfg.production.voice_rate)
            clip = ffmpeg.mix_voiceover(raw, vo, folder / f"hl_{k:02}.mp4")
        else:
            clip = raw
        clips.append(clip)

    out = ffmpeg.concat(clips, folder / "final.mp4")
    job.video_path = str(out)
    job.status = "produced"
    save_job(job)
    log.info("[%s] editado %s (%d highlights)", cfg.id, out, len(clips))
    return job
