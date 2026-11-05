"""GUARDIA DE NARRACIÓN: nada que no sea lo que dice el narrador llega a la voz ni a la pantalla.

Los modelos a veces dejan notas internas dentro del guion: «[verificar dato en la wiki]», «(aparece Layla
disparando)», «Nota: confirmar…», «TODO». Eso se leía en voz alta. Aquí se quitan y, si una escena queda
dudosa o vacía, se descarta. Se usa en el Guionista (todos los formatos) y otra vez antes de producir.
"""
from __future__ import annotations

import re

# notas entre corchetes/paréntesis/llaves o con marcadores típicos
_BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>")
_STAGE_PAREN = re.compile(r"\((?:[^)]*\b(?:aparece|se ve|en pantalla|mostrar|muestra|imagen|clip|corte|transici[oó]n|"
                          r"m[uú]sica|sfx|efecto|zoom|verificar|confirmar|wiki|dato|pendiente|nota|(?-i:TODO|TBD))\b[^)]*)\)", re.I)
_META = re.compile(
    r"(verificar|confirmar|revisar|checar|consultar)\s+(?:este|ese|el|la|los|las|con|en|dato|datos|informaci[oó]n|cifra|n[uú]mero|wiki)"
    r"|\bseg[uú]n la wiki\b|\b(?:en|de) la wiki\b|\bwiki\b|\bdato pendiente\b|\bmarcador\b|\bplaceholder\b|(?-i:\bTODO\b|\bTBD\b)"
    r"|\bnota (?:del|para el) (?:guionista|editor|director)\b|^nota:|\bguion\b.*\bpendiente\b", re.I)
_STAGE = re.compile(r"^\s*(?:aparece|se ve|vemos|en pantalla|mostramos|muestra|corte a|transici[oó]n|plano de|toma de|"
                    r"imagen de|clip de|b-?roll|m[uú]sica|sfx)\b", re.I)


def clean_text(text: str) -> str:
    t = _BRACKETS.sub(" ", text or "")
    t = _STAGE_PAREN.sub(" ", t)
    keep = []
    for sent in re.split(r"(?<=[.!?¡¿])\s+", t):
        s = sent.strip()
        if not s:
            continue
        if _META.search(s) or _STAGE.match(s):
            continue                       # oración que es nota interna o indicación de cámara
        keep.append(s)
    out = re.sub(r"\s{2,}", " ", " ".join(keep)).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", out)


def problems(text: str) -> list[str]:
    """Lo que delata una nota interna (para que Calidad lo marque si algo se colara)."""
    found = []
    if _BRACKETS.search(text or ""):
        found.append("texto entre corchetes")
    if _META.search(text or ""):
        found.append("nota interna (verificar/wiki/pendiente)")
    if _STAGE.match(text or ""):
        found.append("indicación de cámara en la narración")
    return found


def clean_scenes(scenes: list[dict], min_words: int = 3) -> tuple[list[dict], int]:
    """Limpia la narración y los textos de pantalla; quita escenas que quedan vacías. Devuelve (escenas, quitadas)."""
    out, dropped = [], 0
    for s in scenes:
        s = dict(s)
        for k in ("text", "narracion"):
            if s.get(k):
                s[k] = clean_text(s[k])
        for k in ("titulo", "on_screen", "big", "card", "detail", "caption"):
            if isinstance(s.get(k), str):
                v = _BRACKETS.sub("", s[k]).strip()
                s[k] = "" if _META.search(v) else v
        if isinstance(s.get("puntos"), list):
            s["puntos"] = [p for p in (_BRACKETS.sub("", x).strip() for x in s["puntos"]) if p and not _META.search(p)]
        if len((s.get("text") or s.get("narracion") or "").split()) < min_words:
            dropped += 1
            continue
        out.append(s)
    return out, dropped
