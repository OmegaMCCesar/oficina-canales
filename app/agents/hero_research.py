"""INVESTIGADOR DE HÉROES (canal de gaming, sin partidas grabadas).

Mientras César junta partidas, el canal sube videos generados de cada héroe de Mobile Legends:
historia (lore), habilidades y cómo jugarlo. La información sale de la wiki comunitaria
(mobile-legends.fandom.com, licencia CC BY-SA) por su API, y el guion SOLO puede usar esos datos.
- Un héroe por ciclo: 1 idea de video largo (historia + habilidades + consejos) y 1 Short.
- Recuerda qué héroes ya se cubrieron (Firestore `heroes`).
"""
from __future__ import annotations

import html
import logging
import random
import re
from datetime import datetime, timezone

import httpx

from ..config import ChannelConfig, settings
from ..db import add_activity, jobs, save_idea, store
from ..models import Idea

log = logging.getLogger("heroes")

WIKI = "https://mobile-legends.fandom.com/api.php"
UA = {"User-Agent": "OficinaDeCanales/1.0 (canal LCMC Games; contacto por YouTube @LCMCGAMES)"}
CREDIT = "Información de héroes: Mobile Legends: Bang Bang Wiki (mobile-legends.fandom.com), licencia CC BY-SA."
# respaldo si la wiki no da la lista: héroes conocidos y populares
STARTERS = ["Lancelot", "Layla", "Miya", "Alucard", "Zilong", "Tigreal", "Gusion", "Fanny", "Ling", "Chou", "Kagura",
            "Eudora", "Saber", "Franco", "Balmond", "Nana", "Hayabusa", "Granger", "Valir", "Esmeralda", "Khufra",
            "Mathilda", "Beatrix", "Yve", "Julian", "Xavier", "Fredrinn", "Joy", "Novaria", "Arlott", "Cici", "Nolan",
            "Brody", "Claude", "Harith", "Lunox", "Kadita", "Hanabi", "Wanwan", "Yu Zhong", "Paquito", "Lylia"]


def _get(params: dict) -> dict:
    r = httpx.get(WIKI, params={**params, "format": "json"}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _clean(h: str) -> str:
    h = re.sub(r"<(script|style|table)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<sup[^>]*>.*?</sup>", "", h, flags=re.S)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"\[\s*edit\s*\]", "", h, flags=re.I)
    return re.sub(r"\s+", " ", h).strip()


def hero_list() -> list[str]:
    for cat in ("Category:Heroes", "Category:Hero"):
        try:
            d = _get({"action": "query", "list": "categorymembers", "cmtitle": cat, "cmlimit": "500", "cmnamespace": "0"})
            names = [m["title"] for m in d.get("query", {}).get("categorymembers", [])
                     if "/" not in m["title"] and not m["title"].lower().startswith(("list", "hero"))]
            if len(names) >= 20:
                return names
        except Exception as e:
            log.info("lista de héroes (%s) no disponible: %s", cat, e)
    return STARTERS


def hero_facts(hero: str) -> str:
    """Historia + lore + habilidades del héroe, en texto plano (inglés), máx ~6000 caracteres."""
    secs = _get({"action": "parse", "page": hero, "prop": "sections", "redirects": "1"})["parse"]["sections"]
    want = [(s["index"], s["line"]) for s in secs
            if re.search(r"story|lore|background|abilit|skill", s["line"], re.I) and s.get("toclevel", 1) == 1]
    out = []
    for idx, name in want[:4]:
        txt = _clean(_get({"action": "parse", "page": hero, "prop": "text", "section": idx, "redirects": "1"})["parse"]["text"]["*"])
        out.append(f"## {name}\n{txt[:2600]}")
    facts = "\n\n".join(out)
    if len(facts) < 300:
        raise RuntimeError("la página del héroe no tiene suficiente información")
    return facts[:6000]


