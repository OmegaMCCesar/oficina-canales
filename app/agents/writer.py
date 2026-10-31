"""GUIONISTA.

- Canal generado (LCMC): guion por escenas con prompt visual y texto narrado.
- Canal gameplay: analiza el video de la partida y devuelve highlights con
  timestamps + comentario para locución.
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import save_job
from ..llm import ask_json
from ..models import Script, VideoJob
from .base import channel_brief, insights_block, lessons_block
from .hero_research import CREDIT
from .packaging import brief

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
- Shorts: 10-16 escenas de una frase, 40-58 segundos narrados (110-150 palabras).
- Largos: la meta de palabras que se indica (8-10 minutos). Estructura: gancho, qué vas a lograr,
  desarrollo por secciones (cómo funciona, síntomas, causas en orden de probabilidad, cómo medir/probar,
  solución paso a paso, errores comunes, cuándo llamar a un técnico), resumen y cierre. Escenas de 1-3 frases.
  Usa ejemplos reales y datos concretos (valores de medición, tiempos, marcas comunes) en vez de relleno.
- Título: ≤ 70 caracteres, con la palabra clave al inicio. Descripción con 3 líneas útiles.
- Si se abre un equipo o se manipula gas, incluye la advertencia de seguridad en el guion."""

SYSTEM_GAME = """Eres el EDITOR-GUIONISTA de un canal de gaming. Recibes una partida grabada.
Identifica los momentos más intensos (kills múltiples, escapes, remontadas, jugadas de Magic Chess
decisivas) con timestamps exactos. Para un short elige 1-3 momentos que sumen < 45 s; para un largo,
8-12 momentos. Cada highlight lleva un rótulo corto para pantalla y una línea de locución que explique
qué pasó y por qué funcionó. Título con héroe y resultado concreto, sin clickbait."""


def _target_words(cfg: ChannelConfig, fmt: str) -> int:
    return 130 if fmt == "short" else int(cfg.production.long_target_minutes * 150)


def _words(out: "ScriptOut") -> int:
    return sum(len(s.text.split()) for s in out.scenes)


class Extra(BaseModel):
    after_scene: int          # número de escena (desde 1) tras la cual se insertan
    scenes: list[Scene]


class ExpandOut(BaseModel):
    additions: list[Extra]


SYSTEM_EXPAND = """Eres el GUIONISTA. El guion quedó corto para un video largo. Agrega escenas NUEVAS con contenido
útil y concreto (no repitas ni rellenes): mediciones, valores típicos, casos reales, errores comunes, pasos detallados,
precauciones. Indica después de qué escena va cada bloque para que el orden siga siendo lógico."""


def _expand_if_short(cfg: ChannelConfig, out: "ScriptOut", target: int, user: str, fallback) -> "ScriptOut":
    """Largos: si el guion no llega al 80 % de la meta, una ronda para ampliarlo (hasta 2)."""
    if target <= 200 or out is fallback:
        return out
    for _ in range(2):
        have = _words(out)
        if have >= target * 0.8:
            break
        listing = "\n".join(f"{i+1}. {s.text}" for i, s in enumerate(out.scenes))
        try:
            ext = ask_json(SYSTEM_EXPAND, f"{user}\nGUION ACTUAL ({have} palabras, faltan ~{target - have}):\n{listing}",
                           ExpandOut, fallback=ExpandOut(additions=[]))
        except Exception as e:
            log.warning("[%s] no pude ampliar el guion (%s)", cfg.id, e)
            break
        if not ext.additions:
            break
        for add in sorted(ext.additions, key=lambda a: -a.after_scene):
            k = max(0, min(len(out.scenes), add.after_scene))
            out.scenes[k:k] = add.scenes
        log.info("[%s] guion ampliado: %d -> %d palabras (meta %d)", cfg.id, have, _words(out), target)
    return out


class HeroScene(BaseModel):
    text: str
    media: str = ""         # "splash:N" (arte oficial N) | "icon:Nombre" (ícono de habilidad) | ""
    card: str = ""          # 1-4 palabras GRANDES en pantalla (nombre de habilidad, dato clave)
    detail: str = ""        # en escenas de habilidad: qué hace, ≤ 12 palabras
    visual_prompt: str = ""


class HeroOut(BaseModel):
    title: str
    hook: str
    hook_text: str          # ≤ 6 palabras en pantalla los primeros segundos
    scenes: list[HeroScene]
    cta: str
    description: str
    tags: list[str]


class HeroMore(BaseModel):
    after_scene: int
    scenes: list[HeroScene]


class HeroExpand(BaseModel):
    additions: list[HeroMore]


