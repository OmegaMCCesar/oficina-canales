"""ESTRATEGA DE EMPAQUE: título + miniatura ANTES del guion (como en los equipos que más crecen).

El empaque es la promesa que hace el video en el feed. Se decide primero, el Director lo aprueba,
y después el Guionista escribe para CUMPLIR esa promesa; SEO y Miniaturas la respetan.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import save_job
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief, insights_block

log = logging.getLogger("packaging")


class Concept(BaseModel):
    title: str            # ≤ 60 caracteres, búsqueda real al inicio
    thumb_text: str       # 1-3 palabras gigantes
    thumb_visual: str     # qué se ve: un solo sujeto claro + Lumi con emoción
    promise: str          # qué se lleva quien hace clic, en una frase
    curiosity: str        # el hueco de curiosidad que obliga a ver


class PackOut(BaseModel):
    concepts: list[Concept]


class PackPick(BaseModel):
    chosen: int
    reason: str
    improve: str = ""     # ajuste corto al concepto elegido (opcional)


SYSTEM = """Eres el ESTRATEGA DE EMPAQUE de un canal de YouTube. Antes de que exista guion, diseñas
3 empaques distintos (título + miniatura) para la idea. Reglas de los canales que más crecen:
- El empaque es una PROMESA concreta: un resultado, un error común, un antes/después, un número.
- Título ≤ 60 caracteres, con la búsqueda real al inicio y un hueco de curiosidad honesto al final.
- La miniatura NO repite el título: lo complementa con 1-3 palabras gigantes y UN sujeto claro.
- Nada de clickbait falso: todo lo que promete el empaque debe poder cumplirse en el video.
- Los 3 conceptos deben ser ángulos diferentes (no variaciones de palabras)."""

SYSTEM_PICK = """Eres el DIRECTOR del canal. Elige el empaque con más probabilidad de clic Y de que la gente
se quede (la promesa debe cumplirse). Usa la memoria del canal sobre qué títulos y miniaturas funcionaron."""


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    idea = job.idea
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 10)}
IDEA: {idea.title} — {idea.angle}
FORMATO: {idea.format}
INTENCIÓN DE BÚSQUEDA: {idea.search_intent}"""
    base = idea.title[:60]
    fallback = PackOut(concepts=[
        Concept(title=base, thumb_text="¡NO HAGAS ESTO!", thumb_visual=f"Primer plano de {idea.title}, Lumi con advertencia",
                promise="Evitar el error más común", curiosity="Cuál es el error"),
        Concept(title=f"{base[:40]}: 3 causas", thumb_text="3 CAUSAS", thumb_visual="El equipo con 3 flechas, Lumi pensando",
                promise="Las 3 causas en orden de probabilidad", curiosity="Cuál es la número 1"),
        Concept(title=f"{base[:45]} (fácil)", thumb_text="5 MINUTOS", thumb_visual="Antes y después, Lumi feliz",
                promise="Arreglarlo sin técnico", curiosity="Qué tan fácil es"),
    ])
    try:
        out = ask_json(SYSTEM, user, PackOut, fallback=fallback)
        concepts = out.concepts[:3] or fallback.concepts
        listing = "\n".join(f"{i}. TÍTULO: {c.title} | MINIATURA: {c.thumb_text} — {c.thumb_visual} | PROMESA: {c.promise}"
                            for i, c in enumerate(concepts))
        pick = ask_json(SYSTEM_PICK, f"{channel_brief(cfg)}\n{insights_block(cfg.id, 10)}\nEMPAQUES:\n{listing}",
                        PackPick, fallback=PackPick(chosen=0, reason="dry-run: el de error común suele dar más clic"))
    except Exception as e:
        log.warning("[%s] empaque no disponible (%s); sigo con el título de la idea", cfg.id, e)
        return job
    k = pick.chosen if 0 <= pick.chosen < len(concepts) else 0
    c = concepts[k]
    job.package = {**c.model_dump(), "reason": pick.reason, "improve": pick.improve,
                   "alternatives": [x.title for i, x in enumerate(concepts) if i != k]}
    save_job(job)
    log.info("[%s] empaque elegido: %s / %s", cfg.id, c.title, c.thumb_text)
    return job


def brief(job: VideoJob) -> str:
    """Texto que reciben Guionista, SEO y Miniaturas para respetar la promesa."""
    p = job.package
    if not p:
        return ""
    return (f"EMPAQUE APROBADO POR EL DIRECTOR (cúmplelo): título «{p['title']}», miniatura «{p['thumb_text']}» "
            f"({p['thumb_visual']}). Promesa: {p['promise']}. Curiosidad: {p['curiosity']}."
            + (f" Ajuste del Director: {p['improve']}" if p.get("improve") else ""))
