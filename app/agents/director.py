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
    out = ask_json(system, user, DirectorOut, fallback=fallback, smart=True)

    approved_ids, rejected = [], {}
    by_id = {i.id: i for i in candidates}
    for a in sorted(out.approvals, key=lambda x: x.priority):
        idea = by_id.get(a.idea_id)
        if not idea:
            continue
        if cfg.production.kind == "gameplay" and not idea.source_media and not idea.facts:
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
    """El Director agenda los videos listos (día y hora con criterio propio) y devuelve los que hay que subir.
    Se suben a YouTube como privados de inmediato; se publican a su hora cuando César aprueba en el calendario."""
    plan_schedule(cfg)
    return sorted(jobs(cfg.id, "thumbnailed"), key=lambda j: j.scheduled_for or j.created_at)


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
    def _desc(s):
        if s.get("shots"):
            return " | ".join(f"Lumi {x.get('lumi') or '-'} + {x.get('prop') or 'sin objeto'}({x.get('estado', 'normal')})"
                              f" + {x.get('overlay', 'ninguno')}" + (f" «{x['texto']}»" if x.get("texto") else "") +
                              (f" valor {x['valor']}" if x.get("valor") else "") for x in s["shots"])
        return f"{s.get('visual')}:{s.get('anim')} {s.get('anim_params_json')} lumi={s.get('lumi')}"
    lines = [f"{i+1}. «{s['text']}» -> {_desc(s)}" for i, s in enumerate(job.script.scenes)]
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
        r = ask_json(SYSTEM_ANIM, user, AnimReview, fallback=fallback, smart=True)
    except Exception as e:
        r = AnimReview(approve=not code_issues, score=6, issues=code_issues, summary=f"revisión no disponible: {e}")
    if code_issues and r.approve and r.score < 7:
        r.approve = False
    return r.model_dump()


# ---------------- calendario de publicación ----------------

class Slot(BaseModel):
    job_id: str
    when: str            # "YYYY-MM-DD HH:MM" hora de Ciudad de México
    reason: str          # una línea: por qué ese día y hora


class ScheduleOut(BaseModel):
    slots: list[Slot]
    notes: str = ""


SYSTEM_SCHEDULE = """Eres el DIRECTOR del canal y decides CUÁNDO se publica cada video listo. Objetivo: que cada video
salga cuando su audiencia está en YouTube y que el canal suba con ritmo constante (sin ráfagas ni huecos largos).
Criterios:
- Audiencia en México: entre semana suelen funcionar 13-15 h (comida) y 19-22 h (noche); fines de semana desde las 11 h.
  Gaming: tarde-noche y fines de semana. Técnicos/usuarios de línea blanca: mediodía y noche.
- Si hay DATOS DE HORARIOS del canal, pesan más que la intuición.
- Respeta la cadencia del canal y la separación mínima entre videos.
- Videos largos de una serie: en orden de capítulo y en días distintos. Un Short tráiler va DESPUÉS de su episodio.
- Alterna temas: no pongas dos videos casi iguales seguidos.
- Agenda dentro de los próximos 14 días; los más importantes primero.
Para cada video devuelve fecha y hora (hora de CDMX, en punto o y media) y una razón corta."""


def _hour_stats(cfg: ChannelConfig) -> str:
    """Rendimiento por hora/día de publicación (vistas a 7 días), para que el Director aprenda."""
    from ..db import metrics as stored_metrics
    tz = ZoneInfo(cfg.cadence.timezone)
    pub = {j.youtube_video_id: j for j in jobs(cfg.id, "published") if j.published_at and j.youtube_video_id}
    rows: dict[str, list[int]] = {}
    for m in stored_metrics(cfg.id, "7d"):
        j = pub.get(m.youtube_video_id)
        if not j:
            continue
        lt = j.published_at.astimezone(tz)
        key = f"{['lun','mar','mié','jue','vie','sáb','dom'][lt.weekday()]} {lt.hour:02d}h ({j.idea.format})"
        rows.setdefault(key, []).append(m.views)
    if not rows:
        return "DATOS DE HORARIOS: aún no hay videos publicados con métricas."
    lines = [f"- {k}: {sum(v)//len(v)} vistas promedio en 7 días ({len(v)} videos)" for k, v in sorted(rows.items(), key=lambda x: -sum(x[1]) / len(x[1]))]
    return "DATOS DE HORARIOS (lo publicado y cómo le fue):\n" + "\n".join(lines[:12])


def _valid(cfg: ChannelConfig, when: datetime, fmt: str, taken: list[tuple[datetime, str]]) -> bool:
    tz = ZoneInfo(cfg.cadence.timezone)
    lt = when.astimezone(tz)
    if not (8 <= lt.hour <= 22):
        return False
    gap = cfg.cadence.min_gap_hours * 3600
    if any(abs((when - t).total_seconds()) < gap for t, _ in taken):
        return False
    same_day = [f for t, f in taken if t.astimezone(tz).date() == lt.date()]
    if fmt == "short" and same_day.count("short") >= cfg.cadence.shorts_per_day:
        return False
    if fmt == "long":
        week = lt.isocalendar()[:2]
        if sum(1 for t, f in taken if f == "long" and t.astimezone(tz).isocalendar()[:2] == week) >= cfg.cadence.longs_per_week:
            return False
        if "long" in same_day:
            return False
    return True


