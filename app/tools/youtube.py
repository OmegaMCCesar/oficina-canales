"""YouTube Data API v3 + YouTube Analytics API.

Un token OAuth por canal en data/yt_token_<canal>.json (ver scripts/auth_youtube.py).
En MODE=dry todo se simula.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DATA_DIR, settings
from ..models import Metrics

log = logging.getLogger("youtube")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",   # leer comentarios y publicar el comentario del canal
]


def token_path(channel_id: str) -> Path:
    return DATA_DIR / f"yt_token_{channel_id}.json"


def _creds(channel_id: str):
    import json as _json

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from ..db import get_secret, save_secret

    stored = get_secret(channel_id, "youtube")
    if stored:
        creds = Credentials.from_authorized_user_info(stored, stored.get("scopes") or SCOPES)
    else:
        p = token_path(channel_id)
        if not p.exists():
            raise RuntimeError(f"Canal '{channel_id}' sin autorizar. Abre /oauth/start/{channel_id} desde el panel.")
        creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_secret(channel_id, "youtube", _json.loads(creds.to_json()))
    return creds


def is_authorized(channel_id: str) -> bool:
    from ..db import get_secret
    try:
        return bool(get_secret(channel_id, "youtube")) or token_path(channel_id).exists()
    except Exception:
        return False


# ---------------- OAuth web (Render) ----------------

def _web_flow(state: str | None = None, scopes: list[str] | None = None):
    from google_auth_oauthlib.flow import Flow
    s = settings()
    cfg = {"web": {
        "client_id": s.YOUTUBE_CLIENT_ID, "client_secret": s.YOUTUBE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [s.PUBLIC_URL.rstrip("/") + "/oauth/callback"],
    }}
    # Cliente web con secreto: sin PKCE (el verificador no sobreviviría entre /oauth/start y /oauth/callback)
    flow = Flow.from_client_config(cfg, scopes=scopes or SCOPES, state=state, autogenerate_code_verifier=False)
    flow.redirect_uri = s.PUBLIC_URL.rstrip("/") + "/oauth/callback"
    return flow


def auth_url(state: str) -> str:
    flow = _web_flow(state)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                    include_granted_scopes="true")
    return url


def finish_auth(channel_id: str, state: str, full_callback_url: str) -> dict:
    """Intercambia el código por tokens, los guarda y devuelve el canal autorizado."""
    import json as _json
    import os
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    from googleapiclient.discovery import build

    from ..db import save_secret

    flow = _web_flow(state)
    flow.fetch_token(authorization_response=full_callback_url.replace("http://", "https://", 1))
    creds = flow.credentials
    save_secret(channel_id, "youtube", _json.loads(creds.to_json()))
    yt = build("youtube", "v3", credentials=creds)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        return {"title": "(esta cuenta no tiene canal)", "id": "", "handle": ""}
    ch = items[0]
    info = {"title": ch["snippet"]["title"], "id": ch["id"], "handle": ch["snippet"].get("customUrl", "")}
    save_secret(channel_id, "youtube_channel", info)
    return info


def data_api(channel_id: str):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_creds(channel_id))


def analytics_api(channel_id: str):
    from googleapiclient.discovery import build
    return build("youtubeAnalytics", "v2", credentials=_creds(channel_id))


# ---------------- Publicar ----------------

def upload_video(channel_id: str, video_path: str, title: str, description: str,
                 tags: list[str], publish_at: datetime | None, thumbnail_path: str | None,
                 is_short: bool) -> str:
    """Sube como privado y programa publicación. Devuelve video_id."""
    if settings().MODE == "dry":
        vid = "dry_" + hex(random.getrandbits(32))[2:]
        log.info("[dry] upload %s -> %s (publish_at=%s)", title, vid, publish_at)
        return vid

    from googleapiclient.http import MediaFileUpload

    yt = data_api(channel_id)
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at:   # el Publicador solo lo manda si César aprobó (o el modo es automático)
        status["publishAt"] = publish_at.astimezone().isoformat()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": "26" if channel_id == "lcmc" else "20",  # Howto / Gaming
            "defaultLanguage": "es",
        },
        "status": status,
    }
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    if thumbnail_path:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumbnail_path)).execute()
        except Exception as e:  # cuenta sin verificar no puede poner miniatura
            log.warning("thumbnail falló: %s", e)
    return vid


def post_comment(channel_id: str, video_id: str, text: str) -> bool:
    """Comentario del propio canal en su video (fíjalo a mano en Studio: la API no permite fijar).
    Solo funciona cuando el video ya es público; devuelve True si se publicó."""
    if settings().MODE == "dry":
        return True
    try:
        data_api(channel_id).commentThreads().insert(part="snippet", body={"snippet": {
            "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text[:900]}}}}).execute()
        return True
    except Exception as e:  # video aún privado o sin permiso force-ssl: se reintenta en el siguiente ciclo
        log.info("comentario pendiente (%s)", str(e)[:120])
        return False


# ---------------- Leer ----------------

def recent_videos(channel_id: str, max_results: int = 30) -> list[dict]:
    """Últimos videos del canal: [{id, title, published_at}]"""
    if settings().MODE == "dry":
        return []
    yt = data_api(channel_id)
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=max_results).execute()
    return [
        {
            "id": it["snippet"]["resourceId"]["videoId"],
            "title": it["snippet"]["title"],
            "published_at": it["snippet"]["publishedAt"],
        }
        for it in items.get("items", [])
    ]


def recent_comments(channel_id: str, max_results: int = 50) -> list[str]:
    if settings().MODE == "dry":
        return [
            "Mi refri Samsung hace ruido y no enfría abajo, ¿qué puede ser?",
            "Haz uno de lavadoras LG que no desaguan",
            "Explica cómo medir el capacitor del minisplit",
        ] if channel_id == "lcmc" else [
            "Sube más de Magic Chess, la compo de magos está rota",
            "Qué build usas con Lancelot?",
        ]
    try:
        yt = data_api(channel_id)
        r = yt.commentThreads().list(part="snippet", allThreadsRelatedToChannelId=channel_id_of(channel_id),
                                     maxResults=max_results, order="time", textFormat="plainText").execute()
        return [t["snippet"]["topLevelComment"]["snippet"]["textDisplay"] for t in r.get("items", [])]
    except Exception as e:  # sin permiso de comentarios o canal sin comentarios: seguir sin ellos
        log.warning("[%s] no pude leer comentarios: %s", channel_id, str(e)[:160])
        return []


def channel_id_of(channel_id: str) -> str:
    from ..config import get_channel
    from ..db import get_secret
    cid = get_channel(channel_id).youtube_channel_id or (get_secret(channel_id, "youtube_channel") or {}).get("id", "")
    if not cid:
        yt = data_api(channel_id)
        cid = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    return cid


def video_metrics(channel_id: str, video_id: str, window: str, title: str = "") -> Metrics:
    """Métricas de un video en ventana 24h/7d/30d."""
    if settings().MODE == "dry":
        base = random.randint(50, 3000)
        return Metrics(
            youtube_video_id=video_id, channel_id=channel_id, title=title, window=window,
            views=base, likes=int(base * 0.06), comments=int(base * 0.01),
            impressions=base * 8, ctr=round(random.uniform(2, 9), 2),
            avg_view_pct=round(random.uniform(30, 75), 1), avg_view_seconds=random.uniform(10, 50),
            subs_gained=int(base * 0.005),
        )
    days = {"24h": 1, "7d": 7, "30d": 30}[window]
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    an = analytics_api(channel_id)
    r = an.reports().query(
        ids="channel==MINE", startDate=str(start), endDate=str(end),
        metrics="views,likes,comments,averageViewPercentage,averageViewDuration,subscribersGained",
        filters=f"video=={video_id}",
    ).execute()
    row = (r.get("rows") or [[0] * 6])[0]
    # impressions/CTR están en el reporte de 'card'/'impressions' solo vía Studio; se dejan 0 si no vienen
    return Metrics(
        youtube_video_id=video_id, channel_id=channel_id, title=title, window=window,
        views=int(row[0]), likes=int(row[1]), comments=int(row[2]),
        avg_view_pct=float(row[3]), avg_view_seconds=float(row[4]), subs_gained=int(row[5]),
    )


# ---------------- Series y monetización ----------------

def add_to_playlist(channel_id: str, playlist_id: str, title: str, description: str, video_id: str) -> str:
    """Agrega el video a la playlist de la serie (la crea pública si no existe). Devuelve el id."""
    if settings().MODE == "dry":
        return playlist_id or "dry_pl_" + hex(random.getrandbits(24))[2:]
    yt = data_api(channel_id)
    if not playlist_id:
        pl = yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title[:150], "description": description[:4900], "defaultLanguage": "es"},
            "status": {"privacyStatus": "public"}}).execute()
        playlist_id = pl["id"]
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    return playlist_id


def channel_progress(channel_id: str) -> dict:
    """Números para monetizar: suscriptores, horas de videos largos (365 días) y vistas de Shorts (90 días)."""
    if settings().MODE == "dry":
        return {"subs": random.randint(20, 400), "long_hours_365": round(random.uniform(10, 900), 1),
                "shorts_views_90": random.randint(1000, 300000), "public_videos": random.randint(3, 40), "source": "dry"}
    yt = data_api(channel_id)
    st = yt.channels().list(part="statistics", mine=True).execute()["items"][0]["statistics"]
    out = {"subs": int(st.get("subscriberCount", 0)), "public_videos": int(st.get("videoCount", 0)),
           "long_hours_365": None, "shorts_views_90": None, "source": "youtube"}
    an = analytics_api(channel_id)
    end = datetime.utcnow().date()
    try:
        r = an.reports().query(ids="channel==MINE", startDate=str(end - timedelta(days=365)), endDate=str(end),
                               metrics="estimatedMinutesWatched", dimensions="creatorContentType").execute()
        rows = {row[0]: row[1] for row in r.get("rows", [])}
        long_min = sum(v for k, v in rows.items() if str(k).upper() not in ("SHORTS",))
        out["long_hours_365"] = round(long_min / 60, 1)
        r2 = an.reports().query(ids="channel==MINE", startDate=str(end - timedelta(days=90)), endDate=str(end),
                                metrics="views", dimensions="creatorContentType").execute()
        out["shorts_views_90"] = int(sum(row[1] for row in r2.get("rows", []) if str(row[0]).upper() == "SHORTS"))
    except Exception as e:   # si la dimensión no está disponible, al menos el total de minutos
        log.info("progreso sin desglose por tipo (%s)", str(e)[:120])
        r = an.reports().query(ids="channel==MINE", startDate=str(end - timedelta(days=365)), endDate=str(end),
                               metrics="estimatedMinutesWatched").execute()
        out["long_hours_365"] = round(((r.get("rows") or [[0]])[0][0]) / 60, 1)
        out["note"] = "Horas totales (sin separar Shorts); YouTube cuenta solo las de videos largos"
    return out


# ---------------- Calendario: estado real y hora de publicación ----------------

def video_status(channel_id: str, ids: list[str]) -> dict:
    """{id: {"privacy": public|private|unlisted, "publish_at": iso|None, "published_at": iso|None}}"""
    if not ids:
        return {}
    if settings().MODE == "dry":
        return {i: {"privacy": "private", "publish_at": None, "published_at": None} for i in ids}
    yt = data_api(channel_id)
    out = {}
    for k in range(0, len(ids), 50):
        r = yt.videos().list(part="status,snippet", id=",".join(ids[k:k + 50])).execute()
        for it in r.get("items", []):
            st = it.get("status", {})
            out[it["id"]] = {"privacy": st.get("privacyStatus"), "publish_at": st.get("publishAt"),
                             "published_at": it.get("snippet", {}).get("publishedAt")}
    return out


def set_publish(channel_id: str, video_id: str, when: datetime | None) -> str:
    """Programa la publicación en YouTube (privado + publishAt). Si la hora ya pasó, lo hace público ya."""
    if settings().MODE == "dry":
        return "dry"
    yt = data_api(channel_id)
    now = datetime.now(when.tzinfo if when else None) if when else None
    if when and when > now + timedelta(minutes=10):
        status = {"privacyStatus": "private", "publishAt": when.astimezone().isoformat(), "selfDeclaredMadeForKids": False}
        res = "programado"
    else:
        status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
        res = "publicado"
    yt.videos().update(part="status", body={"id": video_id, "status": status}).execute()
    return res


def apply_branding(channel_id: str, description: str, keywords: str, banner: Path | None) -> dict:
    """Pone la descripción, las palabras clave y la portada del canal. La foto de perfil NO se puede cambiar por API
    (YouTube no lo permite): esa la sube César en Studio → Personalización."""
    from googleapiclient.http import MediaFileUpload
    yt = data_api(channel_id)
    me = yt.channels().list(part="brandingSettings", mine=True).execute()["items"][0]
    bs = me.get("brandingSettings", {})
    bs.setdefault("channel", {})
    bs["channel"]["description"] = description[:1000]
    bs["channel"]["keywords"] = keywords[:500]
    done = ["descripción", "palabras clave"]
    if banner and banner.exists():
        url = yt.channelBanners().insert(media_body=MediaFileUpload(str(banner), mimetype="image/png")).execute()["url"]
        bs.setdefault("image", {})["bannerExternalUrl"] = url
        done.append("portada")
    bs.pop("hints", None)
    yt.channels().update(part="brandingSettings", body={"id": me["id"], "brandingSettings": bs}).execute()
    return {"applied": done}
