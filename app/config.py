from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = ROOT / "channels"
DATA_DIR = ROOT / "data"
MEDIA_DIR = ROOT / "media"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    MODE: Literal["dry", "live"] = "dry"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_IMAGE_MODEL: str = "imagen-4.0-generate-001"
    FIREBASE_CREDENTIALS: str = "./firebase-sa.json"
    YOUTUBE_CLIENT_SECRETS: str = "./client_secret.json"
    GOOGLE_APPLICATION_CREDENTIALS: str = ""
    API_TOKEN: str = "dev"
    GAMING_INBOX: str = "./media/gaming/inbox"


@lru_cache
def settings() -> Settings:
    return Settings()


# ---------- Configuración por canal (channels/*.yaml) ----------

class Cadence(BaseModel):
    shorts_per_day: int = 1
    longs_per_week: int = 0
    publish_hours_local: list[int] = Field(default_factory=lambda: [12, 19])
    timezone: str = "America/Mexico_City"
    min_gap_hours: int = 6          # nunca dos videos más juntos que esto
    max_queue_ready: int = 10       # videos producidos esperando turno


class Production(BaseModel):
    kind: Literal["generated", "gameplay"] = "generated"
    voice: str = "es-US-Neural2-B"   # Google TTS
    language: str = "es-MX"
    aspect_short: str = "9:16"
    short_max_seconds: int = 58
    long_target_minutes: int = 6
    image_style: str = ""
    character: str = ""


class ChannelConfig(BaseModel):
    id: str
    name: str
    youtube_handle: str
    youtube_channel_id: str = ""     # se rellena tras OAuth
    niche: str
    audience: str
    tone: str
    language: str = "es-MX"
    cadence: Cadence = Cadence()
    production: Production = Production()
    topic_seeds: list[str] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    director_rules: str = ""         # texto libre que el Director respeta
    games: list[str] = Field(default_factory=list)   # solo gaming


@lru_cache
def load_channels() -> dict[str, ChannelConfig]:
    out: dict[str, ChannelConfig] = {}
    for f in sorted(CHANNELS_DIR.glob("*.yaml")):
        cfg = ChannelConfig(**yaml.safe_load(f.read_text(encoding="utf-8")))
        out[cfg.id] = cfg
    return out


def get_channel(channel_id: str) -> ChannelConfig:
    chans = load_channels()
    if channel_id not in chans:
        raise KeyError(f"Canal desconocido: {channel_id}. Disponibles: {list(chans)}")
    return chans[channel_id]
