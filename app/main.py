"""API de la oficina. Para Render: uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
from __future__ import annotations

import logging
import os
import secrets

from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import orchestrator
from .config import load_channels, settings
from .db import insights, jobs, metrics, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = FastAPI(title="Oficina de Canales")
# El panel 3D puede vivir en otro dominio (artifact); los datos siguen protegidos por x-token.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

_PANEL = Path(__file__).parent / "static" / "panel.html"


@app.get("/panel", response_class=HTMLResponse)
def panel():
    """Vista aérea 3D de la oficina. Abre /panel y pon tu API_TOKEN en 'Conectar API'."""
    head = ('<!doctype html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>body{margin:0}</style></head><body>')
    return head + _PANEL.read_text(encoding="utf-8") + "</body></html>"


def auth(x_token: str = Header(default="")):
    tok = settings().API_TOKEN
    # En producción nunca aceptar el token por defecto (está a la vista en el código)
    if settings().MODE == "live" and (not tok or tok in ("dev", "cambia-esto") or len(tok) < 16):
        raise HTTPException(503, "API_TOKEN no configurado en el servidor")
    if not secrets.compare_digest(x_token, tok):
        raise HTTPException(401, "token inválido")


@app.get("/health")
def health():
    return {"ok": True, "mode": settings().MODE, "channels": list(load_channels())}


@app.post("/run/{channel_id}", dependencies=[Depends(auth)])
def run_channel(channel_id: str, bg: BackgroundTasks, publish: bool = True):
    bg.add_task(orchestrator.run_channel, channel_id, publish)
    return {"started": channel_id}


@app.post("/run", dependencies=[Depends(auth)])
def run_all(bg: BackgroundTasks, publish: bool = True):
    bg.add_task(orchestrator.run_all, publish)
    return {"started": "all"}


@app.get("/channels/{channel_id}/queue", dependencies=[Depends(auth)])
def queue(channel_id: str):
    return [
        {"id": j.id, "status": j.status, "format": j.idea.format, "title": j.script.title if j.script else j.idea.title,
         "scheduled_for": j.scheduled_for, "video_id": j.youtube_video_id, "error": j.error}
        for j in jobs(channel_id)
    ]


@app.get("/channels/{channel_id}/insights", dependencies=[Depends(auth)])
def get_insights(channel_id: str):
    return insights(channel_id)


@app.get("/channels/{channel_id}/metrics", dependencies=[Depends(auth)])
def get_metrics(channel_id: str):
    return metrics(channel_id)


@app.get("/channels/{channel_id}/decisions", dependencies=[Depends(auth)])
def decisions(channel_id: str):
    return store().list(channel_id, "decisions", limit=20)


@app.post("/channels/{channel_id}/jobs/{job_id}/reject", dependencies=[Depends(auth)])
def reject(channel_id: str, job_id: str):
    from .db import get_job, save_job
    j = get_job(channel_id, job_id)
    if not j:
        raise HTTPException(404)
    j.status = "rejected"
    save_job(j)
    return {"ok": True}


# Scheduler interno (opcional). En Render es mejor un Cron Job que llame POST /run.
if os.getenv("ENABLE_SCHEDULER") == "1":
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="America/Mexico_City")
    sched.add_job(orchestrator.run_all, "cron", hour=6, minute=17)
    sched.start()
