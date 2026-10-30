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
from ..tools import anim
from .base import channel_brief

log = logging.getLogger("animator")


class AScene(BaseModel):
    visual: str = "anim"          # anim | studio | image
    anim: str = ""
    anim_params_json: str = "{}"
    studio_clip: str = ""
    lumi: str = ""
    visual_prompt: str = ""
    why: str = ""                 # por qué esta animación en esta escena


class AnimPlan(BaseModel):
    appliance: str                # refrigerador | lavadora | minisplit | secadora | general
    continuity: str               # reglas de continuidad que vas a respetar en este video
    scenes: list[AScene]          # UNA por escena del guion, mismo orden


SYSTEM = f"""Eres el ANIMADOR de un canal técnico de línea blanca. Solo te ocupas de la animación.
Diseñas el storyboard de TODO el video con coherencia visual y técnica:
- Elige UN equipo protagonista (appliance) y usa solo plantillas coherentes con él:
  {json.dumps({k: sorted(v) for k, v in anim.APPLIANCE_TEMPLATES.items()}, ensure_ascii=False)}
- Progresión lógica: síntoma -> causa -> medición -> solución. El componente resaltado en
  ciclo_refrigeracion solo cambia cuando el guion cambia de componente.
- Valores coherentes: lo que mide el multímetro o marca el termómetro debe cuadrar con lo que se dice.
- No repitas plantilla+parámetros idénticos en escenas seguidas; alterna para mantener ritmo.
- Lumi (expresiones: {', '.join(anim.LUMI_EXPRESSIONS)}): acorde a la frase; máximo 3 escenas seguidas
  con la misma; vacío en ~1 de cada 4 escenas.
- visual=studio solo si el clip existe en el catálogo; visual=image solo si ninguna plantilla sirve.
Plantillas y parámetros:
  ciclo_refrigeracion {{"highlight":"compresor|condensador|capilar|evaporador","falla":"texto corto"}}
  lavadora {{"modo":"lavado|centrifugado|desagüe|atorada"}}
  minisplit {{"goteo":true|false,"flujo":"frio|caliente"}}
  multimetro {{"valor":"12.4","unidad":"Ω|V|A|µF","estado":"ok|falla"}}
  termometro {{"desde":4,"hasta":16,"unidad":"°C"}}
  lista {{"titulo":"3 causas","items":["...","..."]}}
  titulo {{"texto":"frase corta","sub":"..."}}
Devuelve exactamente una entrada en scenes por cada escena del guion, en el mismo orden."""


KEYWORDS = {
    "minisplit": ("minisplit", "mini split", "aire acondicionado", "clima", "inverter"),
    "lavadora": ("lavadora", "centrifug", "lavasecadora"),
    "secadora": ("secadora",),
    "refrigerador": ("refri", "refrigerador", "nevera", "congelador", "freezer", "frigo", "no frost"),
}


def infer_appliance(text: str) -> str:
    t = text.lower()
    for k, words in KEYWORDS.items():
        if any(w in t for w in words):
            return k
    return "general"


def _empty_params(name: str, prm: dict) -> bool:
    if name == "lista":
        return not [i for i in prm.get("items", []) if str(i).strip()]
    if name == "titulo":
        return not str(prm.get("texto", "")).strip()
    return False


def _validate(plan: AnimPlan, n: int, title: str = "") -> list[str]:
    """Reglas de coherencia verificables en código; devuelve problemas encontrados."""
    issues = []
    guess = infer_appliance(title)
    if guess != "general" and plan.appliance != guess:
        issues.append(f"El título habla de {guess} y el plan dice {plan.appliance}.")
        plan.appliance = guess
    allowed = anim.APPLIANCE_TEMPLATES.get(plan.appliance, anim.APPLIANCE_TEMPLATES["general"])
    if len(plan.scenes) != n:
        issues.append(f"El plan tiene {len(plan.scenes)} escenas y el guion {n}.")
    prev, same_lumi = None, 0
    for i, sc in enumerate(plan.scenes):
        if sc.visual == "anim":
            if sc.anim not in anim.TEMPLATES:
                issues.append(f"Escena {i+1}: plantilla inexistente '{sc.anim}'.")
            elif sc.anim not in allowed:
                issues.append(f"Escena {i+1}: '{sc.anim}' no corresponde a un {plan.appliance}.")
            try:
                prm = json.loads(sc.anim_params_json or "{}")
                if _empty_params(sc.anim, prm if isinstance(prm, dict) else {}):
                    issues.append(f"Escena {i+1}: '{sc.anim}' quedaría vacía (faltan textos).")
            except Exception:
                issues.append(f"Escena {i+1}: parámetros no son JSON válido.")
            key = (sc.anim, sc.anim_params_json)
            if key == prev:
                issues.append(f"Escena {i+1}: repite exactamente la animación de la escena {i}.")
            prev = key
        if i and sc.lumi and sc.lumi == plan.scenes[i - 1].lumi:
            same_lumi += 1
            if same_lumi >= 3:
                issues.append(f"Escena {i+1}: Lumi repite '{sc.lumi}' demasiadas veces seguidas.")
        else:
            same_lumi = 0
    return issues


