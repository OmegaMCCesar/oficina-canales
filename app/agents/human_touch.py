"""PRODUCTOR DEL TOQUE HUMANO ("Bandeja de César").

YouTube no monetiza videos genéricos hechos en serie. Lo que nadie puede copiar es la experiencia
real de César (15 años de técnico). Este agente:
1. Le pide a César, por el panel, cosas concretas y rápidas: una foto de un caso real, una nota de
   voz de 15-40 s contando un tip o un caso, ligadas a los videos que vienen.
2. Cuando llegan, las asigna al video que mejor encaja:
   - foto -> sustituye el visual de la escena que habla de eso (con la etiqueta "Caso real").
   - nota de voz -> se inserta con la voz real de César después del gancho.
Los archivos viven en Firestore (pequeños: fotos comprimidas, notas de voz cortas) y se les quita
el GPS/EXIF al subirlos.
"""
from __future__ import annotations

import base64
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import add_activity, jobs, save_job, store
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief

log = logging.getLogger("human")

MAX_BYTES = 700_000
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/aac", "audio/x-m4a", "audio/m4a", "audio/wav", "audio/x-wav")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")


class Ask(BaseModel):
    text: str         # pedido concreto y rápido de cumplir (≤ 25 palabras)
    kind: str         # photo | voice
    for_title: str    # video para el que sirve


class AsksOut(BaseModel):
    asks: list[Ask]


class Pick(BaseModel):
    asset_id: str
    scene: int = -1   # para fotos: escena (desde 1) donde va


class PicksOut(BaseModel):
    picks: list[Pick]


SYSTEM_ASK = """Eres el PRODUCTOR DEL TOQUE HUMANO. El dueño del canal es un técnico con 15 años de experiencia.
Pídele cosas pequeñas y concretas que hagan los próximos videos únicos y que solo él puede dar:
- photo: una foto de un caso real (pieza dañada, medición en el multímetro, equipo abierto).
- voice: una nota de voz de 15-40 segundos contando un caso real, un error común que ha visto o un tip.
Cada pedido debe poder cumplirse en menos de 2 minutos. Máximo 3 pedidos."""

SYSTEM_PICK = """Asigna los materiales reales de César al video. Solo usa los que de verdad encajan con el tema.
Para fotos indica la escena (número desde 1) cuya frase habla de lo que se ve en la foto. Máximo 2 fotos y 1 nota de voz."""


def requests(channel_id: str, status: str = "open") -> list[dict]:
    rs = [r for r in store().list(channel_id, "human_requests") if not status or r.get("status") == status]
    return sorted(rs, key=lambda r: r.get("created_at", ""), reverse=True)


def assets(channel_id: str, unused_only: bool = False) -> list[dict]:
    out = []
    for a in store().list(channel_id, "human_assets"):
        if unused_only and a.get("used_in"):
            continue
        out.append({k: v for k, v in a.items() if k != "b64"})
    return sorted(out, key=lambda a: a.get("created_at", ""), reverse=True)


def add_request(cfg: ChannelConfig, text: str, kind: str, for_title: str = "") -> dict | None:
    if any(r["text"] == text for r in requests(cfg.id)):
        return None
    r = {"id": "req_" + uuid.uuid4().hex[:8], "text": text, "kind": kind, "for_title": for_title, "status": "open",
         "created_at": datetime.now(timezone.utc).isoformat()}
    store().put(cfg.id, "human_requests", r["id"], r)
    add_activity(cfg.id, "humano", "César: " + text, kind="ask")
    return r


def make_requests(cfg: ChannelConfig) -> None:
    """Hasta 3 pedidos abiertos; se renuevan cuando César los atiende o pasan 7 días."""
    open_rs = requests(cfg.id)
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for r in open_rs:
        if r.get("created_at", "") < week:
            r["status"] = "expired"
            store().put(cfg.id, "human_requests", r["id"], r)
    if len([r for r in open_rs if r.get("created_at", "") >= week]) >= 2:
        return
    if cfg.production.kind == "gameplay":
        return  # en gaming lo que hace falta son partidas grabadas (lo pide el Director/Showrunner)
    upcoming = [j.idea.title for j in jobs(cfg.id) if j.status in ("approved", "scripted")][:5]
    upcoming += [i.get("title", "") for i in store().list(cfg.id, "ideas")][:3]
    fb = AsksOut(asks=[
        Ask(text="Mándame una foto de una pieza dañada que tengas a la mano (capacitor, bomba, resistencia)", kind="photo",
            for_title=upcoming[0] if upcoming else ""),
        Ask(text="Graba 20 segundos contando el error más común que ves en los técnicos novatos", kind="voice",
            for_title=upcoming[0] if upcoming else ""),
    ])
    try:
        out = ask_json(SYSTEM_ASK, f"{channel_brief(cfg)}\nPRÓXIMOS VIDEOS: {upcoming or 'aún no hay'}", AsksOut, fallback=fb)
    except Exception as e:
        log.warning("[%s] sin pedidos nuevos (%s)", cfg.id, e)
        return
    for a in out.asks[:3]:
        add_request(cfg, a.text, "voice" if a.kind == "voice" else "photo", a.for_title)


