"""AGENTE ANIMADOR: se dedica solo a la animación del video.

1. Recibe el guion ya afinado por Retención.
2. Planea el storyboard COMPLETO con reglas de coherencia:
   - Un solo equipo protagonista por video (refrigerador, lavadora, minisplit...) y plantillas que le correspondan.
   - Progresión lógica: síntoma -> causa -> medición -> solución; el componente resaltado no salta sin razón.
   - Valores coherentes entre escenas (si el capacitor mide 0 µF en una, no aparece "ok" después sin explicar).
   - Lumi: expresión acorde a la frase; no repetir la misma expresión más de 3 escenas seguidas.
   - Ritmo visual: no repetir exactamente la misma plantilla+parámetros en escenas consecutivas.
3. Genera la hoja de storyboard (imagen) para que César y el Director la vean.
4. Si el Director pide cambios, revisa una vez con sus notas.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel

from ..config import MEDIA_DIR, ChannelConfig
from ..db import save_job, store
from ..llm import ask_json
from ..models import VideoJob
from ..tools import anim, doodle
from .base import channel_brief, lessons_block

log = logging.getLogger("animator")


class Shot(BaseModel):
    lumi: str = "feliz"        # expresión de Lumi o "" (sin Lumi)
    lumi_pos: str = "derecha"  # derecha | izquierda | centro | ninguno
    prop: str = ""             # objeto de la biblioteca o ""
    estado: str = "normal"     # normal | falla | hielo | goteo | caliente | frio | abierto | inflado | quemada | desconectado
    overlay: str = "ninguno"   # x | check | chispas | pregunta | alerta | burbuja | flecha | humo | ninguno
    burbuja: str = ""          # objeto dentro de la burbuja de pensamiento
    texto: str = ""            # letrero de 1-3 palabras (opcional)
    valor: str = ""            # lectura del multímetro / termómetro


class AScene(BaseModel):
    shots: list[Shot]          # 1-3 tomas (cambio de imagen cada 2-4 s)
    why: str = ""


class AnimPlan(BaseModel):
    appliance: str             # refrigerador | lavadora | minisplit | secadora | general
    continuity: str            # reglas de continuidad que vas a respetar en este video
    scenes: list[AScene]       # UNA por escena del guion, mismo orden


SYSTEM = f"""Eres el ANIMADOR de un canal técnico de línea blanca. Dibujas el video en estilo caricatura 2D
(fondo blanco, contorno negro grueso, colores planos), como los canales explicativos con un personaje
protagonista: imágenes casi fijas que cambian cada 2-4 segundos y un personaje MUY expresivo.
PROTAGONISTA: Lumi (expresiones: {", ".join(doodle.EXPRESSIONS)}). Sale en casi todas las tomas y su cara
cuenta la emoción de la frase: preocupado ante una falla, sorprendido ante un dato, pensando al diagnosticar,
explicando (señala el objeto) al enseñar, feliz/celebrando al resolver, enojado ante un mal técnico, triste por un gasto.
OBJETOS (prop): {", ".join(doodle.PROPS)}.
ESTADOS: normal, falla, hielo, goteo, caliente, frio, abierto, inflado (capacitor), quemada (tarjeta), desconectado (enchufe).
RECURSOS (overlay): x (está mal / no hagas esto), check (bien hecho), chispas (falla eléctrica), pregunta (duda),
alerta (peligro), burbuja (Lumi piensa en `burbuja`), flecha (señala la pieza), humo, ninguno.
REGLAS DE COHERENCIA:
- UN equipo protagonista (appliance) y solo objetos coherentes con él:
  {json.dumps({k: sorted(v) for k, v in doodle.APPLIANCE_PROPS.items()}, ensure_ascii=False)}
