"""CAZATENDENCIAS: una vez al día mira qué está pegando AFUERA, en YouTube, en el nicho del canal.

- Busca los videos más vistos de los últimos 7 días en español (México primero) para varias búsquedas del nicho.
- Junta vistas, duración (short o largo), título y canal; calcula vistas por hora para no premiar solo a los
  canales gigantes.
- Una IA lo resume en un REPORTE: temas/héroes en alza, patrones de título que se repiten, qué formato gana
  (short o largo, duración) y 5-8 ideas concretas adaptadas al canal (sin copiar títulos).
- El reporte lo leen el Investigador, el Director y el Investigador de héroes (prioriza héroes en tendencia).
Cuota de YouTube: ~100 unidades por búsqueda (de 10 000 diarias); 3 búsquedas al día por canal.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from ..config import ChannelConfig, settings
from ..db import add_activity, store
from ..llm import ask_json
from ..tools import youtube

log = logging.getLogger("trends")

QUERIES = {
    "gaming": ["mobile legends", "mlbb español", "magic chess go go"],
    "lcmc": ["refrigerador no enfría", "lavadora reparación", "minisplit falla"],
}
EVERY_HOURS = 20


class TrendIdea(BaseModel):
    title_idea: str        # idea propia (no copiar el título de otro)
    format: str            # short | long
    why: str               # qué dato del reporte la respalda


class TrendReport(BaseModel):
    rising: list[str]          # temas / héroes / problemas en alza
    title_patterns: list[str]  # patrones que se repiten en los títulos que más jalan
    winning_format: str        # qué gana: short vs largo, duración, estilo
    ideas: list[TrendIdea]
    summary: str


SYSTEM = """Eres el CAZATENDENCIAS de un canal de YouTube. Recibes los videos más vistos de la ÚLTIMA SEMANA en el
nicho (con vistas por hora, duración y canal). Detecta lo que está en alza de verdad (no lo de siempre), los
patrones de título que se repiten en los que más jalan, y qué formato gana. Luego propone 5-8 ideas PROPIAS para
este canal (no copies títulos; adapta el ángulo a lo que el canal puede hacer honestamente). Sé concreto: nombres
de héroes, problemas, números."""


def _iso_dur(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0) if m else 0


def fetch(cfg: ChannelConfig) -> list[dict]:
    yt = youtube.data_api(cfg.id)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: dict[str, str] = {}
    for q in QUERIES.get(cfg.id, [cfg.niche[:40]]):
        r = yt.search().list(part="id", q=q, type="video", order="viewCount", publishedAfter=since,
                             regionCode="MX", relevanceLanguage="es", maxResults=20).execute()
        for it in r.get("items", []):
            ids.setdefault(it["id"]["videoId"], q)
    out = []
    keys = list(ids)
    for k in range(0, len(keys), 50):
        r = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(keys[k:k + 50])).execute()
        for v in r.get("items", []):
            pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
            hours = max(1.0, (datetime.now(timezone.utc) - pub).total_seconds() / 3600)
            views = int(v.get("statistics", {}).get("viewCount", 0))
            secs = _iso_dur(v.get("contentDetails", {}).get("duration", ""))
            out.append({"id": v["id"], "title": v["snippet"]["title"], "channel": v["snippet"]["channelTitle"],
                        "views": views, "vph": round(views / hours), "secs": secs,
                        "format": "short" if secs and secs <= 70 else "long", "query": ids[v["id"]],
                        "lang": v["snippet"].get("defaultAudioLanguage", "")})
    return sorted(out, key=lambda x: -x["vph"])


def latest(channel_id: str) -> dict | None:
    return store().get(channel_id, "trends", "latest")


def block(channel_id: str) -> str:
    """Texto para Investigador/Director."""
    t = latest(channel_id)
    if not t:
        return "TENDENCIAS EN YOUTUBE: aún sin reporte."
    ideas = "\n".join(f"  - [{i['format']}] {i['title_idea']} ({i['why'][:90]})" for i in t.get("ideas", [])[:8])
    return (f"TENDENCIAS EN YOUTUBE (últimos 7 días, {t.get('at', '')[:10]}):\n"
            f"- En alza: {', '.join(t.get('rising', [])[:10])}\n"
            f"- Patrones de título: {'; '.join(t.get('title_patterns', [])[:5])}\n"
            f"- Formato que gana: {t.get('winning_format', '')}\n- Ideas sugeridas:\n{ideas}")


def run(cfg: ChannelConfig, force: bool = False) -> dict | None:
    if settings().MODE == "dry":
        return None
    old = latest(cfg.id)
    if old and not force:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(old["at"])
            if age < timedelta(hours=EVERY_HOURS):
                return old
        except Exception:
            pass
    add_activity(cfg.id, "investigador", "Cazando tendencias: lo más visto de la semana en YouTube…")
    try:
        vids = fetch(cfg)
    except Exception as e:
        log.warning("[%s] sin tendencias de YouTube (%s)", cfg.id, e)
        add_activity(cfg.id, "investigador", f"No pude leer tendencias de YouTube: {str(e)[:80]}", kind="bad")
        return old
    if not vids:
        return old
    shorts = [v for v in vids if v["format"] == "short"]
    longs = [v for v in vids if v["format"] == "long"]
    table = "\n".join(f"- {v['vph']} vistas/h · {v['views']} vistas · {v['format']} {v['secs']}s · «{v['title'][:90]}» "
                      f"({v['channel'][:30]})" for v in vids[:45])
    user = (f"CANAL: {cfg.name} — {cfg.niche}\nAUDIENCIA: {cfg.audience}\n"
            f"Shorts en la muestra: {len(shorts)} (mediana vph {_median([v['vph'] for v in shorts])}) · "
            f"Largos: {len(longs)} (mediana vph {_median([v['vph'] for v in longs])})\n"
            f"VIDEOS MÁS VISTOS DE LA SEMANA (ordenados por vistas por hora):\n{table}")
    try:
        r = ask_json(SYSTEM, user, TrendReport, fallback=TrendReport(rising=[], title_patterns=[], winning_format="",
                                                                   ideas=[], summary="sin análisis"))
    except Exception as e:
        log.warning("[%s] no pude resumir tendencias (%s)", cfg.id, e)
        return old
    rep = {**r.model_dump(), "top": vids[:15], "at": datetime.now(timezone.utc).isoformat()}
    store().put(cfg.id, "trends", "latest", rep)
    add_activity(cfg.id, "investigador", "Tendencias: " + (", ".join(r.rising[:4]) or r.summary[:80]), to="director", kind="ok")
    return rep


def _median(xs: list[int]) -> int:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def trending_names(channel_id: str) -> list[str]:
    t = latest(channel_id) or {}
    return [x.lower() for x in t.get("rising", [])]
