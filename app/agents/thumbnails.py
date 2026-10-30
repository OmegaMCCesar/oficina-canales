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
    lumi: str = "sorpresa"  # expresión de Lumi: sorpresa|advertencia|diagnostico|solucion|idea|pensando
    anim: str = ""          # plantilla del Estudio Animador como fondo (opcional)
    anim_params_json: str = "{}"


class ThumbOut(BaseModel):
    variants: list[ThumbIdea]
    chosen: int = 0       # índice de la que el Director prefiere


SYSTEM = """Eres el DISEÑADOR DE MINIATURAS. Propón 2 variantes: texto de máximo 4 palabras
(grande, legible en celular) + descripción de una imagen con un solo sujeto claro y contraste alto.
Lumi (la mascota) sale grande a la derecha: elige su expresión (sorpresa, advertencia, diagnostico,
solucion, idea, pensando). Puedes usar de fondo una plantilla del Estudio Animador (anim):
ciclo_refrigeracion, lavadora, minisplit, multimetro, termometro, con anim_params_json.
Luego, actuando como el Director, elige la mejor según la memoria del canal (qué estilo ha dado CTR)."""


def _overlay(img_path: Path, text: str, out: Path, lumi: str | None = None, brand: Path | None = None,
             dim: bool = True) -> Path:
    """Miniatura 1280x720: fondo + texto gigante (lima/blanco) a la izquierda + Lumi grande a la derecha."""
    from ..tools.anim import LIME, NAVY, lumi_path
    img = Image.open(img_path).convert("RGB")
    sc = max(1280 / img.width, 720 / img.height)
    img = img.resize((int(img.width * sc) + 1, int(img.height * sc) + 1))
    l, t = (img.width - 1280) // 2, (img.height - 720) // 2
    img = img.crop((l, t, l + 1280, t + 720))
    if dim:  # oscurece la izquierda para que el texto se lea en el celular
        grad = Image.new("L", (1280, 720))
        gd = ImageDraw.Draw(grad)
        for x in range(1280):
            gd.line([(x, 0), (x, 720)], fill=int(190 * max(0, 1 - x / 900)))
        img.paste(Image.new("RGB", (1280, 720), NAVY), (0, 0), grad)
    d = ImageDraw.Draw(img)
    try:
        fnt = ImageFont.truetype(str(ffmpeg.FONT_PATH), 108)
    except OSError:
        fnt = ImageFont.load_default()
    lines = ffmpeg.wrap_lines(d, text.upper(), fnt, 760)[:3]
    y = 360 - len(lines) * 62
    for i, line in enumerate(lines):
        d.text((48, y), line, font=fnt, fill=LIME if i == 0 else "white", stroke_width=9, stroke_fill="black")
        y += 124
    lp = lumi_path(lumi)
    if lp:
        lu = Image.open(lp).convert("RGBA").resize((470, 470), Image.LANCZOS)
        img.paste(lu, (790, 200), lu)
    if brand and brand.exists():
        b = Image.open(brand).convert("RGBA")
        b.thumbnail((260, 100))
        img.paste(b, (1280 - b.width - 24, 20), b)
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
        ThumbIdea(text=" ".join(job.script.title.split()[:3]), visual_prompt=f"Primer plano de {job.idea.title}",
                  lumi="sorpresa", anim="ciclo_refrigeracion", anim_params_json='{"highlight": "compresor"}'),
        ThumbIdea(text="¿POR QUÉ FALLA?", visual_prompt=f"{job.idea.title}", lumi="advertencia"),
    ], chosen=0)
    out = ask_json(SYSTEM, user, ThumbOut, fallback=fallback)

    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for k, v in enumerate(out.variants[:2]):
        base = folder / f"thumb_base_{k}.jpg"
        is_lcmc = cfg.production.kind == "generated"
        if cfg.production.kind == "gameplay" and job.script.highlights:
            h = job.script.highlights[0]
            ffmpeg.frame_at(Path(job.video_path), max(0.5, (h["end"] - h["start"]) / 2), base)
        elif v.anim:
            import json as _j

            from ..tools import anim as _anim
            try:
                prm = _j.loads(v.anim_params_json or "{}")
            except Exception:
                prm = {}
            clip = folder / f"thumb_anim_{k}.mp4"
            _anim.render(v.anim, prm if isinstance(prm, dict) else {}, 2.2, clip, 1280, 720, None)
            ffmpeg.frame_at(clip, 2.0, base)
            clip.unlink(missing_ok=True)
        else:
            generate_image(f"{v.visual_prompt}. {cfg.production.image_style}", base, aspect="16:9",
                           sources=cfg.production.visual_sources, card_text="")
        final = _overlay(base, v.text, folder / f"thumb_{k}.jpg", lumi=v.lumi if is_lcmc else None)
        paths.append(str(final))
    job.thumbnail_paths = paths
    job.thumbnail_chosen = paths[min(out.chosen, len(paths) - 1)]
    job.status = "thumbnailed"
    save_job(job)
    return job