def hero_media(hero: str) -> dict:
    """Arte oficial del héroe en la wiki: splash arts (horizontales grandes) e íconos de habilidades (cuadrados chicos)."""
    files = _get({"action": "parse", "page": hero, "prop": "images", "redirects": "1"})["parse"]["images"]
    files = [f for f in files if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))][:80]
    infos = []
    for i in range(0, len(files), 25):
        d = _get({"action": "query", "titles": "|".join("File:" + f for f in files[i:i + 25]),
                  "prop": "imageinfo", "iiprop": "url|size"})
        for p in d.get("query", {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("url"):
                infos.append((p["title"][5:], ii.get("width", 0), ii.get("height", 0), ii["url"]))
    splash = [u for n, w, h, u in sorted(infos, key=lambda x: -x[1]) if w >= 800 and w >= h * 1.15][:8]
    splash = [re.sub(r"/revision/latest", "/revision/latest/scale-to-width-down/1600", u, count=1) for u in splash]
    icons = []
    for n, w, h, u in infos:
        if w and w == h and w <= 300 and "avatar" not in n.lower() and "emblem" not in n.lower():
            name = re.sub(r"\.(png|jpe?g|webp)$", "", n, flags=re.I).replace("_", " ")
            name = re.sub(rf"^{re.escape(hero)}\s*[-_ ]?\s*", "", name, flags=re.I).strip() or name
            icons.append({"name": name, "url": u})
    return {"splash": splash, "icons": icons[:12]}


def run(cfg: ChannelConfig) -> list[Idea]:
    if cfg.production.kind != "gameplay":
        return []
    # ¿ya hay un héroe en camino? (idea pendiente o video sin publicar)
    pending = [i for i in store().list(cfg.id, "ideas") if i.get("hero")]
    active = [j for j in jobs(cfg.id) if j.idea.hero and j.status in ("approved", "scripted", "produced", "thumbnailed")]
    if pending or len(active) >= 2:
        return []
    covered = {d["id"] for d in store().list(cfg.id, "heroes")}
    if settings().MODE == "dry":
        names, facts_for = STARTERS, (lambda h: f"## Story\n{h} is a hero from the Land of Dawn (dry-run).\n## Abilities\nPassive, Skill 1, Skill 2, Ultimate.")
    else:
        names, facts_for = hero_list(), hero_facts
    options = [h for h in names if h.lower() not in covered]
    random.shuffle(options)
    for hero in options[:3]:
        try:
            facts = facts_for(hero)
            break
        except Exception as e:
            log.info("héroe %s sin datos (%s)", hero, e)
    else:
        return []
    media = {}
    if settings().MODE != "dry":
        try:
            media = hero_media(hero)
            log.info("[%s] arte de %s: %d splash, %d íconos", cfg.id, hero, len(media["splash"]), len(media["icons"]))
        except Exception as e:
            log.info("sin arte oficial de %s (%s)", hero, e)
    ideas = [
        Idea(channel_id=cfg.id, title=f"{hero}: historia, habilidades y cómo jugarlo", format="long", score=8.8,
             source="wiki", hero=hero, facts=facts, media=media, search_intent=f"{hero} mobile legends guia historia",
             angle="Guía completa del héroe: su historia en el universo del juego, qué hace cada habilidad y consejos para jugarlo."),
        Idea(channel_id=cfg.id, title=f"La historia de {hero} en menos de un minuto", format="short", score=8.6,
             source="wiki", hero=hero, facts=facts, media=media, search_intent=f"historia de {hero} mlbb",
             angle="Short narrativo: el dato más interesante del lore del héroe y su habilidad más vistosa."),
    ]
    for i in ideas:
        save_idea(i)
    store().put(cfg.id, "heroes", hero.lower(), {"id": hero.lower(), "hero": hero, "at": datetime.now(timezone.utc).isoformat(),
                                                   "ideas": [i.id for i in ideas]})
    add_activity(cfg.id, "investigador", f"Investigué a {hero} en la wiki: historia y habilidades listas", to="director", kind="ok")
    return ideas
