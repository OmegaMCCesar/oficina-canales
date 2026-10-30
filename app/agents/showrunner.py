"""SHOWRUNNER DE SERIES: videos largos encadenados que suman horas de reproducción.

- Mantiene las series del canal (Firestore `series`): temporadas con episodios en orden.
- Cada ciclo propone el siguiente episodio como idea de video LARGO (el Director decide).
- Cuando un episodio ya está en YouTube, propone un Short "tráiler" que manda al video completo.
- El Publicador agrega cada episodio a la playlist de su serie.
Por qué: las horas que cuentan para monetizar salen de videos largos, no de Shorts; y una
serie hace que la gente vea un capítulo tras otro.
"""
from __future__ import annotations

import logging
import re

from ..config import ChannelConfig
from ..db import add_activity, jobs, save_idea, store
from ..models import Idea

log = logging.getLogger("showrunner")

ACTIVE = ("approved", "scripted", "produced", "thumbnailed")


def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower())[:40].strip("-") or "serie"


def series(channel_id: str) -> list[dict]:
    return sorted(store().list(channel_id, "series"), key=lambda s: s.get("order", 0))


def _ensure_series(cfg: ChannelConfig) -> list[dict]:
    existing = {s["id"]: s for s in series(cfg.id)}
    for i, seed in enumerate(cfg.series_seeds):
        sid = _slug(seed["title"])
        if sid in existing:
            continue
        doc = {"id": sid, "title": seed["title"], "description": seed.get("description", ""), "order": i,
               "playlist_id": "", "active": True,
               "episodes": [{"n": n + 1, "title": t, "status": "planned", "job_id": "", "video_id": ""}
                            for n, t in enumerate(seed.get("episodes", []))]}
        store().put(cfg.id, "series", sid, doc)
        existing[sid] = doc
    return sorted(existing.values(), key=lambda s: s.get("order", 0))


def _sync(cfg: ChannelConfig, all_series: list[dict]) -> None:
    """Actualiza el estado de cada episodio según sus trabajos."""
    by_job = {j.id: j for j in jobs(cfg.id)}
    for s in all_series:
        changed = False
        for ep in s["episodes"]:
            j = by_job.get(ep.get("job_id"))
            if not j:
                # ¿hay un job de este episodio creado después de proponerlo?
                j = next((x for x in by_job.values() if x.idea.series_id == s["id"] and x.idea.episode == ep["n"]), None)
                if j:
                    ep["job_id"] = j.id
                    changed = True
            if not j:
                if ep["status"] not in ("planned", "proposed"):
                    ep["status"], changed = "planned", True
                continue
            st = {"rejected": "planned", "failed": "planned", "scheduled": "uploaded", "published": "published"}.get(j.status, "in_production")
            if st == "planned":
                ep["job_id"] = ""
            if j.youtube_video_id and ep.get("video_id") != j.youtube_video_id:
                ep["video_id"], changed = j.youtube_video_id, True
            if ep["status"] != st:
                ep["status"], changed = st, True
        if changed:
            store().put(cfg.id, "series", s["id"], s)


def run(cfg: ChannelConfig) -> list[Idea]:
    """Propone el siguiente episodio de cada serie activa (si no hay uno ya en camino) y tráileres."""
    all_series = _ensure_series(cfg)
    if not all_series:
        return []
    _sync(cfg, all_series)
    pending_ideas = store().list(cfg.id, "ideas")
    # episodios propuestos que el Director rechazó (ya no hay idea ni trabajo): vuelven a "planeado"
    for s in all_series:
        changed = False
        for ep in s["episodes"]:
            if ep["status"] == "proposed" and not ep.get("job_id") and not any(
                    i.get("series_id") == s["id"] and i.get("episode") == ep["n"] for i in pending_ideas):
                ep["status"], changed = "planned", True
        if changed:
            store().put(cfg.id, "series", s["id"], s)
    proposed: list[Idea] = []
    for s in all_series:
        if not s.get("active", True):
            continue
        in_flight = any(ep["status"] in ("in_production", "proposed") for ep in s["episodes"]) or \
            any(i.get("series_id") == s["id"] for i in pending_ideas)
        nxt = next((ep for ep in s["episodes"] if ep["status"] == "planned"), None)
        if in_flight or not nxt:
            continue
        if cfg.production.kind == "gameplay":
            # sin partida grabada no hay episodio: se le pide a César
            from .human_touch import add_request
            add_request(cfg, f"Para la serie «{s['title']}» graba una partida completa: {nxt['title']}", "video",
                        for_title=nxt["title"])
            continue
        idea = Idea(channel_id=cfg.id, title=nxt["title"], format="long", score=9.5, source="showrunner",
                    angle=f"Episodio {nxt['n']} de la serie «{s['title']}». {s['description']} "
                          f"Debe poder verse solo, pero al final invita al siguiente capítulo.",
                    search_intent=nxt["title"], series_id=s["id"], episode=nxt["n"])
        save_idea(idea)
        nxt["status"] = "proposed"
        store().put(cfg.id, "series", s["id"], s)
        proposed.append(idea)
        add_activity(cfg.id, "showrunner", f"Episodio {nxt['n']} de «{s['title']}»: {nxt['title']}", to="director")

    # tráileres: un Short por cada episodio largo ya subido que aún no tenga el suyo
    all_jobs = jobs(cfg.id)
    with_trailer = {j.idea.trailer_of for j in all_jobs if j.idea.trailer_of} | \
                   {i.get("trailer_of") for i in pending_ideas if i.get("trailer_of")}
    for j in all_jobs:
        if j.idea.format != "long" or not j.youtube_video_id or j.id in with_trailer or cfg.production.kind != "generated":
            continue
        t = Idea(channel_id=cfg.id, title=f"{(j.script.title if j.script else j.idea.title)[:70]} (en 40 segundos)",
                 format="short", score=8.5, source="showrunner", trailer_of=j.id,
                 angle="Short tráiler: el dato más sorprendente del episodio largo y al final manda a ver el video completo.",
                 search_intent=j.idea.search_intent, series_id=j.idea.series_id)
        save_idea(t)
        proposed.append(t)
        add_activity(cfg.id, "showrunner", f"Short tráiler para llevar gente al episodio: {t.title[:60]}", to="director")
        break   # uno por ciclo
    return proposed


def on_uploaded(cfg: ChannelConfig, job) -> None:
    """Tras subir un episodio: lo agrega a la playlist de su serie (la crea si no existe)."""
    if not job.idea.series_id or job.idea.format != "long" or not job.youtube_video_id:
        return
    s = store().get(cfg.id, "series", job.idea.series_id)
    if not s:
        return
    from ..tools import youtube
    try:
        pid = youtube.add_to_playlist(cfg.id, s.get("playlist_id", ""), s["title"], s.get("description", ""),
                                      job.youtube_video_id)
        if pid and pid != s.get("playlist_id"):
            s["playlist_id"] = pid
            store().put(cfg.id, "series", s["id"], s)
        add_activity(cfg.id, "showrunner", f"Agregado a la playlist «{s['title']}»", kind="ok")
    except Exception as e:
        log.warning("[%s] playlist no actualizada: %s", cfg.id, e)