- Cada toma ilustra EXACTAMENTE lo que dice esa parte de la frase. Progresión: síntoma -> causa -> medición -> solución.
- Estados y valores coherentes con lo narrado (si el capacitor está mal, se ve inflado y el multímetro marca bajo).
- Escenas cortas (≤ 12 palabras): 1 toma. Escenas largas: 2-3 tomas que avanzan la idea.
- No repitas la misma toma seguida; varía expresión, objeto o recurso.
- texto solo cuando ayuda (1-3 palabras, como "NO ENFRÍA", "0 µF", "DESCONECTA").
- Advertencias de seguridad: Lumi preocupado + alerta (+ enchufe desconectado si es corriente).
Devuelve exactamente una entrada en scenes por cada escena del guion, en el mismo orden."""


KEYWORDS = {
    "minisplit": ("minisplit", "mini split", "aire acondicionado", "clima", "inverter"),
    "lavadora": ("lavadora", "centrifug", "lavasecadora"),
    "secadora": ("secadora",),
    "refrigerador": ("refri", "refrigerador", "nevera", "congelador", "freezer", "frigo", "no frost"),
}
VALID_POS = {"derecha", "izquierda", "centro", "ninguno"}


def infer_appliance(text: str) -> str:
    t = text.lower()
    for k, words in KEYWORDS.items():
        if any(w in t for w in words):
            return k
    return "general"


def _allowed(appliance: str) -> set:
    return doodle.APPLIANCE_PROPS.get(appliance) or set(doodle.PROPS)


def _key(sh: Shot) -> tuple:
    return (sh.lumi, sh.prop, sh.estado, sh.overlay, sh.texto)


def _validate(plan: AnimPlan, n: int, title: str = "") -> list[str]:
    """Reglas de coherencia verificables en código; devuelve problemas encontrados."""
    issues = []
    guess = infer_appliance(title)
    if guess != "general" and plan.appliance != guess:
        issues.append(f"El título habla de {guess} y el plan dice {plan.appliance}.")
        plan.appliance = guess
    allowed = _allowed(plan.appliance)
    if len(plan.scenes) != n:
        issues.append(f"El plan tiene {len(plan.scenes)} escenas y el guion {n}.")
    prev = None
    for i, sc in enumerate(plan.scenes):
        if not sc.shots:
            issues.append(f"Escena {i+1}: sin tomas.")
            continue
        for sh in sc.shots:
            if sh.prop and sh.prop not in doodle.PROPS:
                issues.append(f"Escena {i+1}: objeto inexistente '{sh.prop}'.")
            elif sh.prop and sh.prop not in allowed:
                issues.append(f"Escena {i+1}: '{sh.prop}' no corresponde a un {plan.appliance}.")
            if sh.lumi and doodle.LEGACY.get(sh.lumi, sh.lumi) not in doodle.EXPRESSIONS:
                issues.append(f"Escena {i+1}: expresión '{sh.lumi}' no existe.")
            if sh.overlay not in doodle.OVERLAYS:
                issues.append(f"Escena {i+1}: recurso '{sh.overlay}' no existe.")
            if len(sh.texto.split()) > 3:
                issues.append(f"Escena {i+1}: letrero muy largo ('{sh.texto}').")
            if prev is not None and _key(sh) == prev:
                issues.append(f"Escena {i+1}: repite exactamente la toma anterior.")
            prev = _key(sh)
    return issues


def enforce(plan: AnimPlan, job: VideoJob) -> list[str]:
    """Última red de seguridad: corrige en código lo que siga incoherente tras la revisión."""
    fixed = []
    allowed = _allowed(plan.appliance)
    for i, (a, s) in enumerate(zip(plan.scenes, job.script.scenes)):
        if not a.shots:
            a.shots = [Shot(lumi="explicando", texto=" ".join((s.get("on_screen") or "").split()[:3]))]
            fixed.append(i + 1)
        for sh in a.shots:
            bad = False
            if sh.prop and (sh.prop not in doodle.PROPS or sh.prop not in allowed):
                sh.prop, bad = ("" if plan.appliance == "general" else plan.appliance if plan.appliance in doodle.PROPS else ""), True
            if sh.lumi and doodle.LEGACY.get(sh.lumi, sh.lumi) not in doodle.EXPRESSIONS:
                sh.lumi, bad = "feliz", True
            if sh.overlay not in doodle.OVERLAYS:
                sh.overlay, bad = "ninguno", True
            if sh.lumi_pos not in VALID_POS:
                sh.lumi_pos = "derecha"
            if len(sh.texto.split()) > 3:
                sh.texto, bad = " ".join(sh.texto.split()[:3]), True
            if bad and i + 1 not in fixed:
                fixed.append(i + 1)
    return fixed


def _apply(job: VideoJob, plan: AnimPlan) -> None:
    for sc, a in zip(job.script.scenes, plan.scenes):
        if sc.get("visual") == "photo":
            continue   # material real de César: no se toca
        sc.update({"visual": "doodle", "shots": [sh.model_dump() for sh in a.shots],
                   "lumi": a.shots[0].lumi if a.shots else ""})


def _fallback(job: VideoJob, appliance: str) -> AnimPlan:
    exprs = ["preocupado", "pensando", "explicando", "sorprendido", "feliz"]
    prop = appliance if appliance in doodle.PROPS else ""
    scenes = []
    for k, s in enumerate(job.script.scenes):
        scenes.append(AScene(shots=[Shot(lumi=exprs[k % len(exprs)], prop=prop if k % 3 != 2 else "multimetro",
                                         estado="falla" if k == 0 else "normal", overlay=["x", "ninguno", "flecha", "pregunta", "check"][k % 5],
                                         texto=" ".join((s.get("on_screen") or "").split()[:3]))], why="dry-run"))
    return AnimPlan(appliance=appliance, continuity="dry-run", scenes=scenes)


def plan(cfg: ChannelConfig, job: VideoJob, director_notes: str = "", cesar_notes: str = "") -> tuple[AnimPlan, list[str]]:
    sc = job.script
    appliance = infer_appliance(sc.title + " " + job.idea.title)
    user = f"""{channel_brief(cfg)}
