"""Capa de persistencia.

Colecciones (por canal):
  channels/{channel_id}/jobs/{job_id}
  channels/{channel_id}/ideas/{idea_id}
  channels/{channel_id}/metrics/{video_id}_{window}
  channels/{channel_id}/insights/{insight_id}
  channels/{channel_id}/decisions/{iso_date}
  channels/{channel_id}/runs/{iso_datetime}

En MODE=dry todo vive en data/db_<channel>.json para probar sin Firebase.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from .config import DATA_DIR, settings


def _dump(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return json.loads(obj.model_dump_json())
    return obj


class _LocalStore:
    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    def _file(self, channel_id: str) -> Path:
        return DATA_DIR / f"db_{channel_id}.json"

    def _load(self, channel_id: str) -> dict:
        f = self._file(channel_id)
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def _save(self, channel_id: str, data: dict) -> None:
        self._file(channel_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def put(self, channel_id: str, coll: str, doc_id: str, obj: Any) -> None:
        with self._lock:
            data = self._load(channel_id)
            data.setdefault(coll, {})[doc_id] = _dump(obj)
            self._save(channel_id, data)

    def get(self, channel_id: str, coll: str, doc_id: str) -> dict | None:
        return self._load(channel_id).get(coll, {}).get(doc_id)

    def list(self, channel_id: str, coll: str, where: dict | None = None, limit: int = 500) -> list[dict]:
        docs = list(self._load(channel_id).get(coll, {}).values())
        if where:
            docs = [d for d in docs if all(d.get(k) == v for k, v in where.items())]
        docs.sort(key=lambda d: str(d.get("created_at") or d.get("collected_at") or ""), reverse=True)
        return docs[:limit]

    def delete(self, channel_id: str, coll: str, doc_id: str) -> None:
        with self._lock:
            data = self._load(channel_id)
            data.get(coll, {}).pop(doc_id, None)
            self._save(channel_id, data)


class _FirestoreStore:
    def __init__(self) -> None:
        import firebase_admin
        from firebase_admin import credentials, firestore

        try:
            firebase_admin.get_app()
            _ready = True
        except ValueError:
            _ready = False
        if not _ready:
            b64 = settings().FIREBASE_CREDENTIALS_B64
            if b64:
                import base64
                info = json.loads(base64.b64decode(b64))
                cred = credentials.Certificate(info)
            else:
                cred = credentials.Certificate(settings().FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(cred)
        self.client = firestore.client()
        self._mirrors: dict = {}
        self._single: dict = {}
        self._lock = threading.Lock()

    def _coll(self, channel_id: str, coll: str):
        return self.client.collection("channels").document(channel_id).collection(coll)

    # ---- caché en memoria (Firestore gratis = 50 000 lecturas al día) ----
    # Este servidor es el único que escribe, así que cada colección se lee UNA vez y después se mantiene
    # al día con lo que escribimos. Se vuelve a leer completa cada MIRROR_TTL por si algo cambió afuera.
    MIRROR_TTL = 1800
    NO_MIRROR = {"storyboards", "human_assets", "activity"}   # pesadas o enormes: no se reflejan completas

    def _mirror_get(self, channel_id: str, coll: str) -> dict | None:
        import time
        m = self._mirrors.get((channel_id, coll))
        return m["docs"] if m and time.time() - m["at"] < self.MIRROR_TTL else None

    def _mirror_load(self, channel_id: str, coll: str) -> dict:
        import time
        docs = {d.id: d.to_dict() for d in self._coll(channel_id, coll).limit(3000).stream()}
        self._mirrors[(channel_id, coll)] = {"docs": docs, "at": time.time()}
        return docs

    def put(self, channel_id: str, coll: str, doc_id: str, obj: Any) -> None:
        data = _dump(obj)
        self._coll(channel_id, coll).document(doc_id).set(data)
        m = self._mirrors.get((channel_id, coll))
        if m is not None:
            m["docs"][doc_id] = json.loads(json.dumps(data, default=str))
        self._single.pop((channel_id, coll, doc_id), None)

    def get(self, channel_id: str, coll: str, doc_id: str) -> dict | None:
        import time
        docs = self._mirror_get(channel_id, coll)
        if docs is not None:
            d = docs.get(doc_id)
            return dict(d) if d else None
        hit = self._single.get((channel_id, coll, doc_id))
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        snap = self._coll(channel_id, coll).document(doc_id).get()
        val = snap.to_dict() if snap.exists else None
        if coll not in ("storyboards", "human_assets"):
            self._single[(channel_id, coll, doc_id)] = (time.time(), val)
        return val

    def list(self, channel_id: str, coll: str, where: dict | None = None, limit: int = 500) -> list[dict]:
        if coll in self.NO_MIRROR:
            q = self._coll(channel_id, coll)
            for k, v in (where or {}).items():
                q = q.where(k, "==", v)
            docs = [d.to_dict() for d in q.limit(limit).stream()]
        else:
            docs = self._mirror_get(channel_id, coll)
            if docs is None:
                with self._lock:
                    docs = self._mirror_get(channel_id, coll) or self._mirror_load(channel_id, coll)
            docs = [dict(d) for d in docs.copy().values() if all(d.get(k) == v for k, v in (where or {}).items())]
        docs.sort(key=lambda d: str(d.get("created_at") or d.get("collected_at") or ""), reverse=True)
        return docs[:limit]

    def delete(self, channel_id: str, coll: str, doc_id: str) -> None:
        self._coll(channel_id, coll).document(doc_id).delete()
        m = self._mirrors.get((channel_id, coll))
        if m is not None:
            m["docs"].pop(doc_id, None)
        self._single.pop((channel_id, coll, doc_id), None)


_store = None
_store_lock = threading.Lock()


def store():
    """Una sola instancia aunque varias peticiones lleguen a la vez al arrancar."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = _FirestoreStore() if settings().MODE == "live" else _LocalStore()
    return _store


