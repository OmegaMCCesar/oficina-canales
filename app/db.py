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

        if not firebase_admin._apps:
            b64 = settings().FIREBASE_CREDENTIALS_B64
            if b64:
                import base64
                info = json.loads(base64.b64decode(b64))
                cred = credentials.Certificate(info)
            else:
                cred = credentials.Certificate(settings().FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(cred)
        self.client = firestore.client()

    def _coll(self, channel_id: str, coll: str):
        return self.client.collection("channels").document(channel_id).collection(coll)

    def put(self, channel_id: str, coll: str, doc_id: str, obj: Any) -> None:
        self._coll(channel_id, coll).document(doc_id).set(_dump(obj))

    def get(self, channel_id: str, coll: str, doc_id: str) -> dict | None:
        snap = self._coll(channel_id, coll).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def list(self, channel_id: str, coll: str, where: dict | None = None, limit: int = 500) -> list[dict]:
        q = self._coll(channel_id, coll)
        for k, v in (where or {}).items():
            q = q.where(k, "==", v)
        docs = [d.to_dict() for d in q.limit(limit).stream()]
        docs.sort(key=lambda d: str(d.get("created_at") or d.get("collected_at") or ""), reverse=True)
        return docs

    def delete(self, channel_id: str, coll: str, doc_id: str) -> None:
        self._coll(channel_id, coll).document(doc_id).delete()


_store = None


def store():
    global _store
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
