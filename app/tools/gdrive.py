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
        return {"connected": bool(get_secret("drive", "google")), "email": acc.get("emailAddress", ""),
                "folder": settings().GAMING_DRIVE_FOLDER}
    except Exception:
        return {"connected": False, "email": "", "folder": settings().GAMING_DRIVE_FOLDER}


def sync(cfg) -> list[Path]:
    """Descarga la partida más reciente aún no usada. Una por ciclo (el plan gratis de Render es chico)."""
    if settings().MODE == "dry" or cfg.production.kind != "gameplay":
        return []
    creds = _creds()
    if not creds:
        return []
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    name = settings().GAMING_DRIVE_FOLDER.replace("'", "\\'")
    folders = svc.files().list(q=f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false",
                               fields="files(id,name)", pageSize=5).execute().get("files", [])
    if not folders:
        add_activity(cfg.id, "humano", f"César: crea en tu Drive una carpeta llamada «{settings().GAMING_DRIVE_FOLDER}» y sube ahí tus partidas", kind="ask")
        return []
    fid = folders[0]["id"]
    files = svc.files().list(q=f"'{fid}' in parents and trashed=false and (mimeType contains 'video/')",
                             fields="files(id,name,size,createdTime,mimeType)", orderBy="createdTime desc", pageSize=50).execute().get("files", [])
    done = {d["id"] for d in store().list(cfg.id, "drive_imports")}
    pending = [f for f in files if f["id"] not in done]
    if not pending:
        return []
    f = pending[0]
    size = int(f.get("size") or 0)
    if size > MAX_BYTES:
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
    return [out]
