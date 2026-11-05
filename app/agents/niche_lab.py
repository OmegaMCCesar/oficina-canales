"""LABORATORIO DE NICHOS: mide nichos candidatos con datos reales de YouTube (sin IA de por medio).

Para cada búsqueda: los videos más vistos de los últimos N días (region/idioma dados), y de sus canales:
cuántos canales distintos, cuántos son chicos (<50k subs), vistas medianas, cuántos videos son OUTLIERS
(vistas >= 3x el promedio de su canal) y cuántos de esos outliers son de canales chicos (= entrada posible).
Cuota: ~101 unidades por búsqueda + 1 por cada 50 videos/canales.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from ..tools import youtube
from .trends import _iso_dur


def measure(channel_id: str, queries: list[str], days: int = 30, region: str = "MX", lang: str = "es") -> list[dict]:
    yt = youtube.data_api(channel_id)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for q in queries:
        try:
            r = yt.search().list(part="id", q=q, type="video", order="viewCount", publishedAfter=since,
                                 regionCode=region, relevanceLanguage=lang, maxResults=50).execute()
            ids = [it["id"]["videoId"] for it in r.get("items", [])]
            vids = []
            if ids:
                vr = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids)).execute()
                for v in vr.get("items", []):
                    vids.append({"title": v["snippet"]["title"], "ch": v["snippet"]["channelId"],
                                 "chname": v["snippet"]["channelTitle"],
                                 "lang": v["snippet"].get("defaultAudioLanguage", ""),
                                 "views": int(v.get("statistics", {}).get("viewCount", 0)),
                                 "secs": _iso_dur(v.get("contentDetails", {}).get("duration", ""))})
            chans = list({v["ch"] for v in vids})
            info = {}
            for k in range(0, len(chans), 50):
                cr = yt.channels().list(part="statistics", id=",".join(chans[k:k + 50])).execute()
                for c in cr.get("items", []):
                    st = c.get("statistics", {})
                    subs = int(st.get("subscriberCount", 0) or 0)
                    n = max(1, int(st.get("videoCount", 1) or 1))
                    info[c["id"]] = {"subs": subs, "avg": int(st.get("viewCount", 0) or 0) / n}
            for v in vids:
                i = info.get(v["ch"], {})
                v["subs"] = i.get("subs")
                v["x"] = round(v["views"] / i["avg"], 1) if i.get("avg") else None
            small = {c for c, i in info.items() if i["subs"] < 50_000}
            outl = [v for v in vids if (v["x"] or 0) >= 3]
            views = [v["views"] for v in vids] or [0]
            longs = [v for v in vids if v["secs"] > 70]
            out.append({
                "query": q, "videos": len(vids), "channels": len(chans), "small_channels": len(small),
                "median_views": int(statistics.median(views)), "top10_views": sorted(views)[-10:][::-1],
                "long_share": round(len(longs) / max(1, len(vids)), 2),
                "outliers": len(outl), "outliers_small": sum(1 for v in outl if v["ch"] in small),
                "best_small": sorted([v for v in vids if v["ch"] in small], key=lambda v: -v["views"])[:6],
                "best_outliers": sorted(outl, key=lambda v: -(v["x"] or 0))[:6],
            })
        except Exception as e:
            out.append({"query": q, "error": str(e)[:200]})
    return out
