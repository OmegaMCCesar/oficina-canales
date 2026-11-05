"""ESTUDIO DE RENDER HÍBRIDO: la laptop de César arma los videos pesados cuando está prendida.

- La laptop corre `oficina_worker.py` (junto a la Máquina Lumi) y avisa cada minuto que está en línea.
- Si está en línea, la oficina NO renderiza los videos de plantilla (Máquina Lumi: LCMC y héroes de LCMC Games):
  los deja en «render_queued» con todo lo necesario (escenas, voz, imágenes) y la laptop los arma y los sube.
- Si la laptop se apaga o tarda demasiado, la oficina los arma aquí como siempre (más lento): nunca se detiene.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ChannelConfig, get_channel
from .db import add_activity, get_job, jobs, save_job, store

ONLINE_SECS = 150          # sin latido por más de esto = apagada
CLAIM_TIMEOUT_MIN = 120    # si la laptop agarró un video y no lo entregó en 2 h, se regresa
QUEUE_TIMEOUT_MIN = 45     # si nadie lo agarró en 45 min (laptop apagada), lo arma el servidor

_state = {"last_seen": 0.0, "name": "", "version": ""}


def heartbeat(name: str, version: str = "") -> None:
    _state.update(last_seen=time.time(), name=name[:40], version=version[:20])
    try:
        store().put("_global", "worker", "status", {"last_seen": datetime.now(timezone.utc).isoformat(),
                                                     "name": _state["name"], "version": _state["version"]})
    except Exception:
        pass


def online() -> bool:
    return time.time() - _state["last_seen"] < ONLINE_SECS


def status() -> dict:
    return {"online": online(), "name": _state["name"], "version": _state["version"],
            "seconds_ago": int(time.time() - _state["last_seen"]) if _state["last_seen"] else None}


def hand_off(cfg: ChannelConfig, job) -> bool:
    """Durante el ciclo: si la laptop está en línea, el video se le encarga a ella."""
    if not online():
        return False
    job.status = "render_queued"
    job.qa = {**job.qa, "queued_at": datetime.now(timezone.utc).isoformat()}
    save_job(job)
    add_activity(cfg.id, "productor", f"Le encargo a la laptop de César armar «{job.idea.title[:50]}» (más rápida)",
                 to="productor", kind="ok")
    return True


def next_job() -> dict | None:
    """La laptop pide trabajo: el video en cola más antiguo; queda «rendering» a su nombre."""
    from .producers.maquina import spec
    for ch in ("lcmc", "gaming"):
        for j in sorted(jobs(ch, "render_queued"), key=lambda j: j.qa.get("queued_at", "")):
            cfg = get_channel(ch)
            j.status = "rendering"
            j.qa = {**j.qa, "claimed_at": datetime.now(timezone.utc).isoformat(), "claimed_by": _state["name"]}
            save_job(j)
            add_activity(ch, "productor", f"La laptop está armando «{j.idea.title[:50]}»", kind="ok")
            return {"channel": ch, "job_id": j.id, "spec": spec(cfg, j)}
    return None


def accept_result(channel_id: str, job_id: str, video: Path, thumb: Path | None, seconds: float) -> None:
    j = get_job(channel_id, job_id)
    if not j:
        raise ValueError("no existe ese video")
    j.video_path, j.status = str(video), "produced"
    if thumb and thumb.exists():
        j.thumbnail_paths, j.thumbnail_chosen = [str(thumb)], str(thumb)
        j.qa = {**j.qa, "thumb_by_maquina": True}
    j.qa = {**j.qa, "rendered_by": "laptop", "needs_critic": True, "render_seconds": round(seconds)}
    save_job(j)
    add_activity(channel_id, "productor", f"La laptop entregó «{j.idea.title[:50]}» ({int(seconds // 60)} min de render)",
                 to="critico", kind="ok")


def give_back(channel_id: str, job_id: str, error: str) -> None:
    j = get_job(channel_id, job_id)
    if j and j.status == "rendering":
        j.status = "scripted"
        j.qa = {**j.qa, "laptop_error": error[:200], "no_laptop": True}
        save_job(j)
        add_activity(channel_id, "productor", f"La laptop no pudo armar «{j.idea.title[:40]}»: {error[:80]}. Lo armo aquí.",
                     kind="ask")


def reclaim(cfg: ChannelConfig) -> int:
    """Al inicio de cada ciclo: lo que la laptop no tomó o no entregó regresa a la cola del servidor."""
    n, now = 0, datetime.now(timezone.utc)
    for j in jobs(cfg.id, "render_queued") + jobs(cfg.id, "rendering"):
        ts = j.qa.get("claimed_at") if j.status == "rendering" else j.qa.get("queued_at")
        limit = CLAIM_TIMEOUT_MIN if j.status == "rendering" else QUEUE_TIMEOUT_MIN
        try:
            old = now - datetime.fromisoformat(ts) > timedelta(minutes=limit)
        except Exception:
            old = True
        if old and not (j.status == "render_queued" and online()):
            j.status = "scripted"
            j.qa = {**j.qa, "no_laptop": True}
            save_job(j)
            n += 1
    return n


def bundle_zip() -> bytes:
    """El código de la plantilla + poses + fuentes, para que la laptop use exactamente lo mismo que la oficina."""
    root = Path(__file__).resolve().parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("app/__init__.py", "")
        z.writestr("app/tools/__init__.py", "")
        z.writestr("app/tools/ffmpeg.py", "import shutil\nFFMPEG = shutil.which('ffmpeg') or 'ffmpeg'\n")
        z.writestr("app/memory.py", "import gc\n\ndef trim():\n    gc.collect()\n")
        for f in (root / "maquina").glob("*.py"):
            z.write(f, f"app/maquina/{f.name}")
        for f in (root / "assets" / "maquina").rglob("*"):
            if f.is_file():
                z.write(f, "app/" + str(f.relative_to(root)).replace("\\", "/"))
    return buf.getvalue()


def bundle_version() -> str:
    import hashlib
    root = Path(__file__).resolve().parent
    h = hashlib.md5()
    for f in sorted((root / "maquina").glob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]