SYSTEM_HERO = """Eres el GUIONISTA de un canal de Mobile Legends en español de México. Escribes un video sobre UN héroe
usando SOLO los DATOS VERIFICADOS que te dan (vienen de la wiki en inglés: tradúcelos con naturalidad). No inventes
números, builds, items, fechas, rangos, kills ni datos que no estén en los datos. Si algo no está, no lo digas.
Estructura del LARGO (6-8 min): gancho con lo más intrigante de su historia; su historia contada como relato (con
detalles del lore: lugares, personajes, conflictos); su rol; la pasiva y CADA habilidad explicada con calma (qué hace,
para qué sirve en una pelea, a qué distancia, contra quién conviene); combos que se deducen de las habilidades;
fortalezas y debilidades; cierre preguntando a la audiencia. SHORT: un solo dato de su historia o su habilidad más
vistosa, contado con gancho, 40-55 segundos.
Escenas de 1-2 frases. VISUALES con el ARTE OFICIAL disponible (el video debe verse rico, nunca vacío):
- "splash:N" = arte oficial N: úsalo en la MAYORÍA de escenas (historia, gancho, combos, cierre), alternando N.
  Si quieres un texto grande encima, ponlo en card (1-4 palabras).
- "icon:Nombre" = ícono de esa habilidad: úsalo cuando explicas esa habilidad; card = nombre de la habilidad y
  detail = qué hace en ≤ 12 palabras.
- Nunca dejes una escena sin media si hay arte disponible.
Tono: de jugador a jugador, energético, sin gritos."""


def _hero_media_scene(idea, s: HeroScene, k: int, splash: list, icons: dict) -> dict:
    sc = {"text": s.text, "visual_prompt": s.visual_prompt, "lumi": ""}
    m = s.media.strip()
    if m.startswith("icon") and icons:
        want = m.split(":", 1)[-1].strip().lower()
        url = icons.get(want) or next((u for nm, u in icons.items() if want and (want in nm or nm in want)), None)
        if url:
            sc.update({"visual": "icon", "img_url": url, "on_screen": s.card or m.split(":", 1)[-1].strip(),
                       "detail": s.detail, "bg_url": splash[k % len(splash)] if splash else ""})
            return sc
    if splash:
        try:
            n = int(m.split(":")[1]) if m.startswith("splash") else k
        except Exception:
            n = k
        sc.update({"visual": "web", "img_url": splash[n % len(splash)], "big": s.card.strip()})
        return sc
    if s.card.strip() or not s.visual_prompt.strip():
        sc.update({"visual": "anim", "anim": "titulo", "on_screen": s.card,
                   "anim_params_json": json.dumps({"texto": (s.card or idea.hero).upper()[:28], "sub": idea.hero}, ensure_ascii=False)})
    else:
        sc["visual"] = "image"
    return sc