# ---------- helpers tipados ----------
from .models import DirectorDecision, Idea, Insight, Metrics, VideoJob  # noqa: E402


def save_job(job: VideoJob) -> VideoJob:
    job.updated_at = datetime.utcnow()
    store().put(job.channel_id, "jobs", job.id, job)
    return job


def get_job(channel_id: str, job_id: str) -> VideoJob | None:
    d = store().get(channel_id, "jobs", job_id)
    return VideoJob(**d) if d else None


def jobs(channel_id: str, status: str | None = None) -> list[VideoJob]:
    where = {"status": status} if status else None
    return [VideoJob(**d) for d in store().list(channel_id, "jobs", where)]


def save_idea(idea: Idea) -> None:
    store().put(idea.channel_id, "ideas", idea.id, idea)


def ideas(channel_id: str) -> list[Idea]:
    return [Idea(**d) for d in store().list(channel_id, "ideas")]


def save_metrics(m: Metrics) -> None:
    store().put(m.channel_id, "metrics", f"{m.youtube_video_id}_{m.window}", m)


def metrics(channel_id: str, window: str | None = None) -> list[Metrics]:
    where = {"window": window} if window else None
    return [Metrics(**d) for d in store().list(channel_id, "metrics", where)]


def save_insight(i: Insight) -> None:
    store().put(i.channel_id, "insights", i.id, i)


def insights(channel_id: str, limit: int = 40) -> list[Insight]:
    return [Insight(**d) for d in store().list(channel_id, "insights", limit=limit)]


def save_decision(d: DirectorDecision) -> None:
    store().put(d.channel_id, "decisions", d.created_at.strftime("%Y-%m-%dT%H%M"), d)


def log_run(channel_id: str, payload: dict, run_id: str | None = None) -> None:
    rid = run_id or datetime.utcnow().strftime("%Y-%m-%dT%H%M%S")
    store().put(channel_id, "runs", rid, payload)


# ---------- secretos por canal (token OAuth de YouTube) ----------
# Viven en Firestore (privado, modo producción) para sobrevivir reinicios de Render.

def save_secret(channel_id: str, name: str, value: dict) -> None:
    store().put(channel_id, "secrets", name, value)


def get_secret(channel_id: str, name: str) -> dict | None:
    return store().get(channel_id, "secrets", name)


# ---------- actividad en vivo (lo que el panel anima) ----------

_recent: dict[str, list[dict]] = {}     # actividad reciente en memoria (el panel la pide cada pocos segundos)
_recent_loaded: set[str] = set()


def add_activity(channel_id: str, agent: str, msg: str, to: str | None = None, kind: str = "info") -> None:
    """agent/to: director|investigador|guionista|productor|miniaturas|publicador|analista|portal"""
    import time
    from datetime import timezone
    now = datetime.now(timezone.utc)
    doc = {"t": now.isoformat(), "agent": agent, "to": to, "msg": msg[:180], "kind": kind}
    try:
        buf = _recent.setdefault(channel_id, [])
        buf.append(doc)
        del buf[:-300]
        store().put(channel_id, "activity", f"{time.time_ns()}", doc)
    except Exception:
        pass  # la actividad es cosmética: nunca debe tumbar el ciclo


def activity(channel_id: str, since: str = "", limit: int = 60) -> list[dict]:
    st = store()
    if isinstance(st, _FirestoreStore):
        # se lee de Firestore solo la primera vez por canal; después todo sale de memoria (0 lecturas)
        if channel_id not in _recent_loaded:
            from firebase_admin import firestore as fs
            try:
                old = [d.to_dict() for d in st._coll(channel_id, "activity")
                       .order_by("t", direction=fs.Query.DESCENDING).limit(150).stream()]
                mine = _recent.get(channel_id, [])
                seen = {(d["t"], d["msg"]) for d in mine}
                _recent[channel_id] = sorted([d for d in old if (d.get("t"), d.get("msg")) not in seen] + mine,
                                             key=lambda d: d.get("t", ""))[-300:]
                _recent_loaded.add(channel_id)
            except Exception:
                pass
        docs = [d for d in _recent.get(channel_id, []) if d.get("t", "") > since]
        return docs[-limit:]
    docs = st.list(channel_id, "activity", limit=10000)
    docs = [d for d in docs if d.get("t", "") > since]
    docs.sort(key=lambda d: d.get("t", ""))
    return docs[-limit:]