def _next_preferred(cfg: ChannelConfig, start: datetime, fmt: str, taken: list[tuple[datetime, str]]) -> datetime:
    """Respaldo sin IA: la siguiente hora preferida del canal que respete la cadencia."""
    tz = ZoneInfo(cfg.cadence.timezone)
    day = start.astimezone(tz).date()
    for _ in range(21):
        for h in sorted(cfg.cadence.publish_hours_local):
            cand = datetime(day.year, day.month, day.day, h, 0, tzinfo=tz).astimezone(timezone.utc)
            if cand >= start and _valid(cfg, cand, fmt, taken):
                return cand
        day += timedelta(days=1)
    return _nearest_valid(cfg, start, fmt, taken)


def _nearest_valid(cfg: ChannelConfig, start: datetime, fmt: str, taken: list[tuple[datetime, str]]) -> datetime:
    t = start.replace(minute=0 if start.minute < 30 else 30, second=0, microsecond=0)
    for _ in range(14 * 48):
        if _valid(cfg, t, fmt, taken):
            return t
        t += timedelta(minutes=30)
    return start


def plan_schedule(cfg: ChannelConfig) -> list[VideoJob]:
    """El Director (re)planea día y hora de los videos listos que César aún no aprueba. Los aprobados no se mueven."""
    tz = ZoneInfo(cfg.cadence.timezone)
    now = datetime.now(timezone.utc)
    floor = now + timedelta(minutes=45)
    movable = [j for j in jobs(cfg.id, "thumbnailed") + jobs(cfg.id, "scheduled") if not j.approved]
    fixed = [(j.scheduled_for, j.idea.format) for j in jobs(cfg.id, "scheduled")
             if j.approved and j.scheduled_for and j.scheduled_for > now - timedelta(hours=1)]
    fixed += [(j.published_at, j.idea.format) for j in jobs(cfg.id, "published") if j.published_at and j.published_at > now - timedelta(days=7)]
    if not movable:
        return []
    by_id = {j.id: j for j in movable}
    listing = "\n".join(
        f"- id={j.id} | {j.idea.format} | {j.script.title if j.script else j.idea.title}"
        + (f" | serie {j.idea.series_id} cap {j.idea.episode}" if j.idea.series_id and j.idea.episode else "")
        + (f" | tráiler del video {j.idea.trailer_of}" if j.idea.trailer_of else "")
        + (f" | ahora: {j.scheduled_for.astimezone(tz):%a %d %H:%M}" if j.scheduled_for else "")
        for j in movable)
    taken_txt = "\n".join(f"- {t.astimezone(tz):%a %d %b %H:%M} ({f})" for t, f in sorted(fixed)) or "- (ninguno)"
    user = f"""{channel_brief(cfg)}
CADENCIA: {cfg.cadence.shorts_per_day} short(s) por día, {cfg.cadence.longs_per_week} largo(s) por semana, mínimo {cfg.cadence.min_gap_hours} h entre videos.
HORAS PREFERIDAS HASTA AHORA: {cfg.cadence.publish_hours_local}
AHORA: {now.astimezone(tz):%A %d %b %Y %H:%M} (CDMX)
{_hour_stats(cfg)}
{insights_block(cfg.id, 10)}
YA OCUPADOS (aprobados o publicados):
{taken_txt}
VIDEOS POR AGENDAR:
{listing}"""
    fallback = ScheduleOut(slots=[], notes="dry-run")
    try:
        out = ask_json(SYSTEM_SCHEDULE, user, ScheduleOut, fallback=fallback, smart=True)
    except Exception as e:
        log.warning("[%s] calendario sin IA (%s); uso la cadencia", cfg.id, e)
        out = fallback
    taken = list(fixed)
    planned: list[VideoJob] = []
    proposals = []
    for s in out.slots:
        j = by_id.get(s.job_id)
        if not j:
            continue
        try:
            when = datetime.strptime(s.when.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
        proposals.append((max(when, floor), j, s.reason))
    seen = {j.id for _, j, _ in proposals}
    # los que la IA no agendó: cadencia normal
    for j in sorted(movable, key=lambda x: x.created_at):
        if j.id not in seen:
            proposals.append(((j.scheduled_for if j.scheduled_for and j.scheduled_for > floor else floor), j, "según la cadencia del canal"))
    # orden: episodios por capítulo y tráileres después de su episodio
    proposals.sort(key=lambda p: (p[0], p[1].idea.episode or 0))
    slot_of: dict[str, datetime] = {}
    for when, j, reason in proposals:
        if j.idea.trailer_of and j.idea.trailer_of in slot_of:
            when = max(when, slot_of[j.idea.trailer_of] + timedelta(hours=cfg.cadence.min_gap_hours))
        w = (_next_preferred(cfg, when, j.idea.format, taken) if j.id not in seen
             else _nearest_valid(cfg, when, j.idea.format, taken))
        taken.append((w, j.idea.format))
        slot_of[j.id] = w
        moved = j.scheduled_for != w
        j.scheduled_for, j.schedule_reason = w, reason[:160]
        if moved or j.status == "thumbnailed":
            save_job(j)
        planned.append(j)
    log.info("[%s] calendario: %d videos agendados. %s", cfg.id, len(planned), out.notes[:120])
    return planned
