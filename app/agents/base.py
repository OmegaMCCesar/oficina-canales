from __future__ import annotations

from ..config import ChannelConfig
from ..db import insights


def channel_brief(cfg: ChannelConfig) -> str:
    """Bloque de contexto que todos los agentes reciben."""
    games = f"\nJuegos: {', '.join(cfg.games)}" if cfg.games else ""
    return f"""CANAL: {cfg.name} ({cfg.youtube_handle or 'sin handle aún'})
Nicho: {cfg.niche}
Audiencia: {cfg.audience}
Tono: {cfg.tone}
Idioma: {cfg.language}{games}
Personaje: {cfg.production.character or 'ninguno'}
Reglas del Director:
{cfg.director_rules.strip()}
Temas prohibidos: {', '.join(cfg.banned_topics) or 'ninguno'}"""


def insights_block(channel_id: str, limit: int = 25) -> str:
    """Memoria del canal (lo que el Analista aprendió) en texto para el prompt."""
    ins = insights(channel_id, limit=limit)
    if not ins:
        return "MEMORIA DEL CANAL: aún no hay datos; usa criterio general del nicho."
    lines = [f"- [{i.kind}] {i.statement} (conf {i.confidence:.0%}). Acción: {i.action}" for i in ins]
    return "MEMORIA DEL CANAL (lo que ha funcionado y lo que no):\n" + "\n".join(lines)


def lessons_block(channel_id: str, kinds: tuple = ("visual",), limit: int = 12) -> str:
    """Lecciones visuales que dejó el Crítico de video: el Animador y el Productor las respetan."""
    ins = [i for i in insights(channel_id, limit=80) if i.kind in kinds][:limit]
    if not ins:
        return ""
    return "LECCIONES DEL CRÍTICO DE VIDEO (aplícalas):\n" + "\n".join(f"- {i.statement}. Haz: {i.action}" for i in ins)
