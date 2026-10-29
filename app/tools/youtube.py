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
]


def token_path(channel_id: str) -> Path:
    return DATA_DIR / f"yt_token_{channel_id}.json"


def _creds(channel_id: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    p = token_path(channel_id)
    if not p.exists():
        raise RuntimeError(f"Sin token OAuth para {channel_id}. Corre: python scripts/auth_youtube.py {channel_id}")
    creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return creds


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
    if publish_at:
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
    yt = data_api(channel_id)
    r = yt.commentThreads().list(part="snippet", allThreadsRelatedToChannelId=channel_id_of(channel_id),
                                 maxResults=max_results, order="time", textFormat="plainText").execute()
    return [t["snippet"]["topLevelComment"]["snippet"]["textDisplay"] for t in r.get("items", [])]


def channel_id_of(channel_id: str) -> str:
    from ..config import get_channel
    cid = get_channel(channel_id).youtube_channel_id
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
