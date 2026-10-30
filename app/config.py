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

    # dry = todo simulado | live = Firestore + Gemini + YouTube reales
    MODE: Literal["dry", "live"] = "dry"
    # private = sube a YouTube como PRIVADO sin programar (tú revisas y publicas en Studio)
    # scheduled = sube privado con fecha de publicación automática
    PUBLISH_MODE: Literal["private", "scheduled"] = "private"

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_IMAGE_MODEL: str = "gemini-2.5-flash-image"
    GEMINI_TTS_MODEL: str = "gemini-2.5-flash-preview-tts"

    FIREBASE_CREDENTIALS: str = "./firebase-sa.json"
    FIREBASE_CREDENTIALS_B64: str = ""      # JSON de la cuenta de servicio en base64 (Render)

    YOUTUBE_CLIENT_SECRETS: str = "./client_secret.json"
    YOUTUBE_CLIENT_ID: str = ""             # cliente OAuth tipo web (Render)
    YOUTUBE_CLIENT_SECRET: str = ""
    PUBLIC_URL: str = "http://localhost:8000"

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
