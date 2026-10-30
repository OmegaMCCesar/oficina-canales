"""DIRECTOR DEL CANAL.

- Lee memoria del canal (insights del Analista) y estado de la cola.
- Aprueba/rechaza ideas del Investigador y las convierte en VideoJobs.
- Calcula el calendario respetando la cadencia (shorts/día, largos/semana, gap mínimo).
- Pide material al humano cuando hace falta (gaming).
- Es el único que decide qué se publica y cuándo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import ideas as stored_ideas, jobs, save_decision, save_job, store
from ..llm import ask_json
from ..models import DirectorDecision, Idea, VideoJob
from .base import channel_brief, insights_block

log = logging.getLogger("director")


class Approval(BaseModel):
    idea_id: str
    approve: bool
    reason: str
    priority: int = 5          # 1 = publicar primero


class DirectorOut(BaseModel):
    approvals: list[Approval]
    requests_to_human: list[str] = []
    rationale: str


SYSTEM = """Eres el DIRECTOR de un canal de YouTube. Decides qué se produce y en qué orden.
Criterios, en este orden:
1. Lo que la memoria del canal dice que funciona (retención y CTR altos) pesa más que la intuición.
2. Variedad: no aprobar dos ideas casi iguales en la misma semana.
3. Respetar las reglas del canal al pie de la letra.
4. No saturar: si la cola de videos listos ya cubre más de 7 días de cadencia, aprueba pocas o ninguna.
Sé exigente: rechaza ideas flojas con una razón de una línea. Si necesitas algo del humano
(grabar partidas con cierto héroe, confirmar un dato técnico), ponlo en requests_to_human."""


def _slots(cfg: ChannelConfig, start: datetime, n: int, format_: str,
           extra_taken: list[datetime] | None = None) -> list[datetime]:
    """Genera n horarios de publicación válidos a partir de `start`, respetando cadencia."""
    tz = ZoneInfo(cfg.cadence.timezone)
    taken = sorted([j.scheduled_for for j in jobs(cfg.id, "scheduled") if j.scheduled_for] + (extra_taken or []))
    per_day = cfg.cadence.shorts_per_day if format_ == "short" else max(1, cfg.cadence.longs_per_week // 7 + 1)
    out: list[datetime] = []
    day = start.astimezone(tz).date()
    guard = 0
    while len(out) < n and guard < 60:
        guard += 1
        day_count = 0
        for h in cfg.cadence.publish_hours_local:
            cand = datetime(day.year, day.month, day.day, h, 0, tzinfo=tz)
            if cand <= start.astimezone(tz):
                continue
            if any(abs((cand - t.astimezone(tz)).total_seconds()) < cfg.cadence.min_gap_hours * 3600
                   for t in taken + out):
                continue
            same_day = sum(1 for t in taken + out if t.astimezone(tz).date() == day)
            if same_day >= per_day:
                break
            out.append(cand)
            day_count += 1
            if len(out) >= n or day_count >= per_day:
                break
        day += timedelta(days=1)
    # largos: máximo longs_per_week por semana
    if format_ == "long":
        out = out[: cfg.cadence.longs_per_week]
    return out


def run(cfg: ChannelConfig) -> DirectorDecision:
    candidates: list[Idea] = stored_ideas(cfg.id)
    ready_queue = jobs(cfg.id, "produced") + jobs(cfg.id, "thumbnailed")
    in_progress = [j for j in jobs(cfg.id) if j.status in ("approved", "scripted")]
    scheduled = jobs(cfg.id, "scheduled")

    daily_capacity = cfg.cadence.shorts_per_day + cfg.cadence.longs_per_week / 7
    days_covered = (len(ready_queue) + len(scheduled)) / max(daily_capacity, 0.1)

    user = f"""{channel_brief(cfg)}

{insights_block(cfg.id)}

ESTADO DE LA COLA:
- Listos para publicar: {len(ready_queue)} | Programados: {len(scheduled)} | En producción: {len(in_progress)}
- Días de contenido cubiertos: {days_covered:.1f} (cadencia: {cfg.cadence.shorts_per_day} short/día, {cfg.cadence.longs_per_week} largos/semana)
- Máximo de listos permitido: {cfg.cadence.max_queue_ready}

IDEAS CANDIDATAS:
{chr(10).join(f'- id={i.id} | {i.format} | score={i.score} | {i.title} — {i.angle}' + (f' | media={i.source_media}' if i.source_media else '') for i in candidates) or '- (ninguna)'}

