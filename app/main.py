"""API de la oficina. Para Render: uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
from __future__ import annotations

import logging
import os
import secrets

from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from . import orchestrator
from .config import load_channels, settings
from .db import insights, jobs, metrics, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = FastAPI(title="Oficina de Canales")
# El panel 3D puede vivir en otro dominio (artifact); los datos siguen protegidos por x-token.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

_PANEL = Path(__file__).parent / "static" / "panel.html"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel")


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
    if orchestrator.is_running():
        raise HTTPException(409, "Ya hay un ciclo en marcha; espera a que termine")
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


@app.get("/providers", dependencies=[Depends(auth)])
def providers_status():
    """Qué proveedores de IA tienen llave y cómo les ha ido (usos, fallas)."""
    from .providers import health, key
    keys = ["GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "GITHUB_MODELS_TOKEN",
            "MISTRAL_API_KEY", "CLOUDFLARE_API_TOKEN", "PEXELS_API_KEY", "PIXABAY_API_KEY"]
    return {"configurados": {k: bool(key(k)) for k in keys}, "voz_gratis": "edge-tts (sin llave)", **health.report()}


@app.get("/channels/{channel_id}/activity", dependencies=[Depends(auth)])
def get_activity(channel_id: str, since: str = ""):
    from .db import activity
    return activity(channel_id, since)


@app.get("/channels/{channel_id}/runs", dependencies=[Depends(auth)])
def runs(channel_id: str):
    docs = store().list(channel_id, "runs", limit=200)
    docs.sort(key=lambda d: str(d.get("finished") or d.get("started") or ""), reverse=True)
    return docs[:5]


@app.get("/channels/{channel_id}/status", dependencies=[Depends(auth)])
def channel_status(channel_id: str):
    from .db import get_secret
    from .tools import youtube
    info = None
    try:
        info = get_secret(channel_id, "youtube_channel")
    except Exception:
        pass
    return {"channel": channel_id, "mode": settings().MODE, "publish_mode": settings().PUBLISH_MODE,
            "youtube_authorized": youtube.is_authorized(channel_id), "youtube_channel": info}


# ---------- Autorizar YouTube desde el navegador ----------
import hashlib  # noqa: E402
import hmac  # noqa: E402

from fastapi import Request  # noqa: E402


def _sign(value: str) -> str:
    return hmac.new(settings().API_TOKEN.encode(), value.encode(), hashlib.sha256).hexdigest()[:24]


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#11131d;color:#eef0f7;
font:16px system-ui,sans-serif;padding:16px}}main{{max-width:460px;background:#181b2a;border:1px solid #2a2e45;
border-radius:14px;padding:24px}}h1{{color:#f2c14e;font-size:22px;margin:0 0 12px}}a{{color:#7bd389}}</style>
</head><body><main><h1>{title}</h1>{body}<p><a href="/panel">Volver a la oficina</a></p></main></body></html>""")


@app.get("/oauth/start/{channel_id}", include_in_schema=False)
def oauth_start(channel_id: str, t: str = ""):
    """Abre esta URL en el navegador: /oauth/start/lcmc?t=TU_API_TOKEN"""
    if not secrets.compare_digest(t, settings().API_TOKEN):
        return _page("Token inválido", "<p>Agrega <code>?t=TU_TOKEN</code> al final de la dirección.</p>")
    if channel_id not in load_channels():
        return _page("Canal desconocido", f"<p>No existe el canal <b>{channel_id}</b>.</p>")
    if not settings().YOUTUBE_CLIENT_ID:
        return _page("Falta configuración", "<p>El servidor no tiene YOUTUBE_CLIENT_ID.</p>")
    from .tools import youtube
    state = f"{channel_id}.{_sign(channel_id)}"
    return RedirectResponse(youtube.auth_url(state))


@app.get("/oauth/callback", include_in_schema=False)
def oauth_callback(request: Request, state: str = "", error: str = ""):
    if error:
        return _page("Autorización cancelada", f"<p>Google respondió: {error}</p>")
    channel_id, _, sig = state.partition(".")
    if not channel_id or not secrets.compare_digest(sig, _sign(channel_id)):
        return _page("Solicitud inválida", "<p>Vuelve a empezar desde la oficina.</p>")
    from .tools import youtube
    try:
        info = youtube.finish_auth(channel_id, state, str(request.url))
    except Exception as e:
        logging.exception("oauth")
        return _page("No se pudo autorizar", f"<p>{str(e)[:300]}</p>")
    cfg = load_channels()[channel_id]
    expected = (cfg.youtube_handle or "").lower()
    got = (info.get("handle") or "").lower()
    warn = ""
    if expected and got and expected != got:
        warn = (f"<p style='color:#ff9f68'>Ojo: esperaba <b>{cfg.youtube_handle}</b> y autorizaste "
                f"<b>{info.get('handle')}</b>. Si no es el correcto, repite y elige el otro canal.</p>")
    return _page("Canal autorizado",
                 f"<p><b>{cfg.name}</b> quedó conectado a <b>{info['title']}</b> {info.get('handle', '')}.</p>{warn}")


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
