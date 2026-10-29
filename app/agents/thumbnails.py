"""DISEÑADOR DE MINIATURAS: genera 2 variantes y el Director elige una.

- generated: imagen con Imagen + texto grande superpuesto (PIL).
- gameplay: frame del mejor highlight + texto.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from ..config import MEDIA_DIR, ChannelConfig
from ..db import save_job
from ..llm import ask_json, generate_image
from ..models import VideoJob
from ..tools import ffmpeg
from .base import channel_brief, insights_block

log = logging.getLogger("thumbs")


class ThumbIdea(BaseModel):
    text: str             # máx 4 palabras, en mayúsculas
    visual_prompt: str


class ThumbOut(BaseModel):
    variants: list[ThumbIdea]
    chosen: int = 0       # índice de la que el Director prefiere


SYSTEM = """Eres el DISEÑADOR DE MINIATURAS. Propón 2 variantes: texto de máximo 4 palabras
(grande, legible en celular) + descripción de una imagen con un solo sujeto claro y contraste alto.
Luego, actuando como el Director, elige la mejor según la memoria del canal (qué estilo ha dado CTR)."""


def _overlay(img_path: Path, text: str, out: Path) -> Path:
    img = Image.open(img_path).convert("RGB")
    if img.width < img.height:                       # miniaturas siempre 16:9
        img = img.resize((1280, int(1280 * img.height / img.width)))
        top = (img.height - 720) // 2
        img = img.crop((0, top, 1280, top + 720))
    else:
        img = img.resize((1280, 720))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
    except OSError:
        font = ImageFont.load_default()
    words, lines, cur = text.upper().split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) > 1180:
            lines.append(cur); cur = w
        else:
            cur = t
    lines.append(cur)
    y = 720 - 40 - len(lines) * 120
    for line in lines:
        x = (1280 - d.textlength(line, font=font)) / 2
        d.text((x, y), line, font=font, fill="white", stroke_width=8, stroke_fill="black")
        y += 120
    img.save(out, quality=92)
    return out


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    assert job.script and job.video_path
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 10)}
VIDEO: {job.script.title}
GANCHO: {job.script.hook}
ESTILO VISUAL: {cfg.production.image_style}"""
    fallback = ThumbOut(variants=[
        ThumbIdea(text=" ".join(job.script.title.split()[:3]), visual_prompt=f"Primer plano de {job.idea.title}"),
        ThumbIdea(text="¿POR QUÉ FALLA?", visual_prompt=f"{job.idea.title}, expresión de sorpresa"),
    ], chosen=0)
    out = ask_json(SYSTEM, user, ThumbOut, fallback=fallback)

    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for k, v in enumerate(out.variants[:2]):
        base = folder / f"thumb_base_{k}.jpg"
        if cfg.production.kind == "gameplay" and job.script.highlights:
            h = job.script.highlights[0]
            ffmpeg.frame_at(Path(job.video_path), max(0.5, (h["end"] - h["start"]) / 2), base)
        else:
            generate_image(f"{v.visual_prompt}. {cfg.production.image_style}", base, aspect="16:9")
        final = _overlay(base, v.text, folder / f"thumb_{k}.jpg")
        paths.append(str(final))
    job.thumbnail_paths = paths
    job.thumbnail_chosen = paths[min(out.chosen, len(paths) - 1)]
    job.status = "thumbnailed"
    save_job(job)
    return job
