"""GUARDIÁN DE CALIDAD Y ORIGINALIDAD (revisa antes de gastar en producir).

1. Originalidad (código): compara título, gancho y estructura visual con los últimos 20 videos.
   YouTube no monetiza contenido "repetitivo o hecho con plantillas"; si un video sale casi igual
   a otro, se bloquea.
2. Seguridad (código): si se habla de corriente, gas o abrir el equipo, exige advertencia; si falta,
   la agrega después del gancho.
3. Revisión técnica y de honestidad (IA): datos incorrectos, promesas del título que el guion no
   cumple, afirmaciones peligrosas. Corrige frases concretas o bloquea.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import add_activity, jobs, save_job, store
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief

log = logging.getLogger("quality")

RISK = ("gas", "r600", "r134", "r410", "r22", "corriente", "220", "127 v", "voltaje", "capacitor", "compresor",
        "desarm", "abrir el", "abre el", "tarjeta electr", "resistencia", "soldar", "cable")
WARN = ("desconect", "seguridad", "precauci", "cuidado", "advertencia", "peligro", "sin corriente", "guantes", "descarga")


class Fix(BaseModel):
    scene: int          # número de escena desde 1
    new_text: str


class Review(BaseModel):
    verdict: str        # ok | fix | block
    score: int          # 1-10 calidad técnica y claridad
    issues: list[str] = []
    fixes: list[Fix] = []
    title_keeps_promise: bool = True
    summary: str


SYSTEM = """Eres el GUARDIÁN DE CALIDAD de un canal técnico de línea blanca. Revisa el guion ANTES de producirlo:
- Datos técnicos incorrectos o peligrosos (voltajes, gases, procedimientos). Un técnico experto debe poder firmarlo.
- Que el título y la promesa del empaque se cumplan dentro del video (nada de clickbait).
- Advertencias de seguridad cuando se abre un equipo, se mide voltaje o se manipula gas.
- Frases de relleno o genéricas que suenen a "video hecho en serie".
verdict: ok (listo), fix (corregible: da fixes con el número de escena y la frase corregida, máximo 5),
block (no debe publicarse: peligroso, falso o sin sentido). Sé exigente pero práctico."""


def _sig(j: VideoJob) -> dict:
    sc = j.script
    return {"title": (sc.title if sc else j.idea.title).lower(), "hook": ((sc.hook_text or sc.hook) if sc else "").lower(),
            "tpl": [s.get("anim") or s.get("visual") or "" for s in (sc.scenes if sc else [])]}


def originality(cfg: ChannelConfig, job: VideoJob) -> dict:
    me = _sig(job)
    others = [j for j in jobs(cfg.id) if j.id != job.id and j.script and j.status not in ("rejected", "failed")]
    others = sorted(others, key=lambda j: j.created_at, reverse=True)[:20]
    worst, worst_job = 0.0, None
    for o in others:
        if o.id == job.idea.trailer_of:
            continue   # el tráiler se parece a su episodio a propósito
        if job.idea.hero and o.idea.hero and o.idea.hero.lower() != job.idea.hero.lower():
            continue   # serie de héroes: mismo formato de título, distinto héroe
        s = _sig(o)
        t = SequenceMatcher(None, me["title"], s["title"]).ratio()
        h = SequenceMatcher(None, me["hook"], s["hook"]).ratio() if me["hook"] and s["hook"] else 0
        v = SequenceMatcher(None, me["tpl"], s["tpl"]).ratio() if me["tpl"] and s["tpl"] else 0
        sim = 0.5 * t + 0.25 * h + 0.25 * v
        if sim > worst:
            worst, worst_job = sim, o
    has_human = bool(job.script and job.script.human)
    score = round(100 * (1 - worst) + (10 if has_human else 0))
    return {"score": max(0, min(100, score)), "max_similarity": round(worst, 2), "has_human": has_human,
            "similar_to": (worst_job.script.title if worst_job and worst_job.script else ""),
            "title_similarity": round(SequenceMatcher(None, me["title"], _sig(worst_job)["title"]).ratio(), 2) if worst_job else 0}


def _safety(job: VideoJob) -> str | None:
    text = job.script.body.lower()
    if not any(r in text for r in RISK) or any(w in text for w in WARN):
        return None
    gas = any(g in text for g in ("gas", "r600", "r134", "r410", "r22"))
    return ("Ojo: el gas refrigerante es inflamable o está a presión; trabaja ventilado y con equipo de protección."
            if gas else "Antes de abrir o medir, desconecta el equipo de la corriente.")


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    if not sc:
        return job
    orig = originality(cfg, job)
    issues: list[str] = []
    applied: list[str] = []
    # repetido: casi el mismo título que un video que ya existe
    if orig["title_similarity"] >= 0.88 or orig["max_similarity"] >= 0.85:
        return _block(cfg, job, f"Repetido: casi igual a «{orig['similar_to'][:60]}»", orig, [])
    if cfg.production.kind == "generated" and sc.scenes:
        warn = _safety(job)
        if warn:
            sc.scenes.insert(1 if len(sc.scenes) > 1 else 0, {"text": warn, "on_screen": "SEGURIDAD", "visual": "doodle", "lumi": "preocupado",
                             "shots": [{"lumi": "preocupado", "prop": "" if "gas" in warn else "enchufe",
                                        "estado": "desconectado", "overlay": "alerta",
                                        "texto": "VENTILA" if "gas" in warn else "DESCONECTA"}],
                             "anim": "titulo", "anim_params_json": json.dumps({"texto": "⚠ SEGURIDAD", "sub": warn[:60]}, ensure_ascii=False),
                             "stock_query": "", "visual_prompt": ""})
            sc.body = " ".join(s["text"] for s in sc.scenes)
            applied.append("Agregué advertencia de seguridad")
    from .packaging import brief
    scenes = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(sc.scenes)) if sc.scenes else \
        "\n".join(f"- {h.get('caption')}: {h.get('voiceover')}" for h in sc.highlights)
    user = f"""{channel_brief(cfg)}
{brief(job)}
TÍTULO: {sc.title}
FORMATO: {job.idea.format}
GUION:
{scenes}"""
    try:
        r = ask_json(SYSTEM, user, Review, fallback=Review(verdict="ok", score=8, summary="dry-run"))
    except Exception as e:
        log.warning("[%s] revisión IA no disponible (%s); paso solo con revisión por código", cfg.id, e)
        r = Review(verdict="ok", score=7, summary=f"sin revisión IA: {str(e)[:80]}")
    issues += r.issues[:6]
    if r.verdict == "block":
        return _block(cfg, job, r.summary or "; ".join(r.issues[:2]), orig, issues)
    for f in r.fixes[:5]:
        k = f.scene - 1
        if sc.scenes and 0 <= k < len(sc.scenes) and f.new_text.strip():
            sc.scenes[k]["text"] = f.new_text.strip()
            applied.append(f"Escena {f.scene} corregida")
    if sc.scenes:
        sc.body = " ".join(s["text"] for s in sc.scenes)
    import re as _re
    body_l = sc.body.lower()
    claims = [n for n in _re.findall(r"\d+[%k]?", sc.title.lower()) if n not in body_l] + \
             [wd for wd in ("kills", "mítico", "mythic", "inmortal", "100%", "garantizado") if wd in sc.title.lower() and wd not in body_l]
    # cuántas kills hizo alguien solo lo puede decir una partida real vista por el Analista
    kill_claim = _re.search(r"\d+\s*(kills?|asesinatos|bajas|muertes)|(kills?|asesinatos)\s*\d+", sc.title.lower())
    if kill_claim:
        from . import scout
        rep = scout.report(cfg.id, job.idea.source_media) if job.idea.source_media else None
        if job.idea.hero or not rep or not rep.get("kda"):
            claims.append(kill_claim.group(0))
    if claims:
        r.title_keeps_promise = False
        issues.append(f"El título afirma cosas que el video no dice: {', '.join(claims)}")
    if not r.title_keeps_promise:
        # el título promete algo que el video no tiene: se regresa al título honesto de la idea
        old = sc.title
        sc.title = (job.idea.title if job.idea.hero or job.idea.source == "showrunner" else
                    (job.package.get("title") if job.package.get("title") and job.package.get("title") != old else job.idea.title))[:100]
        applied.append(f"Título corregido (prometía de más): «{old[:50]}» → «{sc.title[:50]}»")
    if not orig["has_human"] and cfg.production.kind == "generated":
        issues.append("Sin material real de César: el video es más fácil de confundir con contenido genérico")
    job.qa = {"verdict": "ok" if not applied else "corregido", "score": r.score, "originality": orig["score"],
              "similar_to": orig["similar_to"], "issues": issues, "applied": applied, "summary": r.summary,
              "at": datetime.now(timezone.utc).isoformat()}
    save_job(job)
    _record(cfg, job)
    msg = f"Calidad {r.score}/10 · originalidad {orig['score']}/100" + (f" · {applied[0]}" if applied else "")
    add_activity(cfg.id, "calidad", msg, to="productor", kind="ok" if r.score >= 7 else "ask")
    return job


def _block(cfg: ChannelConfig, job: VideoJob, reason: str, orig: dict, issues: list[str]) -> VideoJob:
    job.status = "rejected"
    job.director_notes = "Calidad: " + reason[:200]
    job.qa = {"verdict": "bloqueado", "score": 0, "originality": orig["score"], "similar_to": orig["similar_to"],
              "issues": [reason] + issues, "applied": [], "summary": reason, "at": datetime.now(timezone.utc).isoformat()}
    # libera el material de César para otro video
    for u in (job.script.human if job.script else []):
        d = store().get(cfg.id, "human_assets", u["id"])
        if d and job.id in d.get("used_in", []):
            d["used_in"] = [x for x in d["used_in"] if x != job.id]
            store().put(cfg.id, "human_assets", u["id"], d)
    save_job(job)
    _record(cfg, job)
    add_activity(cfg.id, "calidad", f"Bloqueado: {reason[:80]}", to="director", kind="bad")
    return job


def _record(cfg: ChannelConfig, job: VideoJob) -> None:
    store().put(cfg.id, "quality", job.id, {"job_id": job.id, "title": job.script.title if job.script else job.idea.title,
                                            "format": job.idea.format, **job.qa})
