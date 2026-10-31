"""Ciclo diario de la oficina para un canal.

Orden:
  1. Analista   -> métricas + insights (memoria)
  2. Investigador -> ideas nuevas (lee memoria)
  3. Director   -> aprueba ideas -> VideoJobs
  4. Guionista  -> guion de cada job aprobado
  5. Productor  -> mp4
  6. Miniaturas -> 2 variantes, elige 1
  7. Director   -> asigna horario respetando cadencia
  8. Publicador -> sube privado + programado
"""
from __future__ import annotations

import logging
import traceback

from . import producers
from .agents import (analyst, animator, critic, director, hero_research, human_touch, monetization, packaging, quality, showrunner,)
from .agents import publisher, researcher, retention, seo, thumbnails, writer
from .config import get_channel, load_channels
from .db import add_activity as act, jobs, log_run, save_job, store

log = logging.getLogger("orchestrator")

MAX_PRODUCE_PER_RUN = 2   # tope por ciclo (cuidar cuota gratuita de Gemini)


class _NoModels(Exception):
    pass


def _step(job, fn, cfg):
    from .providers.text import ProvidersDown
    try:
        return fn(cfg, job)
    except ProvidersDown as e:
        # sin modelos de texto por cuota: el trabajo espera al siguiente ciclo, no se pierde
        job.error = f"en espera: {e}"
        save_job(job)
        raise _NoModels(str(e))
    except Exception as e:
        job.status = "failed"
        job.error = f"{fn.__module__}: {e}"
        save_job(job)
        log.error("[%s] %s falló en job %s\n%s", cfg.id, fn.__module__, job.id, traceback.format_exc())
        return job


def _keepalive(stop):
    """Render gratis apaga el servidor tras 15 min sin visitas: durante el ciclo nos visitamos solos."""
    import httpx

    from .config import settings
    url = settings().PUBLIC_URL.rstrip("/") + "/health"
    while not stop.wait(240):
        try:
            httpx.get(url, timeout=20)
        except Exception:
            pass


import threading as _th

_RUN_LOCK = _th.Lock()   # un solo ciclo a la vez en todo el servidor (memoria limitada)


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_channel(channel_id: str, publish: bool = True) -> dict:
    """Ejecuta el ciclo y SIEMPRE deja registro (aunque falle) para que el panel lo muestre."""
    import threading
    if not _RUN_LOCK.acquire(blocking=False):
        act(channel_id, "director", "Ya hay un ciclo en marcha; espera a que termine", kind="ask")
        return {"channel": channel_id, "status": "busy"}
    try:
        # hilo propio: la prioridad baja (nice) se queda en este hilo y no en los que atienden al panel
        out: dict = {}
        t = threading.Thread(target=lambda: out.update(_run_guarded(channel_id, publish) or {}), name=f"ciclo-{channel_id}")
        t.start()
        t.join()
        return out
    finally:
        _RUN_LOCK.release()


def _run_guarded(channel_id: str, publish: bool) -> dict:
    import os
    import threading
    # El ciclo corre con prioridad baja (en Linux "nice" es por hilo y lo heredan los ffmpeg que lance):
    # con 0.15 de CPU en Render, así el panel sigue respondiendo mientras se renderiza.
    try:
        os.nice(15)
    except Exception:
        pass
    stop = threading.Event()
    threading.Thread(target=_keepalive, args=(stop,), daemon=True).start()
    try:
        return _run_logged(channel_id, publish)
    finally:
        stop.set()