def enforce(plan: AnimPlan, job: VideoJob) -> list[str]:
    """Última red de seguridad: corrige en código lo que siga incoherente tras la revisión."""
    fixed = []
    allowed = anim.APPLIANCE_TEMPLATES.get(plan.appliance, anim.APPLIANCE_TEMPLATES["general"])
    for i, (a, s) in enumerate(zip(plan.scenes, job.script.scenes)):
        if a.visual != "anim":
            continue
        try:
            prm = json.loads(a.anim_params_json or "{}")
            prm = prm if isinstance(prm, dict) else {}
        except Exception:
            prm = {}
        if a.anim not in anim.TEMPLATES or a.anim not in allowed or _empty_params(a.anim, prm):
            words = (s.get("on_screen") or s["text"]).rstrip(".!?")
            a.anim = "titulo"
            a.anim_params_json = json.dumps({"texto": " ".join(words.split()[:6])}, ensure_ascii=False)
            fixed.append(i + 1)
    return fixed


def _apply(job: VideoJob, plan: AnimPlan) -> None:
    for sc, a in zip(job.script.scenes, plan.scenes):
        sc.update({"visual": a.visual, "anim": a.anim, "anim_params_json": a.anim_params_json,
                   "studio_clip": a.studio_clip, "lumi": a.lumi,
                   "visual_prompt": a.visual_prompt or sc.get("visual_prompt", "")})


def plan(cfg: ChannelConfig, job: VideoJob, director_notes: str = "", cesar_notes: str = "") -> tuple[AnimPlan, list[str]]:
    from .retention import studio_catalog
    sc = job.script
    user = f"""{channel_brief(cfg)}
FORMATO: {job.idea.format}
TÍTULO: {sc.title}
CATÁLOGO DEL STUDIO QUINTO SOL: {studio_catalog() or '(vacío)'}
GUION (escena: frase | propuesta actual):
{chr(10).join(f"{i+1}. {s['text']} | {s.get('visual','')}:{s.get('anim','')} {s.get('anim_params_json','')} lumi={s.get('lumi','')}" for i, s in enumerate(sc.scenes))}
{('NOTAS DEL DIRECTOR A CORREGIR: ' + director_notes) if director_notes else ''}
{('NOTAS DE CÉSAR (prioridad máxima): ' + cesar_notes) if cesar_notes else ''}"""
    fallback = AnimPlan(appliance="general", continuity="dry-run", scenes=[
        AScene(visual=s.get("visual", "anim"), anim=s.get("anim") or "titulo",
               anim_params_json=s.get("anim_params_json") or json.dumps({"texto": s["text"][:40]}, ensure_ascii=False),
               lumi=s.get("lumi", ""), why="dry-run") for s in sc.scenes])
    p = ask_json(SYSTEM, user, AnimPlan, fallback=fallback)
    # completar o recortar para que coincida con el guion
    while len(p.scenes) < len(sc.scenes):
        s = sc.scenes[len(p.scenes)]
        p.scenes.append(AScene(anim="titulo", anim_params_json=json.dumps({"texto": s["text"][:40]}, ensure_ascii=False)))
    p.scenes = p.scenes[:len(sc.scenes)]
    return p, _validate(p, len(sc.scenes), sc.title + ' ' + job.idea.title)


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
        "scenes": [{"text": s["text"], "visual": s.get("visual"), "anim": s.get("anim"), "lumi": s.get("lumi")}
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
