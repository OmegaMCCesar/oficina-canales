"""PRODUCTOR para canales generados (LCMC/Lumi).

Una sola locución por video (ahorra cuota de voz de Gemini): se narra el guion completo,
y cada escena dura en proporción a sus palabras. Imagen por escena + subtítulo + zoom suave.
"""
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
    scenes = job.script.scenes

    full_text = " ".join(sc["text"].strip() for sc in scenes)
    voice = tts.synthesize(full_text, folder / "voz.mp3", cfg.production.voice, cfg.production.language)
    total = tts.duration(voice) or tts.estimate_seconds(full_text)

    words = [max(1, len(sc["text"].split())) for sc in scenes]
    per_word = total / sum(words)

    clips: list[Path] = []
    for k, sc in enumerate(scenes):
        img = generate_image(f"{sc['visual_prompt']}. {cfg.production.image_style}", folder / f"scene_{k:02}.png", aspect)
        secs = words[k] * per_word + (0.4 if k == len(scenes) - 1 else 0)
        clips.append(ffmpeg.scene_still(img, secs, folder / f"clip_{k:02}.mp4", sc["text"], portrait))

    silent = ffmpeg.concat(clips, folder / "video_mudo.mp4")
    out = ffmpeg.mux_audio(silent, voice, folder / "final.mp4")
    job.video_path = str(out)
    job.status = "produced"
    save_job(job)
    log.info("[%s] producido %s (%.0fs, %d escenas, 1 locución)", cfg.id, out, total, len(clips))
    return job