def _hero_script(cfg: ChannelConfig, job: VideoJob) -> Script:
    idea = job.idea
    target = 130 if idea.format == "short" else min(_target_words(cfg, "long"), 1100)
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 8)}
HÉROE: {idea.hero}
IDEA: {idea.title} — {idea.angle}
{brief(job)}
{lessons_block(cfg.id)}
FORMATO: {idea.format}. META: ~{target} palabras narradas.
ARTE OFICIAL DISPONIBLE: {len(idea.media.get("splash", []))} splash (splash:0 a splash:{max(0, len(idea.media.get("splash", [])) - 1)}){"; íconos: " + ", ".join(i["name"] for i in idea.media.get("icons", [])) if idea.media.get("icons") else "; sin íconos"}
DATOS VERIFICADOS:
{idea.facts}"""
    fb = HeroOut(title=idea.title[:70], hook=f"¿Sabías esto de {idea.hero}?", hook_text=f"¿QUIÉN ES {idea.hero.upper()}?",
                 scenes=[HeroScene(text=f"Dato {k+1} sobre {idea.hero}.", media=f"splash:{k}", card=(f"HABILIDAD {k}" if k % 2 else ""),
                                   visual_prompt="Paisaje de fantasía épica al atardecer con un castillo")
                         for k in range(8 if idea.format == "short" else 20)],
                 cta="Comenta qué héroe quieres que cuente después", description=f"{idea.title}", tags=["mlbb", "mobile legends", idea.hero.lower()])
    out = ask_json(SYSTEM_HERO, user, HeroOut, fallback=fb)
    # largos: si quedó corto, una o dos rondas para ampliar SOLO con los datos verificados
    if idea.format == "long" and out is not fb:
        for _ in range(2):
            have = sum(len(x.text.split()) for x in out.scenes)
            if have >= target * 0.8:
                break
            listing = "\n".join(f"{i+1}. {x.text}" for i, x in enumerate(out.scenes))
            try:
                ext = ask_json(SYSTEM_HERO + "\nAhora AMPLÍA el guion: agrega escenas nuevas (no repitas) con más detalle "
                               "de la historia y de cada habilidad, usando solo los datos verificados.",
                               f"{user}\nGUION ACTUAL ({have} palabras, faltan ~{target - have}):\n{listing}",
                               HeroExpand, fallback=HeroExpand(additions=[]))
            except Exception as e:
                log.warning("[%s] no pude ampliar el guion del héroe (%s)", cfg.id, e)
                break
            if not ext.additions:
                break
            for add in sorted(ext.additions, key=lambda a: -a.after_scene):
                kk = max(0, min(len(out.scenes), add.after_scene))
                out.scenes[kk:kk] = add.scenes
    splash = idea.media.get("splash", [])
    icons = {i["name"].lower(): i["url"] for i in idea.media.get("icons", [])}
    scenes = [_hero_media_scene(idea, s, k, splash, icons) for k, s in enumerate(out.scenes)]
    desc = out.description.strip() + "\n\n" + CREDIT
    return Script(title=out.title, hook=out.hook, hook_text=out.hook_text, body=" ".join(s["text"] for s in scenes),
                  cta=out.cta, cta_end=out.cta, description=desc, tags=out.tags, scenes=scenes)


def _fit_highlights(hl: list, rep: dict, max_total: float) -> list:
    """Cada jugada debe caer dentro de un tramo que el Analista vio de verdad; se recorta a ese tramo."""
    moments = rep.get("moments", [])
    out, total = [], 0.0
    for h in sorted(hl, key=lambda x: x.start):
        m = next((m for m in moments if h.start < m["end"] and h.end > m["start"]), None)
        if not m:
            continue
        h.start, h.end = max(h.start, m["start"] - 3), min(max(h.end, h.start + 4), m["end"] + 3, rep["duration"])
        if h.end - h.start < 3 or (out and h.start < out[-1].end):
            continue
        if total + (h.end - h.start) > max_total:
            break
        out.append(h)
        total += h.end - h.start
    return out


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    idea = job.idea
    if idea.facts:
        job.script = _hero_script(cfg, job)
        if job.package.get("title"):
            job.script.title = job.package["title"][:100]
        job.status = "scripted"
        save_job(job)
        log.info("[%s] guion de héroe listo: %s (%d palabras)", cfg.id, job.script.title, len(job.script.body.split()))
        return job
    if cfg.production.kind == "gameplay":
        from pathlib import Path as _P
        if not idea.source_media or not _P(idea.source_media).exists():
            job.status, job.error = "rejected", "la partida grabada no existe (idea inventada o archivo borrado)"
            save_job(job)
            return job
        from . import scout
        rep = scout.report(cfg.id, idea.source_media)
        seen = rep and rep.get("moments")
        user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
IDEA: {idea.title} — {idea.angle}
{brief(job)}
FORMATO: {idea.format} (short máx {cfg.production.short_max_seconds}s)
""" + (f"""LO QUE EL ANALISTA VIO EN LA PARTIDA (usa SOLO estos tramos; start/end dentro de ellos; no inventes kills,
héroes ni resultado que no estén aquí):
{scout.brief(rep)}""" if seen else "Analiza el video adjunto.")
        fallback = GameplayOut(
            title=idea.title[:70], hook="Mira esto",
            highlights=[Highlight(start=5, end=20, caption="Triple kill", voiceover="Aquí entro por atrás y nadie me ve venir.")],
            cta="Sígueme para más jugadas", description=f"{idea.title}\n#MLBB", tags=["mlbb", "mobile legends"],
        )
        out = ask_json(SYSTEM_GAME, user, GameplayOut, fallback=fallback, video_path=None if seen else idea.source_media)
        if seen:
            out.highlights = _fit_highlights(out.highlights, rep, cfg.production.short_max_seconds if idea.format == "short" else 600)
            if not out.highlights:
                job.status, job.error = "rejected", "ninguna jugada del guion cae en lo que el Analista vio en la partida"
                save_job(job)
                return job
        job.script = Script(title=out.title, hook=out.hook, body=" ".join(h.voiceover for h in out.highlights),
                            cta=out.cta, description=out.description, tags=out.tags,
                            highlights=[h.model_dump() for h in out.highlights])
    else:
        user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
ESTILO VISUAL: {cfg.production.image_style}
IDEA: {idea.title} — {idea.angle}
{brief(job)}
FORMATO: {idea.format}"""
        fallback = ScriptOut(
            title=idea.title[:70], hook=f"¿{idea.title}? Esto es lo primero que revisas.",
            scenes=[Scene(text=f"Escena {k+1} sobre {idea.title}.", visual_prompt=f"Ilustración simple de {idea.title}, escena {k+1}")
                    for k in range(7 if idea.format == "short" else 20)],
            cta="Sígueme y cuéntame tu caso en comentarios.",
            description=f"{idea.title}. Explicado simple por Lumi.", tags=["refrigeracion", "linea blanca"],
        )
        target = _target_words(cfg, idea.format)
        user += f"\nMETA: {target} palabras narradas en total (cuenta tus palabras)."
        out = ask_json(SYSTEM_GEN, user, ScriptOut, fallback=fallback)
        out = _expand_if_short(cfg, out, target, user, fallback)
        job.script = Script(title=out.title, hook=out.hook, body=" ".join(s.text for s in out.scenes),
                            cta=out.cta, description=out.description, tags=out.tags,
                            scenes=[s.model_dump() for s in out.scenes])
    if job.package.get("title"):
        job.script.title = job.package["title"][:100]   # el empaque aprobado manda
    job.status = "scripted"
    save_job(job)
    log.info("[%s] guion listo: %s", cfg.id, job.script.title)
    return job