Aprueba las que merecen producirse ahora (máximo {max(0, cfg.cadence.max_queue_ready - len(ready_queue) - len(in_progress))}).
"""
    room = max(0, cfg.cadence.max_queue_ready - len(ready_queue) - len(in_progress))
    fallback = DirectorOut(
        approvals=[Approval(idea_id=i.id, approve=(k < min(room, 3)), priority=k + 1,
                            reason="dry-run: aprobadas las de mayor score" if k < 3 else "dry-run: cola llena")
                   for k, i in enumerate(sorted(candidates, key=lambda x: -x.score))],
        requests_to_human=["Graba 2-3 partidas nuevas esta semana"] if cfg.production.kind == "gameplay" else [],
        rationale="dry-run",
    )
    system = SYSTEM + ("\nEste canal es 100% GENERADO por IA (imágenes y voz): el humano NO graba nada. "
                       "Solo pídele algo si necesitas confirmar un dato técnico; si no, deja requests_to_human vacío."
                       if cfg.production.kind == "generated" else "")
    out = ask_json(system, user, DirectorOut, fallback=fallback)

    approved_ids, rejected = [], {}
    by_id = {i.id: i for i in candidates}
    for a in sorted(out.approvals, key=lambda x: x.priority):
        idea = by_id.get(a.idea_id)
        if not idea:
            continue
        if cfg.production.kind == "gameplay" and not idea.source_media:
            rejected[idea.id] = "sin partida grabada: no se puede producir"
            if "Graba partidas nuevas para: " + idea.title not in out.requests_to_human:
                out.requests_to_human.append("Graba partidas nuevas para: " + idea.title)
        elif a.approve and len(approved_ids) < room:
            job = VideoJob(channel_id=cfg.id, idea=idea, status="approved", director_notes=a.reason)
            save_job(job)
            approved_ids.append(idea.id)
        else:
            rejected[idea.id] = a.reason
        store().delete(cfg.id, "ideas", idea.id)   # la idea ya fue decidida

    decision = DirectorDecision(channel_id=cfg.id, approved_idea_ids=approved_ids, rejected=rejected,
                                schedule_plan=[], requests_to_human=out.requests_to_human,
                                rationale=out.rationale)
    save_decision(decision)
    log.info("[%s] Director aprobó %d, rechazó %d. Pide al humano: %s",
             cfg.id, len(approved_ids), len(rejected), out.requests_to_human)
    return decision


def assign_slots(cfg: ChannelConfig) -> list[VideoJob]:
    """Asigna fecha de publicación a los jobs con miniatura lista. Devuelve los que toca subir ya."""
    ready = sorted(jobs(cfg.id, "thumbnailed"), key=lambda j: j.created_at)
    now = datetime.now(timezone.utc)
    to_publish: list[VideoJob] = []
    for fmt in ("short", "long"):
        group = [j for j in ready if j.idea.format == fmt]
        already = [j.scheduled_for for j in to_publish]
        for job, slot in zip(group, _slots(cfg, now, len(group), fmt, already)):
            job.scheduled_for = slot
            to_publish.append(job)
    return to_publish


# ---------------- revisión de animación ----------------

class AnimReview(BaseModel):
    approve: bool
    score: int                 # 1-10 coherencia y claridad
    issues: list[str] = []     # problemas concretos por número de escena
    summary: str


SYSTEM_ANIM = """Eres el DIRECTOR revisando el storyboard de animación antes de producir.
Aprueba solo si:
- Todas las escenas muestran el MISMO equipo protagonista o elementos coherentes con él.
- La animación de cada escena ilustra lo que se narra en ESA escena (no otra cosa).
- Hay progresión lógica (síntoma -> causa -> medición -> solución) y los valores/estados no se contradicen.
- Lumi tiene la expresión adecuada (advertencia en riesgos, celebración al final) y no satura.
- El gancho (escena 1) es visualmente fuerte.
Si algo falla, approve=false y en issues di exactamente qué escena y qué cambiar. Sé exigente pero práctico:
detalles menores no bloquean (score >= 7 con issues menores puede aprobarse)."""


def review_animation(cfg, job, plan, code_issues: list[str]) -> dict:
    lines = [f"{i+1}. «{s['text']}» -> {s.get('visual')}:{s.get('anim')} {s.get('anim_params_json')} lumi={s.get('lumi')}"
             for i, s in enumerate(job.script.scenes)]
    user = f"""{channel_brief(cfg)}
VIDEO: {job.script.title} ({job.idea.format})
EQUIPO: {plan.appliance}
CONTINUIDAD DECLARADA: {plan.continuity}
STORYBOARD:
{chr(10).join(lines)}
PROBLEMAS DETECTADOS AUTOMÁTICAMENTE: {code_issues or 'ninguno'}"""
    fallback = AnimReview(approve=not code_issues, score=8 if not code_issues else 5, issues=code_issues,
                          summary="dry-run")
    try:
        r = ask_json(SYSTEM_ANIM, user, AnimReview, fallback=fallback)
    except Exception as e:
        r = AnimReview(approve=not code_issues, score=6, issues=code_issues, summary=f"revisión no disponible: {e}")
    if code_issues and r.approve and r.score < 7:
        r.approve = False
    return r.model_dump()
