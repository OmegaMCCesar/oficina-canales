"""Visuales por escena con respaldo: video de stock > foto de stock > IA (FLUX/Gemini) > tarjeta.

El orden depende del canal (production.visual_sources). Para LCMC conviene stock real
(refrigeradores, herramientas) porque da credibilidad técnica; la IA cubre lo que no exista.
"""
from __future__ import annotations

import base64
import logging
import random
from pathlib import Path

import httpx

from . import health, is_quota_error, key

log = logging.getLogger("providers.visuals")

UA = {"User-Agent": "oficina-canales/1.0"}


def _download(url: str, out: Path, max_mb: int = 40) -> Path:
    with httpx.stream("GET", url, headers=UA, timeout=90, follow_redirects=True) as r:
        r.raise_for_status()
        size = 0
        with open(out, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    raise RuntimeError("archivo demasiado grande")
                f.write(chunk)
    return out


# ---------------- video de stock ----------------

def _pexels_video(query: str, portrait: bool, used: set) -> str | None:
    r = httpx.get("https://api.pexels.com/videos/search", headers={"Authorization": key("PEXELS_API_KEY")},
                  params={"query": query, "orientation": "portrait" if portrait else "landscape",
                          "per_page": 15, "size": "medium"}, timeout=30)
    r.raise_for_status()
    for v in r.json().get("videos", []):
        if v["id"] in used or v.get("duration", 0) < 3:
            continue
        files = [f for f in v["video_files"] if f.get("width") and f.get("file_type") == "video/mp4"]
        files.sort(key=lambda f: abs((f["width"] if not portrait else f["height"]) - 1280))
        if files:
            used.add(v["id"])
            return files[0]["link"]
    return None


def _pixabay_video(query: str, portrait: bool, used: set) -> str | None:
    r = httpx.get("https://pixabay.com/api/videos/", params={"key": key("PIXABAY_API_KEY"), "q": query,
                                                              "per_page": 15, "safesearch": "true"}, timeout=30)
    r.raise_for_status()
    for h in r.json().get("hits", []):
        if h["id"] in used:
            continue
        v = h["videos"].get("small") or h["videos"].get("medium")
        if v and v.get("url"):
            used.add(h["id"])
            return v["url"]
    return None


def stock_video(query: str, out: Path, portrait: bool, used: set) -> Path | None:
    for name, env, fn in (("pexels-video", "PEXELS_API_KEY", _pexels_video),
                          ("pixabay-video", "PIXABAY_API_KEY", _pixabay_video)):
        if not key(env) or not health.ok(name):
            continue
        try:
            url = fn(query, portrait, used)
            if url:
                health.success(name)
                return _download(url, out)
        except Exception as e:
            health.failure(name, e, quota=is_quota_error(e))
            log.warning("%s falló: %s", name, str(e)[:120])
    return None


# ---------------- imagen ----------------

def _pexels_photo(query: str, portrait: bool, used: set) -> str | None:
    r = httpx.get("https://api.pexels.com/v1/search", headers={"Authorization": key("PEXELS_API_KEY")},
                  params={"query": query, "orientation": "portrait" if portrait else "landscape", "per_page": 15},
                  timeout=30)
    r.raise_for_status()
    for p in r.json().get("photos", []):
        if p["id"] not in used:
            used.add(p["id"])
            return p["src"]["portrait" if portrait else "large2x"]
    return None


def _pixabay_photo(query: str, portrait: bool, used: set) -> str | None:
    r = httpx.get("https://pixabay.com/api/", params={"key": key("PIXABAY_API_KEY"), "q": query, "image_type": "photo",
                                                       "orientation": "vertical" if portrait else "horizontal",
                                                       "per_page": 15, "safesearch": "true"}, timeout=30)
    r.raise_for_status()
    for h in r.json().get("hits", []):
        if h["id"] not in used:
            used.add(h["id"])
            return h["largeImageURL"]
    return None


def _cloudflare_flux(prompt: str, out: Path) -> Path:
    acc, tok = key("CLOUDFLARE_ACCOUNT_ID"), key("CLOUDFLARE_API_TOKEN")
    r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                   headers={"Authorization": f"Bearer {tok}"}, json={"prompt": prompt[:2000], "steps": 6}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    out.write_bytes(base64.b64decode(r.json()["result"]["image"]))
    return out


def image(prompt: str, query: str, out: Path, portrait: bool, used: set, sources: list[str], card_text: str = "") -> Path:
    """Devuelve una imagen para la escena siguiendo el orden de `sources`."""
    for src in sources:
        try:
            if src == "pexels" and key("PEXELS_API_KEY") and health.ok("pexels"):
                url = _pexels_photo(query, portrait, used)
                if url:
                    health.success("pexels")
                    return _download(url, out)
            elif src == "pixabay" and key("PIXABAY_API_KEY") and health.ok("pixabay"):
                url = _pixabay_photo(query, portrait, used)
                if url:
                    health.success("pixabay")
                    return _download(url, out)
            elif src == "flux" and key("CLOUDFLARE_API_TOKEN") and health.ok("flux"):
                _cloudflare_flux(prompt, out)
                health.success("flux")
                return out
            elif src == "gemini" and key("GEMINI_API_KEY") and health.ok("gemini-image"):
                from ..llm import gemini_image
                gemini_image(prompt, out, "9:16" if portrait else "16:9")
                health.success("gemini-image")
                return out
        except Exception as e:
            name = {"flux": "flux", "gemini": "gemini-image"}.get(src, src)
            health.failure(name, e, quota=is_quota_error(e))
            log.warning("imagen %s falló: %s", src, str(e)[:140])
    from ..llm import card_image
    return card_image(card_text, out, "9:16" if portrait else "16:9")


def random_pick(items: list):
    return random.choice(items) if items else None