FORMATO: {job.idea.format}
TÍTULO: {sc.title}
EQUIPO PROBABLE: {appliance}
GUION (escena: frase):
{chr(10).join(f"{i+1}. {s['text']}" for i, s in enumerate(sc.scenes))}
{lessons_block(cfg.id)}
{('NOTAS DEL DIRECTOR A CORREGIR: ' + director_notes) if director_notes else ''}
{('NOTAS DE CÉSAR (prioridad máxima): ' + cesar_notes) if cesar_notes else ''}"""
    p = ask_json(SYSTEM, user, AnimPlan, fallback=_fallback(job, appliance))
    while len(p.scenes) < len(sc.scenes):
        p.scenes.append(AScene(shots=[Shot(lumi="explicando")]))
    p.scenes = p.scenes[:len(sc.scenes)]
    return p, _validate(p, len(sc.scenes), sc.title + " " + job.idea.title)


def save_storyboard(cfg: ChannelConfig, job: VideoJob, p: AnimPlan, issues: list[str], verdict: dict | None,
                    revision: int) -> None:
    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    img = anim.storyboard(job.script.scenes, folder / "storyboard.jpg", job.idea.format == "short")
    # Firestore admite ~1 MB por documento: bajar calidad/tamaño hasta que quepa
    from PIL import Image
    data, im = img.read_bytes(), Image.open(img)
    q = 80
    while len(data) * 4 / 3 > 700_000 and q > 20:
        q -= 15
        if q < 50:
            im = im.resize((int(im.width * 0.8), int(im.height * 0.8)))
        im.save(img, quality=q)
        data = img.read_bytes()
    doc = {
        "job_id": job.id, "title": job.script.title, "format": job.idea.format,
        "appliance": p.appliance, "continuity": p.continuity, "revision": revision,
        "issues_code": issues, "verdict": verdict or {},
        "status": (verdict or {}).get("status", "pendiente"),
        "jpeg_b64": base64.b64encode(data).decode(),
        "cesar_notes": job.anim_notes,
        "scenes": [{"text": s["text"], "visual": s.get("visual"), "lumi": s.get("lumi"),
                    "shots": len(s.get("shots") or [])}
                   for s in job.script.scenes],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store().put(cfg.id, "storyboards", job.id, doc)


def run(cfg: ChannelConfig, job: VideoJob, cesar_notes: str = "") -> VideoJob:
    """Plan + revisión del Director (1 corrección) + storyboard guardado. Devuelve el job listo para producir."""
    from . import director
    if not job.script or cfg.production.kind != "generated":
        return job
    p, issues = plan(cfg, job, cesar_notes=cesar_notes)
    _apply(job, p)
    verdict = director.review_animation(cfg, job, p, issues)
    revision = 0
    if not verdict["approve"]:
        revision = 1
        notes = "; ".join(verdict.get("issues", []) + issues)
        p2, issues2 = plan(cfg, job, director_notes=notes, cesar_notes=cesar_notes)
        _apply(job, p2)
        verdict = director.review_animation(cfg, job, p2, issues2)
        verdict["status"] = "aprobado" if verdict["approve"] else "aprobado_con_notas"
        p, issues = p2, issues2
    else:
        verdict["status"] = "aprobado"
    fixed = enforce(p, job)
    if fixed:
        _apply(job, p)
        verdict.setdefault("issues", []).append(f"Corregidas automáticamente las escenas {fixed} (no cuadraban con el {p.appliance}).")
    job.script.retention_notes = (job.script.retention_notes + f" | Animación: {p.continuity}")[:800]
    save_job(job)
    try:
        save_storyboard(cfg, job, p, issues, verdict, revision)
    except Exception as e:
        log.warning("storyboard no guardado: %s", e)
    log.info("[%s] animación %s (rev %d): %s", cfg.id, verdict["status"], revision, p.appliance)
    return job
