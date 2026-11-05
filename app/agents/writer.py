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
    vo_at: float = 0      # segundo (de la partida) en que empieza la narración: cuando empieza la acción que describe


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
ESTRUCTURA DEL LARGO (6-8 min), en este orden y SIN REPETIR NADA (prohibido resumir o recapitular la historia):
1. GANCHO (0-5 s, 2-3 escenas cortísimas): lo más visual primero (animación o arte con texto grande) + la promesa
   ("en este video vas a dominar a <héroe>") + el dato emocional más fuerte de su historia en una frase.
2. HISTORIA BREVE (máx 20% del video), contada para EXPLICAR cómo se juega: conecta el lore con la mecánica
   ("su entrenamiento en el Abismo → por eso su pasiva hace daño verdadero").
3. HABILIDADES: pasiva y cada habilidad (qué hace, cuándo usarla, contra quién), con su ícono.
4. BUILD / OBJETOS / EMBLEMA / HECHIZO: SOLO si vienen en los datos, cada objeto con su ícono (media "item:Nombre
   exacto en inglés") y qué aporta.
5. COMBOS (deducidos de las habilidades) y ERRORES COMUNES (solo los que se deducen de los datos).
6. PREGUNTA A MITAD DEL VIDEO (una escena hacia el 50%): pregunta concreta para comentarios ("¿qué combo usas tú?").
7. CONSEJOS FINALES y cierre con pregunta.
SHORT: un solo dato (historia o habilidad más vistosa) con gancho en el primer segundo, 40-55 s.
Escenas de 1-2 frases. VISUALES (el video debe moverse y verse rico, nunca vacío ni repetido):
- "clip:Nombre" = animación oficial de esa habilidad (si está en la lista): úsala en el gancho y al explicar esa
  habilidad. Es lo más dinámico: priorízala.
- "icon:Nombre" = ícono de la habilidad: card = nombre, detail = qué hace en ≤ 12 palabras.
- "item:Nombre" = ícono de un objeto de la build (solo objetos que estén en los datos): card = nombre, detail = qué da.
- "splash:N" = arte oficial N: para historia y transiciones, alternando N (nunca el mismo dos veces seguidas).
REGLA DE ORO: "text" es SOLO lo que dice el narrador en voz alta. Prohibido escribir notas internas, corchetes,
"verificar en la wiki", "dato pendiente", ni describir lo que se ve ("aparece X disparando"): lo visual va en media/card.
Si un dato no está en los datos verificados, NO lo menciones (no lo marques: omítelo).
Tono: de jugador a jugador, energético, sin gritos. Título del largo: «<Héroe>: guía completa 2026 …» + el gancho."""


def _hero_media_scene(idea, s: HeroScene, k: int, splash: list, icons: dict) -> dict:
    sc = {"text": s.text, "visual_prompt": s.visual_prompt, "lumi": ""}
    m = s.media.strip()
    if m.startswith("clip"):
        want = m.split(":", 1)[-1].strip().lower()
        clips = {c["name"].lower(): c["url"] for c in idea.media.get("clips", [])}
        url = clips.get(want) or next((u for nm, u in clips.items() if want and (want in nm or nm in want)), None)
        if url:
            sc.update({"visual": "clip", "img_url": url, "big": s.card.strip(),
                       "bg_url": splash[k % len(splash)] if splash else ""})
            return sc
        m = "icon:" + m.split(":", 1)[-1]
    if m.startswith("item"):
        name = m.split(":", 1)[-1].strip()
        sc.update({"visual": "item", "item": name, "on_screen": s.card or name, "detail": s.detail,
                   "bg_url": splash[k % len(splash)] if splash else ""})
        return sc
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
ANIMACIONES DE HABILIDADES: {", ".join(c["name"] for c in idea.media.get("clips", [])) or "ninguna"}
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
                ext = ask_json(SYSTEM_HERO + "\nAhora AMPLÍA el guion con secciones que FALTEN (build/objetos, combos, "
                               "errores comunes, consejos, contra quién conviene) o con más detalle de cada habilidad. "
                               "PROHIBIDO volver a contar la historia o repetir algo ya dicho. Usa solo los datos verificados.",
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
    from difflib import SequenceMatcher
    kept: list = []
    for x in out.scenes:   # nada repetido: si una escena dice casi lo mismo que otra anterior, se quita
        if any(SequenceMatcher(None, x.text.lower(), y.text.lower()).ratio() > 0.72 for y in kept):
            continue
        kept.append(x)
    out.scenes = kept
    splash = idea.media.get("splash", [])
    icons = {i["name"].lower(): i["url"] for i in idea.media.get("icons", [])}
    scenes = [_hero_media_scene(idea, s, k, splash, icons) for k, s in enumerate(out.scenes)]
    from .narration_guard import clean_scenes
    scenes, dropped = clean_scenes(scenes)
    if dropped:
        log.info("[%s] guardia de narración: quité %d escena(s) con notas internas", cfg.id, dropped)
    desc = out.description.strip() + "\n\n" + CREDIT
    return Script(title=out.title, hook=out.hook, hook_text=out.hook_text, body=" ".join(s["text"] for s in scenes),
                  cta=out.cta, cta_end=out.cta, description=desc, tags=out.tags, scenes=scenes)


# ---------- guion estilo MÁQUINA LUMI (plantilla de César para LCMC) ----------
class MaqScene(BaseModel):
    tipo: str = "explicacion"   # gancho|explicacion|causa|advertencia|pasos|cierre
    pose: str = "feliz"
    titulo: str                 # texto grande en pantalla (máx 6 palabras)
    puntos: list[str] = []      # hasta 3 viñetas de máx 6 palabras
    narracion: str              # 1-3 frases, máx 45 palabras


class MaqThumb(BaseModel):
    texto: str
    resaltar: str = ""
    subtexto: str = ""
    aparato: str = "otro"
    codigo: str = ""
    pose: str = "sorpresa"


class MaqOut(BaseModel):
    titulo: str
    descripcion: str
    hashtags: list[str] = []
    miniatura: MaqThumb
    escenas: list[MaqScene]
    cta: str = "Sígueme para más soluciones de línea blanca"


SYSTEM_MAQ = """Eres el guionista del canal "LCMC · Servir para Servir". Lo dirige César, técnico con más de 15 años en
línea blanca y refrigeración (excapacitador de Samsung). La mascota es LUMI, una chispa de conocimiento amable que
explica las fallas de forma sencilla, con humor ligero y sin tecnicismos innecesarios.
Estilo: narración con ilustraciones fijas (canales de divulgación animada). Español de México, cercano, frases
cortas, una idea por escena. Nada de relleno.
SEGURIDAD (obligatorio): una escena de advertencia (desconectar antes de abrir); nunca indiques manipular gas,
soldar o recargar gas (solo técnico certificado); si hay alto voltaje (magnetrón, capacitores de inverter) dilo y
recomienda técnico; si no estás seguro de un dato, no lo inventes: describe la verificación, no la cifra.
ESTRUCTURA: gancho (≤ 8 s, plantea el problema) → cómo funciona → cada causa (de la más común a la menos, con su
síntoma y cómo verificarla) → advertencia → pasos que SÍ puede hacer el usuario → cuándo llamar a un técnico →
errores comunes → cierre invitando a seguir el canal. Cada escena: titulo ≤ 6 palabras, hasta 3 puntos de ≤ 6
palabras, narracion 1-3 frases (≤ 45 palabras). Elige la pose de Lumi según la emoción (sorpresa para el gancho,
diagnostico/analizando para causas, advertencia para seguridad, solucion/listo para el arreglo, celebracion al final).
Poses: principal, feliz, analizando, pensando, sorpresa, idea, confirmacion, advertencia, listo, aprendizaje,
diagnostico, solucion, celebracion."""


def _maquina_script(cfg: ChannelConfig, job: VideoJob) -> Script:
    idea = job.idea
    long_ = idea.format == "long"
    n_esc, words = ((24, 32), 1150) if long_ else ((5, 7), 120)
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 10)}
TEMA: {idea.title} — {idea.angle}
{brief(job)}
FORMATO: {"LARGO para YouTube 16:9" if long_ else "CORTO (Shorts/TikTok 9:16, 40-55 s)"}.
EXTENSIÓN: entre {n_esc[0]} y {n_esc[1]} escenas y ~{words} palabras narradas en total ({"unos 7-9 minutos: profundiza en cada causa, cómo verificarla, qué piezas intervienen y errores comunes" if long_ else "una sola idea fuerte"}).
miniatura: texto (2-4 palabras ENORMES con la falla como la busca la gente), resaltar (palabra clave), subtexto
(aparato y marca), aparato (lavadora|secadora|refrigerador|minisplit|microondas|otro), codigo (si hay), pose."""
    fb = MaqOut(titulo=idea.title[:70], descripcion=idea.title, miniatura=MaqThumb(texto=" ".join(idea.title.split()[:3])),
                escenas=[MaqScene(tipo="gancho" if k == 0 else "explicacion", pose="sorpresa" if k == 0 else "feliz",
                                  titulo=idea.title[:30], narracion=f"Escena {k+1} sobre {idea.title}.") for k in range(n_esc[0])])
    out = ask_json(SYSTEM_MAQ, user, MaqOut, fallback=fb, smart=long_)
    if long_ and out is not fb:   # si quedó corto, una ronda para AGREGAR secciones (sin repetir)
        have = sum(len(e.narracion.split()) for e in out.escenas)
        if have < words * 0.75:
            listing = "\n".join(f"{i+1}. [{e.tipo}] {e.titulo}: {e.narracion}" for i, e in enumerate(out.escenas))
            try:
                more = ask_json(SYSTEM_MAQ + "\nDevuelve SOLO escenas NUEVAS para insertar antes del cierre: más detalle de "
                                "cada causa (síntoma, cómo verificar, piezas), errores comunes, cuándo llamar al técnico. "
                                "Prohibido repetir lo ya dicho.", f"{user}\nGUION ACTUAL ({have} palabras, faltan ~{words - have}):\n{listing}",
                                MaqOut, fallback=MaqOut(titulo="", descripcion="", miniatura=out.miniatura, escenas=[]))
                if more.escenas:
                    out.escenas[-1:-1] = more.escenas
            except Exception as e:
                log.warning("[%s] no pude ampliar el guion (%s)", cfg.id, e)
    from difflib import SequenceMatcher
    kept: list = []
    for e in out.escenas:
        if not any(SequenceMatcher(None, e.narracion.lower(), x.narracion.lower()).ratio() > 0.72 for x in kept):
            kept.append(e)
    scenes = [{"text": e.narracion, "tipo": e.tipo, "pose": e.pose, "titulo": e.titulo, "puntos": e.puntos[:3],
               "visual": "maquina", "visual_prompt": ""} for e in kept]
    from .narration_guard import clean_scenes
    scenes, _ = clean_scenes(scenes)
    desc = out.descripcion.strip() + ("\n\n" + " ".join(out.hashtags) if out.hashtags else "")
    return Script(title=out.titulo, hook=kept[0].narracion if kept else "", body=" ".join(s["text"] for s in scenes),
                  cta=out.cta, cta_end=out.cta, description=desc, tags=[h.strip("#") for h in out.hashtags][:15],
                  hashtags=out.hashtags[:5], scenes=scenes, extra={"miniatura": out.miniatura.model_dump()})


def _fit_highlights(hl: list, rep: dict, max_total: float) -> list:
    """Cada jugada debe caer dentro de un tramo que el Analista vio de verdad; se recorta a ese tramo."""
    from . import scout as _sc
    moments = _sc.moments_of(rep)
    out, total = [], 0.0
    for h in sorted(hl, key=lambda x: x.start):
        m = next((m for m in moments if h.start < m["end"] and h.end > m["start"]), None)
        if not m:
            continue
        h.start, h.end = max(h.start, m["start"] - 3), min(max(h.end, h.start + 4), m["end"] + 3, rep["duration"])
        ba = m.get("banner_at")
        if ba and not (h.start <= ba <= h.end):           # el anuncio de la kill SIEMPRE dentro del clip
            h.start, h.end = min(h.start, ba - 4), max(h.end, ba + 2)
        if m.get("refined"):                               # con jugada precisa, el tramo es el que se vio
            h.start, h.end = max(h.start, m["start"]), min(h.end, m["end"]) if not ba or ba + 2 <= m["end"] else h.end
        if not (h.start <= h.vo_at < h.end):
            h.vo_at = (m["beats"][0]["t"] if m.get("beats") else h.start)
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
        seen = rep and scout.moments_of(rep)
        user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
IDEA: {idea.title} — {idea.angle}
{brief(job)}
FORMATO: {idea.format} (short máx {cfg.production.short_max_seconds}s)
""" + ("""ES UN VIDEO LARGO DE PARTIDA COMPLETA: el editor pone la partida entera (acelera lo tranquilo y quita cargas y
tienda). Tú escribes: hook (1-2 frases para el cold open con la mejor jugada, sin spoilear el resultado), 6-12
highlights = COMENTARIOS en las jugadas (start/end dentro de los tramos vistos; voiceover de máx 25 palabras: qué
pasó y por qué funcionó, como comentarista de jugador a jugador), y cta de cierre (suscribirse y pedir el próximo
héroe). Título: «Partida completa con <héroe>…» + lo mejor que pasó; nada que no esté en lo visto.
""" if idea.format == "long" else "") + (f"""LO QUE EL ANALISTA VIO EN LA PARTIDA (usa SOLO estos tramos; start/end dentro de ellos; no inventes kills,
héroes ni resultado que no estén aquí):
{scout.brief(rep)}
SINCRONÍA (lo más importante): usa las jugadas [PRECISO] y sus beats segundo a segundo. Cada highlight debe
incluir el anuncio de racha (si lo hay) entre start y end. La voz narra SOLO lo que se ve en esos beats, en el mismo
orden, y vo_at = segundo del primer beat que narras. Máximo 2.3 palabras por segundo del tramo (voz corta: deja
oír el juego). Si una jugada es [aproximado], no la uses salvo que no haya otra.""" if seen else "Analiza el video adjunto.")
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
    elif cfg.production.style == "lumi":
        job.script = _maquina_script(cfg, job)
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
