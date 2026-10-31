"""API de la oficina. Para Render: uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
from __future__ import annotations

import logging
import os
import secrets

from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import orchestrator
from .config import load_channels, settings
from .db import add_activity, insights, jobs, metrics, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
from .memory import limit_arenas  # noqa: E402
limit_arenas()
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


@app.post("/providers/test", dependencies=[Depends(auth)])
def providers_test():
    """Prueba real de cada proveedor con una llamada mínima (texto, imagen FLUX, voz Edge)."""
    import logging as _l
    import tempfile
    from pathlib import Path as _P

    from pydantic import BaseModel as _BM

    from .providers import key, text, visuals, voice

    class _Ping(_BM):
        ok: bool
        palabra: str

    res: dict = {}
    for name, env, base, model in text.OPENAI_COMPAT:
        if key(env) and name not in res:
            try:
                o = text._openai_compat(name, env, base, model, "Responde en JSON.", "Di ok=true y una palabra en español.", _Ping)
                res[name] = f"ok ({o.palabra})"
            except Exception as e:
                res[name] = f"error: {str(e)[:160]}"
    if key("GEMINI_API_KEY"):
        try:
            res["gemini"] = "ok (" + text._gemini("Responde en JSON.", "Di ok=true y una palabra.", _Ping, None).palabra + ")"
        except Exception as e:
            res["gemini"] = f"error: {str(e)[:160]}"
    if key("CLOUDFLARE_API_TOKEN"):
        res["cloudflare_account"] = key("CLOUDFLARE_ACCOUNT_ID") or "no encontrado"
        try:
            tmp = _P(tempfile.mkdtemp()) / "flux.jpg"
            visuals._cloudflare_flux("a refrigerator in a bright kitchen, photo", tmp)
            res["flux"] = f"ok ({tmp.stat().st_size // 1024} KB)"
        except Exception as e:
            res["flux"] = f"error: {str(e)[:160]}"
    try:
        tmp = _P(tempfile.mkdtemp()) / "v.mp3"
        _, w = voice._edge("Hola, soy Lumi.", tmp, voice.EDGE_VOICES["male"], "+8%"), None
        res["edge-tts"] = f"ok ({tmp.stat().st_size // 1024} KB)"
    except Exception as e:
        res["edge-tts"] = f"error: {str(e)[:160]}"
    _l.getLogger("providers").info("PRUEBA DE PROVEEDORES: %s", res)
    return res


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


@app.get("/oauth/drive", include_in_schema=False)
def oauth_drive(t: str = ""):
    """Conecta Google Drive (solo lectura) para la bandeja de partidas de LCMC Games."""
    if not secrets.compare_digest(t, settings().API_TOKEN):
        return _page("Token inválido", "<p>Abre esto desde Acciones en la oficina.</p>")
    from .tools import gdrive
    return RedirectResponse(gdrive.auth_url(f"drive.{_sign('drive')}"))


@app.get("/oauth/callback", include_in_schema=False)
def oauth_callback(request: Request, state: str = "", error: str = ""):
    if error:
        return _page("Autorización cancelada", f"<p>Google respondió: {error}</p>")
    channel_id, _, sig = state.partition(".")
    if not channel_id or not secrets.compare_digest(sig, _sign(channel_id)):
        return _page("Solicitud inválida", "<p>Vuelve a empezar desde la oficina.</p>")
    if channel_id == "drive":
        from .tools import gdrive
        try:
            info = gdrive.finish_auth(state, str(request.url))
        except Exception as e:
            logging.exception("oauth drive")
            return _page("No se pudo conectar Drive", f"<p>{str(e)[:300]}</p>")
        return _page("Google Drive conectado",
                     f"<p>Listo: la oficina puede leer la carpeta <b>{settings().GAMING_DRIVE_FOLDER}</b> de "
                     f"<b>{info.get('emailAddress', 'tu cuenta')}</b>. Sube ahí tus partidas desde el celular.</p>")
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


@app.get("/channels/{channel_id}/office", dependencies=[Depends(auth)])
def office(channel_id: str):
    """Todo lo de los puestos nuevos en una sola llamada: monetización, bandeja de César, series y calidad."""
    from .agents import human_touch, monetization, showrunner
    prog = store().get(channel_id, "money", "progress")
    q = sorted(store().list(channel_id, "quality"), key=lambda d: d.get("at", ""), reverse=True)[:5]
    return {
        "progress": prog, "thresholds": monetization.thresholds(),
        "pending_products": monetization.pending(channel_id)[:10],
        "links": len(monetization.links(channel_id)),
        "amazon_tag": bool(settings().AMAZON_ASSOCIATE_TAG),
        "requests": human_touch.requests(channel_id)[:6],
        "assets": human_touch.assets(channel_id)[:10],
        "series": [{"id": s["id"], "title": s["title"], "playlist_id": s.get("playlist_id", ""),
                    "episodes": [{"n": e["n"], "title": e["title"], "status": e["status"]} for e in s["episodes"]]}
                   for s in showrunner.series(channel_id)],
        "quality": q,
        "drive": __import__("app.tools.gdrive", fromlist=["status"]).status() if channel_id == "gaming" else None,
        "reviews": sorted(store().list(channel_id, "reviews"), key=lambda d: d.get("at", ""), reverse=True)[:3],
    }


@app.post("/channels/{channel_id}/human/assets", dependencies=[Depends(auth)])
def human_upload(channel_id: str, body: dict):
    """César sube una foto o nota de voz (base64) desde el panel."""
    import base64
    import binascii

    from .agents import human_touch
    b64 = str(body.get("b64", ""))
    if len(b64) > 1_000_000:
        raise HTTPException(413, "Archivo muy grande (máx 700 KB)")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Archivo inválido")
    try:
        return human_touch.save_asset(channel_id, str(body.get("kind", "")), str(body.get("mime", "")), raw,
                                      str(body.get("note", "")), str(body.get("request_id", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/channels/{channel_id}/money/links", dependencies=[Depends(auth)])
def money_link(channel_id: str, body: dict):
    """César pega el link de afiliado de un producto pendiente (Mercado Libre, Amazon u otra tienda)."""
    from urllib.parse import urlparse

    from .agents import monetization
    product, url = str(body.get("product", "")).strip()[:120], str(body.get("url", "")).strip()[:500]
    u = urlparse(url)
    if not product or u.scheme != "https" or not u.netloc:
        raise HTTPException(400, "Pon el nombre del producto y un link que empiece con https://")
    return monetization.save_link(channel_id, product, url)


BRAND_DIR = Path(__file__).parent / "static" / "brand"


@app.get("/channels/{channel_id}/brand", dependencies=[Depends(auth)])
def brand(channel_id: str):
    """Identidad del canal: logo, portada, descripción y palabras clave (y si ya se aplicó en YouTube)."""
    from .config import get_channel
    cfg = get_channel(channel_id)
    d = BRAND_DIR / channel_id
    return {**cfg.branding, "logo": (d / "logo.png").exists(), "banner": (d / "banner.png").exists(),
            "applied": store().get(channel_id, "brand", "applied")}


@app.get("/brand/{channel_id}/{name}", include_in_schema=False)
def brand_file(channel_id: str, name: str):
    from fastapi.responses import FileResponse
    if name not in ("logo.png", "banner.png") or not channel_id.isalnum():
        raise HTTPException(404)
    f = BRAND_DIR / channel_id / name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/png", filename=f"{channel_id}-{name}")


@app.post("/channels/{channel_id}/brand/apply", dependencies=[Depends(auth)])
def brand_apply(channel_id: str):
    """César aprueba en el panel y la oficina pone descripción, palabras clave y portada en YouTube."""
    from datetime import datetime, timezone

    from .config import get_channel
    from .tools import youtube
    cfg = get_channel(channel_id)
    if not cfg.branding.get("description"):
        raise HTTPException(400, "Este canal todavía no tiene identidad preparada")
    try:
        r = youtube.apply_branding(channel_id, cfg.branding["description"], cfg.branding.get("keywords", ""),
                                   BRAND_DIR / channel_id / "banner.png")
    except Exception as e:
        raise HTTPException(502, f"YouTube no aceptó el cambio: {str(e)[:200]}")
    store().put(channel_id, "brand", "applied", {**r, "at": datetime.now(timezone.utc).isoformat()})
    add_activity(channel_id, "seo", "Actualicé la página del canal: " + ", ".join(r["applied"]), kind="ok")
    return r


@app.get("/channels/{channel_id}/recordings", dependencies=[Depends(auth)])
def recordings(channel_id: str):
    """Partidas de César con lo que vio el Analista (y lo que César confirmó)."""
    docs = sorted(store().list(channel_id, "recordings"), key=lambda d: d.get("at", ""), reverse=True)[:8]
    for d in docs:
        d["key"] = __import__("app.agents.scout", fromlist=["key_for"]).key_for(d["path"])
        d["videos"] = [{"title": j.script.title if j.script else j.idea.title, "status": j.status}
                       for j in jobs(channel_id) if j.idea.source_media == d["path"]]
    return docs


@app.post("/channels/{channel_id}/recordings/{key}", dependencies=[Depends(auth)])
def recording_confirm(channel_id: str, key: str, body: dict):
    """César confirma héroe, resultado y KDA de su partida: con eso ya se puede titular honestamente."""
    d = store().get(channel_id, "recordings", key)
    if not d:
        raise HTTPException(404, "No encontré esa partida")
    res = str(body.get("result", "")).strip().lower()
    c = {"hero": str(body.get("hero", "")).strip()[:40], "result": res if res in ("victoria", "derrota") else "",
         "kda": str(body.get("kda", "")).strip()[:12], "note": str(body.get("note", "")).strip()[:200]}
    d["cesar"] = {k: v for k, v in c.items() if v}
    store().put(channel_id, "recordings", key, d)
    add_activity(channel_id, "investigador", f"César confirmó su partida: {c['hero'] or '?'} · {c['result'] or '?'}"
                 + (f" · {c['kda']}" if c["kda"] else "") + ". En el siguiente ciclo propongo los videos.", to="director", kind="ok")
    return {"ok": True}


@app.get("/usage", dependencies=[Depends(auth)])
def usage():
    """Gasto estimado en Claude (hoy y en el mes)."""
    from .providers import claude
    return claude.usage_summary()


@app.get("/channels/{channel_id}/calendar", dependencies=[Depends(auth)])
def calendar(channel_id: str):
    """Calendario del Director: qué video sale y cuándo, por qué, y si César ya lo aprobó."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    items = []
    for j in jobs(channel_id):
        if j.status not in ("thumbnailed", "scheduled", "published") or not j.scheduled_for:
            continue
        if j.status == "published" and (j.published_at or j.scheduled_for) < now - timedelta(days=3):
            continue
        items.append({"job_id": j.id, "title": j.script.title if j.script else j.idea.title, "format": j.idea.format,
                      "when": j.scheduled_for.isoformat(), "reason": j.schedule_reason, "approved": j.approved,
                      "status": j.status, "video_id": j.youtube_video_id,
                      "studio": f"https://studio.youtube.com/video/{j.youtube_video_id}/edit" if j.youtube_video_id else "",
                      "score": (j.qa.get("video_review") or {}).get("score")})
    items.sort(key=lambda x: x["when"])
    return {"items": items, "auto": settings().PUBLISH_MODE == "scheduled"}


