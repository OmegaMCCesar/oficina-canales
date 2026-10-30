from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


Format = Literal["short", "long"]
JobStatus = Literal[
    "idea",          # Investigador la propuso
    "approved",      # Director la aprobó
    "scripted",      # Guionista terminó
    "produced",      # Productor dejó el mp4
    "thumbnailed",   # Miniaturas listas
    "scheduled",     # Publicador la subió programada
    "published",     # ya visible
    "rejected",
    "failed",
]


class Idea(BaseModel):
    id: str = Field(default_factory=lambda: new_id("idea"))
    channel_id: str
    title: str
    angle: str                      # por qué este ángulo
    format: Format = "short"
    search_intent: str = ""
    score: float = 0.0              # 0-10 asignado por Investigador
    source: str = "researcher"      # researcher | comments | analyst | manual
    source_media: Optional[str] = None   # gaming: archivo de partida
    series_id: str = ""             # Showrunner: serie a la que pertenece
    episode: int = 0
    trailer_of: str = ""            # short tráiler: id del job largo al que manda
    created_at: datetime = Field(default_factory=now)


class Script(BaseModel):
    title: str
    hook: str
    body: str                       # texto narrado completo
    cta: str
    scenes: list[dict] = Field(default_factory=list)   # [{text, visual_prompt, seconds}]
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # gaming
    highlights: list[dict] = Field(default_factory=list)  # [{start, end, caption}]
    # Editor de Retención
    hook_text: str = ""             # texto grande en pantalla los primeros 2-3 s
    cta_mid: str = ""               # CTA a mitad (largos)
    cta_end: str = ""               # CTA final hablado + en pantalla
    comment_question: str = ""      # pregunta para provocar comentarios
    loop_line: str = ""             # shorts: frase final que conecta con el inicio
    retention_notes: str = ""
    # Estratega SEO
    alt_titles: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    pinned_comment: str = ""
    playlist: str = ""
    keywords: list[str] = Field(default_factory=list)
    chapters: list[dict] = Field(default_factory=list)  # [{t, title}] (se calcula al producir)
    seo_notes: str = ""
    # Gerente de monetización
    products: list[dict] = Field(default_factory=list)   # [{name, query, url, kind}]
    affiliate_block: str = ""
    # Productor del toque humano
    human: list[dict] = Field(default_factory=list)      # [{id, kind, note, use}]


class VideoJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    channel_id: str
    idea: Idea
    status: JobStatus = "idea"
    script: Optional[Script] = None
    video_path: Optional[str] = None
    thumbnail_paths: list[str] = Field(default_factory=list)
    thumbnail_chosen: Optional[str] = None
    youtube_video_id: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    published_at: Optional[datetime] = None
    error: Optional[str] = None
    comment_posted: bool = False
    director_notes: str = ""
    package: dict = Field(default_factory=dict)   # Estratega de empaque: título + miniatura aprobados
    qa: dict = Field(default_factory=dict)        # Guardián de calidad: veredicto y originalidad
    anim_notes: str = ""          # notas de César para rehacer la animación
    anim_redo: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Metrics(BaseModel):
    youtube_video_id: str
    channel_id: str
    job_id: Optional[str] = None
    title: str = ""
    window: Literal["24h", "7d", "30d"]
    views: int = 0
    likes: int = 0
    comments: int = 0
    impressions: int = 0
    ctr: float = 0.0                # %
    avg_view_pct: float = 0.0       # retención promedio %
    avg_view_seconds: float = 0.0
    subs_gained: int = 0
    collected_at: datetime = Field(default_factory=now)


class Insight(BaseModel):
    """Memoria del canal: lo que el Analista aprendió y el Director lee."""
    id: str = Field(default_factory=lambda: new_id("ins"))
    channel_id: str
    kind: Literal["topic", "hook", "thumbnail", "timing", "format", "general"]
    statement: str                  # "Temas de refrigerador que no enfría retienen 65%"
    evidence: str                   # ids / números
    confidence: float = 0.5
    action: str = ""                # recomendación concreta
    created_at: datetime = Field(default_factory=now)
    expires_at: Optional[datetime] = None


class DirectorDecision(BaseModel):
    channel_id: str
    approved_idea_ids: list[str]
    rejected: dict[str, str]        # idea_id -> motivo
    schedule_plan: list[dict]       # [{job_id|idea_id, publish_at}]
    requests_to_human: list[str]    # "graba 3 partidas con Fanny"
    rationale: str
    created_at: datetime = Field(default_factory=now)