def save_asset(channel_id: str, kind: str, mime: str, raw: bytes, note: str, request_id: str = "") -> dict:
    """Valida y guarda un material de César. Las fotos se re-codifican (quita GPS/EXIF y reduce tamaño)."""
    if kind == "photo":
        if mime not in IMAGE_TYPES:
            raise ValueError("formato de imagen no permitido")
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        q = 85
        im.save(buf, "JPEG", quality=q)
        while buf.tell() > MAX_BYTES and q > 40:
            q -= 10
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q)
        raw, mime = buf.getvalue(), "image/jpeg"
    elif kind == "voice":
        if mime.split(";")[0] not in AUDIO_TYPES:
            raise ValueError("formato de audio no permitido")
    else:
        raise ValueError("tipo no permitido")
    if len(raw) > MAX_BYTES:
        raise ValueError("archivo muy grande (máx 700 KB: notas de voz de hasta ~60 s)")
    doc = {"id": "ast_" + uuid.uuid4().hex[:10], "kind": kind, "mime": mime.split(";")[0], "note": note[:300],
           "request_id": request_id, "used_in": [], "bytes": len(raw),
           "b64": base64.b64encode(raw).decode(), "created_at": datetime.now(timezone.utc).isoformat()}
    store().put(channel_id, "human_assets", doc["id"], doc)
    if request_id:
        r = store().get(channel_id, "human_requests", request_id)
        if r:
            r["status"] = "done"
            store().put(channel_id, "human_requests", request_id, r)
    add_activity(channel_id, "humano", f"¡Llegó material de César! ({'foto' if kind == 'photo' else 'nota de voz'}: {note[:50]})",
                 to="director", kind="ok")
    return {k: v for k, v in doc.items() if k != "b64"}


def asset_file(channel_id: str, asset_id: str, folder: Path) -> Path | None:
    d = store().get(channel_id, "human_assets", asset_id)
    if not d or not d.get("b64"):
        return None
    ext = {"image/jpeg": ".jpg", "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
           "audio/m4a": ".m4a", "audio/mpeg": ".mp3", "audio/aac": ".aac", "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(d["mime"], ".bin")
    out = folder / f"cesar_{asset_id}{ext}"
    out.write_bytes(base64.b64decode(d["b64"]))
    return out


def attach(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    """Asigna fotos/nota de voz sin usar al video que mejor encaje."""
    sc = job.script
    if not sc or cfg.production.kind != "generated":
        return job
    avail = assets(cfg.id, unused_only=True)
    if not avail:
        return job
    reqs = {r["id"]: r for r in store().list(cfg.id, "human_requests")}
    listing = "\n".join(f"- {a['id']} | {a['kind']} | {a['note']} | pedido: {reqs.get(a.get('request_id'), {}).get('text', '')} "
                        f"| para: {reqs.get(a.get('request_id'), {}).get('for_title', '')}" for a in avail[:12])
    scenes = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(sc.scenes))
    # sin IA: lo que se pidió para este video, o lo más reciente
    mine = [a for a in avail if reqs.get(a.get("request_id"), {}).get("for_title", "")[:25].lower() in job.idea.title.lower()
            and reqs.get(a.get("request_id"), {}).get("for_title")]
    fb_list = mine or avail
    fb = PicksOut(picks=[Pick(asset_id=a["id"], scene=min(3, len(sc.scenes))) for a in
                         ([x for x in fb_list if x["kind"] == "photo"][:1] + [x for x in fb_list if x["kind"] == "voice"][:1])])
    try:
        out = ask_json(SYSTEM_PICK, f"VIDEO: {sc.title}\nESCENAS:\n{scenes}\nMATERIALES:\n{listing}", PicksOut, fallback=fb)
    except Exception as e:
        log.warning("[%s] toque humano sin asignar (%s)", cfg.id, e)
        out = fb
    by_id = {a["id"]: a for a in avail}
    used, photos, voice = [], 0, 0
    for p in out.picks:
        a = by_id.get(p.asset_id)
        if not a:
            continue
        if a["kind"] == "photo" and photos < 2:
            k = p.scene - 1 if 1 <= p.scene <= len(sc.scenes) else min(2, len(sc.scenes) - 1)
            if k == 0 and len(sc.scenes) > 1:
                k = 1       # el gancho no se toca
            sc.scenes[k]["visual"] = "photo"
            sc.scenes[k]["photo_id"] = a["id"]
            photos += 1
        elif a["kind"] == "voice" and voice < 1:
            voice += 1
        else:
            continue
        used.append({"id": a["id"], "kind": a["kind"], "note": a["note"]})
    if not used:
        return job
    sc.human = used
    save_job(job)
    for u in used:
        d = store().get(cfg.id, "human_assets", u["id"])
        if d:
            d["used_in"] = d.get("used_in", []) + [job.id]
            store().put(cfg.id, "human_assets", u["id"], d)
    add_activity(cfg.id, "humano", f"Puse {photos} foto(s) y {voice} nota(s) de voz de César en: {sc.title[:50]}", to="productor", kind="ok")
    return job