def _run_logged(channel_id: str, publish: bool) -> dict:
    from datetime import datetime, timezone
    started = datetime.now(timezone.utc).isoformat()
    rid = started[:19].replace(":", "")
    log_run(channel_id, {"channel": channel_id, "status": "running", "started": started}, rid)
    try:
        report = _run_channel(channel_id, publish)
        report.update(status="ok", started=started, finished=datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.error("[%s] ciclo falló\n%s", channel_id, traceback.format_exc())
        act(channel_id, "director", f"El ciclo falló: {str(e)[:120]}", kind="bad")
        report = {"channel": channel_id, "status": "failed", "error": str(e)[:400], "started": started,
                  "finished": datetime.now(timezone.utc).isoformat()}
    log_run(channel_id, report, rid)
    return report


def _run_channel(channel_id: str, publish: bool = True) -> dict:
    cfg = get_channel(channel_id)
    report: dict = {"channel": channel_id}

    act(cfg.id, "director", "¡Arranca el ciclo, equipo!", kind="ok")
    _revive(cfg)
    _dedupe(cfg)
    act(cfg.id, "analista", "Revisando métricas de los videos publicados…")
    a = analyst.run(cfg)
    report["analyst"] = a.summary if a else "sin métricas"
    if a and a.insights:
        act(cfg.id, "analista", f"Aprendí: {a.insights[0].statement}", to="director", kind="ok")
    else:
        act(cfg.id, "analista", "Aún no hay métricas para aprender")

    act(cfg.id, "monetizacion", "Midiendo el avance para monetizar…")
    prog = _safe(monetization.progress, cfg)
    if prog:
        hours = prog.get("long_hours_365")
        act(cfg.id, "monetizacion", f"Suscriptores {prog['subs']}/500 · horas de largos {hours if hours is not None else '?'}/3000",
            to="director")
    act(cfg.id, "showrunner", "Revisando las series: ¿qué capítulo sigue?")
    _safe(showrunner.run, cfg)

    if cfg.production.kind == "gameplay":
        from .tools import gdrive
        act(cfg.id, "humano", "Revisando tu carpeta de Drive…")
        try:
            got = gdrive.sync(cfg)
        except Exception as e:
            log.exception("[%s] Drive falló", cfg.id)
            gdrive._note(cfg, f"error: {str(e)[:160]}")
            act(cfg.id, "humano", f"No pude leer tu Drive: {str(e)[:90]}", kind="bad")
            got = []
        if not got:
            act(cfg.id, "humano", "Drive: " + (gdrive.status().get("last") or "sin partidas nuevas"))
        from .agents import scout
        _safe(scout.run, cfg)
        act(cfg.id, "investigador", "Investigando un héroe en la wiki (historia y habilidades)…")
        _safe(hero_research.run, cfg)
    act(cfg.id, "investigador", "Buscando temas: comentarios, tendencias y memoria del canal…")
    new_ideas = researcher.run(cfg)
    report["ideas"] = len(new_ideas)
    best = max(new_ideas, key=lambda i: i.score).title if new_ideas else "ninguna"
    act(cfg.id, "investigador", f"{len(new_ideas)} ideas. La mejor: {best}", to="director")

    act(cfg.id, "director", "Revisando ideas y la cola de videos…")
    d = director.run(cfg)
    report["approved"] = len(d.approved_idea_ids)
    report["requests_to_human"] = d.requests_to_human
    if d.requests_to_human:
        act(cfg.id, "director", "César: " + d.requests_to_human[0], kind="ask")

    produced = 0
    queue = jobs(cfg.id, "approved") + jobs(cfg.id, "scripted") + jobs(cfg.id, "produced")
    queue.sort(key=lambda j: not j.anim_redo)   # lo que César pidió rehacer va primero
    attempted = 0
    try:
        for job in queue:
            if produced >= MAX_PRODUCE_PER_RUN or attempted >= MAX_PRODUCE_PER_RUN + 1:
                break
            attempted += 1
            title = job.idea.title[:60]
            if job.status == "approved":
                act(cfg.id, "director", f"Aprobada: {title}", to="empaque")
                act(cfg.id, "empaque", "Diseñando 3 empaques (título + miniatura) antes del guion…")
                job = _safe(packaging.run, cfg, job) or job
                if job.package:
                    act(cfg.id, "empaque", f"«{job.package['title'][:50]}» + «{job.package['thumb_text']}»", to="director")
                    act(cfg.id, "director", f"Empaque aprobado: {job.package.get('reason', '')[:70]}", to="guionista", kind="ok")
                act(cfg.id, "guionista", f"Escribiendo guion para cumplir la promesa: {title}")
                job = _step(job, writer.run, cfg)
                if job.status == "scripted":
                    act(cfg.id, "guionista", f"Guion listo ({len(job.script.scenes or job.script.highlights)} partes)", to="retencion")
                    act(cfg.id, "retencion", "Afinando gancho de 1 s, ritmo, loop y llamadas a la acción…")
                    job = retention.run(cfg, job)
                    hook = job.script.hook_text or job.script.hook
                    act(cfg.id, "retencion", f"Gancho: «{hook[:50]}» · {len(job.script.scenes or job.script.highlights)} cortes", to="seo")
                    act(cfg.id, "seo", "Optimizando título, descripción, etiquetas y hashtags…")
                    job = seo.run(cfg, job)
                    act(cfg.id, "seo", f"Título: {job.script.title[:60]}", to="monetizacion")
                    job = _safe(monetization.run, cfg, job) or job
                    n_links = sum(1 for p in job.script.products if p.get("url"))
                    act(cfg.id, "monetizacion", f"{n_links} link(s) de afiliado en la descripción" if n_links else
                        "Descripción con link a tu sitio; sin productos con link aún", to="animador")
                    job = _animate(cfg, job)
                    if cfg.production.kind == "generated":
                        job = _safe(human_touch.attach, cfg, job) or job
                    act(cfg.id, "calidad", "Revisando datos técnicos, seguridad, promesa y originalidad…")
                    job = _safe(quality.run, cfg, job) or job
                    if job.status == "rejected":
                        act(cfg.id, "director", f"Descartado por Calidad: {title}", kind="bad")
                        continue
            elif job.status == "scripted" and job.anim_redo:
                act(cfg.id, "animador", f"César pidió rehacer la animación: «{job.anim_notes[:60]}»", kind="ask")
                job = _animate(cfg, job, job.anim_notes)
            if job.status == "scripted":
                act(cfg.id, "productor", f"Generando imágenes, voz y video: {title}")
                job = _step(job, producers.run, cfg)
                if job.status == "produced":
                    act(cfg.id, "productor", "Video renderizado", to="critico")
                    act(cfg.id, "critico", "Viendo el video cuadro por cuadro…")
                    job = _safe(critic.run, cfg, job) or job
                    if job.status == "produced":
                        act(cfg.id, "critico", "Aprobado para miniatura", to="miniaturas")
            from .memory import trim
            trim()
            if job.status == "produced":
                act(cfg.id, "miniaturas", f"Diseñando 2 miniaturas: {title}")
                job = _step(job, thumbnails.run, cfg)
                if job.status == "thumbnailed":
                    act(cfg.id, "miniaturas", "Miniatura elegida", to="director")
                    produced += 1
            if job.status == "failed":
                act(cfg.id, "director", f"Falló: {title} ({(job.error or '')[:80]})", kind="bad")
    except _NoModels as e:
        act(cfg.id, "director", "Los modelos de texto gratis llegaron a su límite por ahora; los videos pendientes esperan al siguiente ciclo (no se pierden).", kind="ask")
        report["paused"] = str(e)[:200]
    report["produced"] = produced
    _safe(human_touch.make_requests, cfg)

    uploaded = []
    if publish:
        for job in director.assign_slots(cfg):
            act(cfg.id, "director", f"A YouTube: {job.script.title[:60]}", to="publicador")
            act(cfg.id, "publicador", "Subiendo video como privado…")
            job = publisher.run(cfg, job)
            if job.status == "scheduled":
                act(cfg.id, "publicador", "Subido a YouTube Studio (privado)", to="portal", kind="ok")
            elif job.status == "scripted":
                act(cfg.id, "publicador", "El archivo se perdió en un reinicio; vuelve a producción", to="productor", kind="ask")
            else:
                act(cfg.id, "publicador", f"No se pudo subir: {(job.error or '')[:80]}", kind="bad")
            if job.status == "scheduled":
                uploaded.append({"title": job.script.title, "at": job.scheduled_for.isoformat(),
                                 "video_id": job.youtube_video_id})
    report["scheduled"] = uploaded
    report["failed"] = [{"id": j.id, "error": j.error} for j in jobs(cfg.id, "failed")][:5]

    act(cfg.id, "director", f"Ciclo terminado: {produced} video(s) listos", kind="ok")
    log.info("[%s] ciclo terminado: %s", cfg.id, report)
    return report


def _safe(fn, *args):
    """Un agente nuevo que falla no debe tirar el ciclo completo."""
    from .providers.text import ProvidersDown
    try:
        return fn(*args)
    except ProvidersDown as e:
        log.warning("%s sin modelos de texto por ahora: %s", getattr(fn, "__module__", fn), e)
        return None
    except Exception as e:
        log.exception("%s falló: %s", getattr(fn, "__module__", fn), e)
        return None


def _animate(cfg, job, cesar_notes: str = ""):
    """Animador planea el storyboard con coherencia y el Director lo aprueba (o pide 1 corrección)."""
    if cfg.production.kind != "generated":
        return job
    act(cfg.id, "animador", "Armando storyboard: un equipo, progresión lógica y Lumi coherente…")
    try:
        job = animator.run(cfg, job, cesar_notes)
        sb = store().get(cfg.id, "storyboards", job.id) or {}
        v = sb.get("verdict", {})
        act(cfg.id, "animador", f"Storyboard listo ({sb.get('appliance', '?')}, rev {sb.get('revision', 0)})", to="director")
        msg = f"Animación {sb.get('status', 'aprobada').replace('_', ' ')} · {v.get('score', '?')}/10"
        if v.get("issues"):
            msg += f" · {v['issues'][0][:70]}"
        act(cfg.id, "director", msg, to="productor", kind="ok" if v.get("approve") else "ask")
    except Exception as e:
        log.warning("[%s] animador falló (%s); sigo con el plan de Retención", cfg.id, e)
        act(cfg.id, "animador", f"No pude revisar la animación: {str(e)[:70]}", kind="bad")
    job.anim_redo = False
    save_job(job)
    return job


def _revive(cfg) -> None:
    """Recupera trabajos que fallaron por causas pasajeras (sin modelos, archivo perdido en un reinicio)."""
    for j in jobs(cfg.id, "failed"):
        err = j.error or ""
        if "Ningún proveedor" in err or "No such file" in err or "en espera" in err:
            j.status = "scripted" if (j.script and (j.script.scenes or j.script.highlights)) else "approved"
            j.video_path, j.thumbnail_paths, j.thumbnail_chosen = None, [], None
            j.error = None
            save_job(j)


def _dedupe(cfg) -> None:
    """Rechaza trabajos repetidos (mismo título) y recorta la cola al máximo permitido."""
    seen, kept = set(), 0
    pending = sorted(jobs(cfg.id, "approved") + jobs(cfg.id, "scripted"), key=lambda j: j.created_at)
    for j in pending:
        key = j.idea.title.strip().lower()
        if key in seen or kept >= cfg.cadence.max_queue_ready:
            j.status = "rejected"
            j.director_notes = "duplicado o cola llena"
            save_job(j)
        else:
            seen.add(key)
            kept += 1


def run_all(publish: bool = True) -> list[dict]:
    return [run_channel(cid, publish) for cid in load_channels()]
