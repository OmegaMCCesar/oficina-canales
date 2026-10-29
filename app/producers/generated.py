"""PRODUCTOR para canales generados (LCMC/Lumi): imagen por escena + TTS + ffmpeg."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import MEDIA_DIR, ChannelConfig
from ..db import save_job
from ..llm import generate_image
from ..models import VideoJob
from ..tools import ffmpeg, tts

log = logging.getLogger("producer.generated")


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    assert job.script and job.script.scenes
    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    portrait = job.idea.format == "short"
    aspect = "9:16" if portrait else "16:9"

    clips: list[Path] = []
    total = 0.0
    for k, sc in enumerate(job.script.scenes):
        img = generate_image(f"{sc['visual_prompt']}. {cfg.production.image_style}", folder / f"scene_{k:02}.png", aspect)
        audio = tts.synthesize(sc["text"], folder / f"scene_{k:02}.mp3", cfg.production.voice, cfg.production.language)
        clip = ffmpeg.scene_clip(img, audio, folder / f"clip_{k:02}.mp4", sc["text"], portrait)
        clips.append(clip)
        total += tts.duration(audio)
        if portrait and total > cfg.production.short_max_seconds:
            log.warning("[%s] short excede %ss, se corta en escena %d", cfg.id, cfg.production.short_max_seconds, k)
            break

    out = ffmpeg.concat(clips, folder / "final.mp4")
    job.video_path = str(out)
    job.status = "produced"
    save_job(job)
    log.info("[%s] producido %s (%.0fs, %d escenas)", cfg.id, out, total, len(clips))
    return job
