"""GUIONISTA.

- Canal generado (LCMC): guion por escenas con prompt visual y texto narrado.
- Canal gameplay: analiza el video de la partida y devuelve highlights con
  timestamps + comentario para locución.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import save_job
from ..llm import ask_json
from ..models import Script, VideoJob
from .base import channel_brief, insights_block

log = logging.getLogger("writer")


class Scene(BaseModel):
    text: str
    visual_prompt: str
    seconds: float = 6


class ScriptOut(BaseModel):
    title: str
    hook: str
    scenes: list[Scene]
    cta: str
    description: str
    tags: list[str]


class Highlight(BaseModel):
    start: float
    end: float
    caption: str          # rótulo en pantalla
    voiceover: str        # lo que se narra encima


class GameplayOut(BaseModel):
    title: str
    hook: str
    highlights: list[Highlight]
    cta: str
    description: str
    tags: list[str]


SYSTEM_GEN = """Eres el GUIONISTA. Escribes guiones narrados para videos explicativos.
Reglas:
- Primera escena = gancho de máximo 2 frases que nombre el problema y prometa la solución.
- Una idea por escena. Frases cortas. Nada de "en este video veremos".
- visual_prompt: describe UNA imagen simple y concreta para esa escena (sin texto en la imagen),
  siguiendo el estilo visual del canal.
- Cierra con CTA de una frase (seguir / comentar su caso).
- Shorts: 6-9 escenas, total < 55 segundos narrados (~130 palabras).
- Largos: 18-30 escenas, 5-7 minutos (~900 palabras).
- Título: ≤ 70 caracteres, con la palabra clave al inicio. Descripción con 3 líneas útiles.
- Si se abre un equipo o se manipula gas, incluye la advertencia de seguridad en el guion."""

SYSTEM_GAME = """Eres el EDITOR-GUIONISTA de un canal de gaming. Recibes una partida grabada.
Identifica los momentos más intensos (kills múltiples, escapes, remontadas, jugadas de Magic Chess
decisivas) con timestamps exactos. Para un short elige 1-3 momentos que sumen < 45 s; para un largo,
8-12 momentos. Cada highlight lleva un rótulo corto para pantalla y una línea de locución que explique
qué pasó y por qué funcionó. Título con héroe y resultado concreto, sin clickbait."""


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    idea = job.idea
    if cfg.production.kind == "gameplay":
        user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
IDEA: {idea.title} — {idea.angle}
FORMATO: {idea.format} (short máx {cfg.production.short_max_seconds}s)
Analiza el video adjunto."""
        fallback = GameplayOut(
            title=idea.title[:70], hook="Mira esto",
            highlights=[Highlight(start=5, end=20, caption="Triple kill", voiceover="Aquí entro por atrás y nadie me ve venir.")],
            cta="Sígueme para más jugadas", description=f"{idea.title}\n#MLBB", tags=["mlbb", "mobile legends"],
        )
        out = ask_json(SYSTEM_GAME, user, GameplayOut, fallback=fallback, video_path=idea.source_media)
        job.script = Script(title=out.title, hook=out.hook, body=" ".join(h.voiceover for h in out.highlights),
                            cta=out.cta, description=out.description, tags=out.tags,
                            highlights=[h.model_dump() for h in out.highlights])
    else:
        user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
ESTILO VISUAL: {cfg.production.image_style}
IDEA: {idea.title} — {idea.angle}
FORMATO: {idea.format}"""
        fallback = ScriptOut(
            title=idea.title[:70], hook=f"¿{idea.title}? Esto es lo primero que revisas.",
            scenes=[Scene(text=f"Escena {k+1} sobre {idea.title}.", visual_prompt=f"Ilustración simple de {idea.title}, escena {k+1}")
                    for k in range(7 if idea.format == "short" else 20)],
            cta="Sígueme y cuéntame tu caso en comentarios.",
            description=f"{idea.title}. Explicado simple por Lumi.", tags=["refrigeracion", "linea blanca"],
        )
        out = ask_json(SYSTEM_GEN, user, ScriptOut, fallback=fallback)
        job.script = Script(title=out.title, hook=out.hook, body=" ".join(s.text for s in out.scenes),
                            cta=out.cta, description=out.description, tags=out.tags,
                            scenes=[s.model_dump() for s in out.scenes])
    job.status = "scripted"
    save_job(job)
    log.info("[%s] guion listo: %s", cfg.id, job.script.title)
    return job