def _apply_schedule(channel_id: str, j) -> str:
    from .db import save_job
    from .tools import youtube
    res = "aprobado (se programa al subirlo)"
    if j.youtube_video_id and j.status == "scheduled":
        res = youtube.set_publish(channel_id, j.youtube_video_id, j.scheduled_for)
    save_job(j)
    return res


@app.post("/channels/{channel_id}/calendar/{job_id}/approve", dependencies=[Depends(auth)])
def calendar_approve(channel_id: str, job_id: str):
    from .db import add_activity, get_job
    j = get_job(channel_id, job_id)
    if not j or not j.scheduled_for:
        raise HTTPException(404)
    j.approved = True
    try:
        res = _apply_schedule(channel_id, j)
    except Exception as e:
        j.approved = False
        raise HTTPException(502, f"YouTube no aceptó la programación: {str(e)[:160]}")
    add_activity(channel_id, "director", f"César aprobó: «{(j.script.title if j.script else j.idea.title)[:50]}» ({res})",
                 to="publicador", kind="ok")
    return {"ok": True, "result": res}


@app.post("/channels/{channel_id}/calendar/{job_id}/move", dependencies=[Depends(auth)])
def calendar_move(channel_id: str, job_id: str, body: dict):
    """César cambia la hora (hora de CDMX 'YYYY-MM-DDTHH:MM')."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from .db import get_job
    j = get_job(channel_id, job_id)
    if not j:
        raise HTTPException(404)
    try:
        when = datetime.strptime(str(body.get("when", ""))[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo("America/Mexico_City"))
    except ValueError:
        raise HTTPException(400, "Fecha inválida")
    if when < datetime.now(timezone.utc) - timedelta(minutes=1):
        raise HTTPException(400, "Esa hora ya pasó")
    j.scheduled_for, j.schedule_reason, j.approved = when.astimezone(timezone.utc), "Hora elegida por César", True
    try:
        res = _apply_schedule(channel_id, j)
    except Exception as e:
        raise HTTPException(502, f"YouTube no aceptó el cambio: {str(e)[:160]}")
    return {"ok": True, "result": res}


@app.get("/channels/{channel_id}/storyboards", dependencies=[Depends(auth)])
def storyboards(channel_id: str, limit: int = 12):
    """Storyboards del Animador con el veredicto del Director (sin la imagen)."""
    out = []
    for d in store().list(channel_id, "storyboards"):
        d = {k: v for k, v in d.items() if k != "jpeg_b64"}
        out.append(d)
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    from .db import get_job
    for d in out[:limit]:
        j = get_job(channel_id, d["job_id"])
        d["job_status"] = j.status if j else "?"
    return out[:limit]


@app.get("/channels/{channel_id}/storyboards/{job_id}.jpg", include_in_schema=False)
def storyboard_img(channel_id: str, job_id: str, t: str = ""):
    """Imagen del storyboard (token por query porque un <img> no manda headers)."""
    auth(t)
    import base64
    d = store().get(channel_id, "storyboards", job_id)
    if not d or not d.get("jpeg_b64"):
        raise HTTPException(404)
    return Response(base64.b64decode(d["jpeg_b64"]), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@app.post("/channels/{channel_id}/storyboards/{job_id}/approve", dependencies=[Depends(auth)])
def storyboard_approve(channel_id: str, job_id: str):
    d = store().get(channel_id, "storyboards", job_id)
    if not d:
        raise HTTPException(404)
    d["status"], d["cesar"] = "aprobado_por_cesar", "aprobado"
    store().put(channel_id, "storyboards", job_id, d)
    from .db import add_activity
    add_activity(channel_id, "director", "César aprobó la animación 👍", kind="ok")
    return {"ok": True}


@app.post("/channels/{channel_id}/storyboards/{job_id}/redo", dependencies=[Depends(auth)])
def storyboard_redo(channel_id: str, job_id: str, body: dict):
    """César pide rehacer la animación con notas: el video vuelve a producción antes de subir."""
    from .db import add_activity, get_job, save_job
    j = get_job(channel_id, job_id)
    if not j:
        raise HTTPException(404)
    if j.status in ("scheduled", "published"):
        raise HTTPException(409, "Ese video ya se subió a YouTube; bórralo en Studio y genera otro")
    j.status, j.anim_redo = "scripted", True
    j.anim_notes = str(body.get("notes", ""))[:500]
    j.video_path, j.thumbnail_paths, j.thumbnail_chosen = None, [], None
    save_job(j)
    d = store().get(channel_id, "storyboards", job_id) or {}
    if d:
        d["status"] = "rehacer"
        d["cesar_notes"] = j.anim_notes
        store().put(channel_id, "storyboards", job_id, d)
    add_activity(channel_id, "animador", f"Rehago la animación con tus notas: «{j.anim_notes[:60]}»", kind="ask")
    return {"ok": True, "msg": "Se rehace en el siguiente ciclo"}


# Scheduler interno (opcional). En Render es mejor un Cron Job que llame POST /run.
if os.getenv("ENABLE_SCHEDULER") == "1":
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="America/Mexico_City")
    sched.add_job(orchestrator.run_all, "cron", hour=6, minute=17)
    sched.start()
