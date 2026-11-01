"""Bandeja de partidas en Google Drive (LCMC Games).

César graba en el celular y sube el video a una carpeta de su Drive (app de Google Drive → Subir).
Cada ciclo, la oficina revisa esa carpeta, descarga la partida más reciente que no haya usado y la deja
en la bandeja de gaming: el Investigador propone ideas con ella y el Guionista (Gemini, que ve video)
marca los highlights para cortarlos.

Se autoriza aparte de YouTube (los canales son cuentas de marca y esas no tienen Drive), solo lectura.
YouTube no permite descargar videos por API, por eso Drive es el camino.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..db import add_activity, get_secret, save_secret, store

log = logging.getLogger("gdrive")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "openid", "https://www.googleapis.com/auth/userinfo.email"]
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".3gp")
MAX_BYTES = 2_500_000_000


def auth_url(state: str) -> str:
    from .youtube import _web_flow
    flow = _web_flow(state, SCOPES)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account")
    return url


def finish_auth(state: str, full_callback_url: str) -> dict:
    import os
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    from googleapiclient.discovery import build

    from .youtube import _web_flow
    flow = _web_flow(state, SCOPES)
    flow.fetch_token(authorization_response=full_callback_url.replace("http://", "https://", 1))
    creds = flow.credentials
    save_secret("drive", "google", json.loads(creds.to_json()))
    about = build("drive", "v3", credentials=creds).about().get(fields="user(emailAddress,displayName)").execute()
    info = about.get("user", {})
    save_secret("drive", "account", info)
    return info


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    stored = get_secret("drive", "google")
    if not stored:
        return None
    creds = Credentials.from_authorized_user_info(stored, stored.get("scopes") or SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_secret("drive", "google", json.loads(creds.to_json()))
    return creds


def status() -> dict:
    try:
        acc = get_secret("drive", "account") or {}
        last = store().get("gaming", "drive_status", "last") or {}
        return {"connected": bool(get_secret("drive", "google")), "email": acc.get("emailAddress", ""),
                "folder": settings().GAMING_DRIVE_FOLDER, "last": last.get("msg", ""), "last_at": last.get("at", "")}
    except Exception:
        return {"connected": False, "email": "", "folder": settings().GAMING_DRIVE_FOLDER}


def _note(cfg, msg: str, **extra) -> None:
    """Deja constancia (log + Firestore) de qué pasó en la última revisión de Drive."""
    log.info("[%s] drive: %s %s", cfg.id, msg, extra or "")
    try:
        store().put(cfg.id, "drive_status", "last", {"msg": msg, **extra, "at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def sync(cfg) -> list[Path]:
    """Descarga la partida más reciente aún no usada. Una por ciclo (el plan gratis de Render es chico)."""
    if settings().MODE == "dry" or cfg.production.kind != "gameplay":
        return []
    creds = _creds()
    if not creds:
        _note(cfg, "Drive no está conectado")
        return []
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    name = settings().GAMING_DRIVE_FOLDER.replace("'", "\\'")
    folders = svc.files().list(q=f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false",
                               fields="files(id,name)", pageSize=5).execute().get("files", [])
    if not folders:
        _note(cfg, f"no encontré la carpeta «{settings().GAMING_DRIVE_FOLDER}»")
        add_activity(cfg.id, "humano", f"César: crea en tu Drive una carpeta llamada «{settings().GAMING_DRIVE_FOLDER}» y sube ahí tus partidas", kind="ask")
        return []
    fid = folders[0]["id"]
    files = svc.files().list(q=f"'{fid}' in parents and trashed=false and (mimeType contains 'video/')",
                             fields="files(id,name,size,createdTime,mimeType)", orderBy="createdTime desc", pageSize=50).execute().get("files", [])
    from ..db import jobs
    used = {j.idea.source_media for j in jobs(cfg.id) if j.idea.source_media}
    needed = {j.idea.source_media for j in jobs(cfg.id) if j.idea.source_media and j.status in ("approved", "scripted")}
    done = set()
    for d in store().list(cfg.id, "drive_imports"):
        p = d.get("path")
        # el disco de Render se borra en cada reinicio/deploy: si la partida se bajó pero ningún video la usó
        # y el archivo ya no está, se vuelve a bajar
        if d.get("status") == "descargada" and p and not Path(p).exists() and (p not in used or p in needed):
            continue
        # "muy grande" se reintenta si César reemplazó el archivo por uno más chico (mismo id, otro tamaño)
        if d.get("status") == "muy grande" and int(next((x.get("size") or 0 for x in files if x["id"] == d["id"]), 0)) != d.get("size"):
            continue
        done.add(d["id"])
    pending = [f for f in files if f["id"] not in done]
    log.info("[%s] drive: carpeta con %d video(s): %s", cfg.id, len(files),
             [(x["name"], int(x.get("size") or 0) // 1_000_000) for x in files])
    if not pending:
        big = [x["name"] for x in files if int(x.get("size") or 0) > MAX_BYTES]
        _note(cfg, f"{len(files)} video(s) en la carpeta, todos ya usados" +
              (f"; muy grandes para bajar (máx 2.5 GB): {', '.join(big)}" if big else ""), files=len(files))
        return []
    fit = [x for x in pending if int(x.get("size") or 0) <= MAX_BYTES]
    f = fit[0] if fit else pending[0]
    size = int(f.get("size") or 0)
    if size > MAX_BYTES:
        _note(cfg, f"«{f['name']}» es muy grande ({size // 1_000_000} MB)")
        store().put(cfg.id, "drive_imports", f["id"], {"id": f["id"], "name": f["name"], "status": "muy grande", "size": size})
        add_activity(cfg.id, "humano", f"«{f['name']}» pesa {size // 1_000_000} MB; súbela recortada (máx 2.5 GB)", kind="ask")
        return []
    inbox = Path(settings().GAMING_INBOX)
    inbox.mkdir(parents=True, exist_ok=True)
    ext = Path(f["name"]).suffix.lower() if Path(f["name"]).suffix.lower() in VIDEO_EXT else ".mp4"
    out = inbox / f"drive_{f['id'][:10]}{ext}"
    add_activity(cfg.id, "humano", f"Bajando tu partida de Drive: {f['name']} ({size // 1_000_000} MB)…")
    with open(out, "wb") as fh:
        dl = MediaIoBaseDownload(fh, svc.files().get_media(fileId=f["id"]), chunksize=16 * 1024 * 1024)
        fin = False
        while not fin:
            _, fin = dl.next_chunk()
    store().put(cfg.id, "drive_imports", f["id"], {"id": f["id"], "name": f["name"], "status": "descargada", "size": size,
                                                  "path": str(out), "at": datetime.now(timezone.utc).isoformat()})
    add_activity(cfg.id, "humano", f"Partida lista para editar: {f['name']}", to="investigador", kind="ok")
    _note(cfg, f"descargué «{f['name']}» ({size // 1_000_000} MB)", pending=len(pending) - 1)
    return [out]
