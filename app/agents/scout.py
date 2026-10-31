"""ANALISTA DE PARTIDAS: "ve" cada grabación de César antes de que el Investigador proponga videos.

Sin esto la oficina solo sabe que hay un archivo .mp4, pero no qué héroe se jugó, si se ganó o dónde están las
buenas jugadas; y sin eso (regla de honestidad) no se puede titular nada.

Cómo lo hace con 0.15 CPU:
1. Audio (barato de decodificar): volumen por segundo. Los "Double Kill", "Savage", peleas y el anunciador suben
   el volumen: los picos son candidatos a jugada.
2. Cuadros sueltos (búsqueda rápida, sin decodificar todo el video): uno cada ~20 s para el panorama, los últimos
   segundos (pantalla de Victoria/Derrota) y uno en cada pico de audio.
3. Hojas de cuadros con su segundo → modelo con visión (Claude primero): juego, héroe, resultado, KDA final y
   qué pasa en cada cuadro. Con eso se arman los MOMENTOS con su inicio y fin reales.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel

from ..config import ChannelConfig, settings
from ..db import add_activity, store
from ..tools import ffmpeg
from ..tools.tts import duration

log = logging.getLogger("scout")

PER_SHEET = 16          # 4×4 cuadros por hoja
TILE = (480, 270)


class FrameNote(BaseModel):
    frame: int            # número del cuadro en la hoja (1-16)
    what: str             # qué pasa ("pelea en mid, el jugador mata a 2", "pantalla de carga", "tienda"...)
    action: int           # 0-10 qué tan buena jugada / momento emocionante
    kill_banner: str = "" # texto de racha visible: Double Kill, Triple Kill, Maniac, Savage, Wiped Out, Lord...


class SheetRead(BaseModel):
    game: str = ""        # "Mobile Legends" | "Magic Chess" | "otro" | ""
    hero: str = ""        # héroe del JUGADOR (el que se controla), si se distingue
    result: str = ""      # "victoria" | "derrota" | "" si no se ve
    kda: str = ""         # K/D/A del jugador si se ve (p. ej. "12/3/7")
    frames: list[FrameNote] = []
    summary: str = ""


SYSTEM = """Eres el ANALISTA DE PARTIDAS de un canal de Mobile Legends: Bang Bang / Magic Chess. Ves una hoja con
16 cuadros de una grabación del celular de César, cada uno con su segundo en la esquina. Di SOLO lo que se ve:
- game: el juego; hero: el héroe que controla el jugador (el del centro de la cámara / el de los botones de
  habilidad); result: "victoria" o "derrota" SOLO si se ve la pantalla final (Victory/Defeat); kda si se lee.
- Para cada cuadro: qué pasa, una nota de acción 0-10 (10 = racha de muertes, robo de Lord/Tortuga, pelea
  decisiva; 0 = carga, tienda, muerto esperando) y el anuncio de racha si aparece en pantalla.
Nunca inventes: si no se distingue, deja el campo vacío."""


def _rms_per_second(src: Path) -> list[float]:
    """Volumen (dB) de cada segundo, solo decodificando el audio a 8 kHz."""
    cmd = [ffmpeg.FFMPEG, "-hide_banner", "-nostats", "-threads", "1", "-i", str(src), "-vn",
           "-af", "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", r.stderr):
        v = m.group(1)
        vals.append(-90.0 if v == "-inf" else float(v))
    return vals


def _peaks(rms: list[float], n: int = 14, gap: int = 12) -> list[int]:
    """Segundos con más volumen que su alrededor (picos separados por al menos `gap` s)."""
    if len(rms) < 30:
        return []
    out: list[int] = []
    base = sorted(rms)[len(rms) // 2]
    scored = []
    for i in range(len(rms)):
        lo, hi = max(0, i - 15), min(len(rms), i + 15)
        local = sorted(rms[lo:hi])[(hi - lo) // 2]
        scored.append((rms[i] - max(local, base), i))
    for s, i in sorted(scored, reverse=True):
        if s < 3:
            break
        if all(abs(i - j) >= gap for j in out):
            out.append(i)
        if len(out) >= n:
            break
    return sorted(out)


def _frame(src: Path, t: float, out: Path) -> Image.Image:
    try:
        ffmpeg._run(["-ss", f"{max(0.0, t):.2f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale={TILE[0]}:-2", str(out)])
        im = Image.open(out).convert("RGB")
        im.load()
        return im
    except Exception:
        return Image.new("RGB", TILE, (0, 0, 0))
    finally:
        out.unlink(missing_ok=True)


def _sheet(src: Path, times: list[float], out: Path) -> Path:
    tw, th = TILE
    sheet = Image.new("RGB", (tw * 4 + 12, th * 4 + 12), (25, 25, 25))
    d = ImageDraw.Draw(sheet)
    f = ffmpeg.font(22)
    for k, t in enumerate(times):
        im = _frame(src, t, out.with_name(f"sc_{k}.jpg"))
        im.thumbnail(TILE)            # sin deformar (las grabaciones del cel son 20:9 o verticales)
        x, y = (k % 4) * (tw + 4), (k // 4) * (th + 4)
        sheet.paste(im, (x, y))
        lab = f"{k+1} · {int(t // 60)}:{int(t % 60):02d}"
        d.rectangle([x, y, x + 12 + int(d.textlength(lab, font=f)), y + 30], fill=(255, 214, 40))
        d.text((x + 6, y + 3), lab, font=f, fill=(0, 0, 0))
    sheet.save(out, quality=85)
    return out


def _moments(notes: list[tuple[float, FrameNote]], peaks: list[int], total: float) -> list[dict]:
    """Junta cuadros con acción alta en momentos de 12-25 s (empieza unos segundos antes de la jugada)."""
    hot = sorted([(t, n) for t, n in notes if n.action >= 6 or n.kill_banner], key=lambda x: x[0])
    out: list[dict] = []
    for t, n in hot:
        start, end = max(0.0, t - 9), min(total, t + 6)
        if out and start <= out[-1]["end"] + 3:
            out[-1]["end"] = min(total, max(out[-1]["end"], end), out[-1]["start"] + 30)
            out[-1]["action"] = max(out[-1]["action"], n.action)
            out[-1]["what"] += f" · {n.what}"
            if n.kill_banner:
                out[-1]["banner"] = n.kill_banner
            continue
        out.append({"start": round(start, 1), "end": round(end, 1), "what": n.what, "action": n.action,
                    "banner": n.kill_banner, "audio_peak": any(abs(t - p) <= 3 for p in peaks)})
    return sorted(out, key=lambda m: (-m["action"], m["start"]))[:10]


def key_for(path: str) -> str:
    return md5(Path(path).name.encode()).hexdigest()[:16]


def report(channel_id: str, path: str) -> dict | None:
    return store().get(channel_id, "recordings", key_for(path))


def scout(cfg: ChannelConfig, src: Path) -> dict:
    from ..providers import vision
    total = duration(src) or 0
    if total < 20:
        raise ValueError("la grabación dura menos de 20 s o no se pudo leer")
    add_activity(cfg.id, "investigador", f"Viendo tu partida {src.name} ({int(total // 60)} min): escucho dónde suben las peleas…")
    try:
        peaks = _peaks(_rms_per_second(src))
    except Exception as e:
        log.warning("[%s] sin análisis de audio (%s)", cfg.id, e)
        peaks = []
    step = max(20.0, total / 40)
    grid = [t for t in frange(5, total - 10, step)]
    end = [max(0, total - s) for s in (25, 12, 4)]                 # pantalla final: Victoria/Derrota y KDA
    times = sorted({round(t, 1) for t in grid + [p + 0.5 for p in peaks] + end})
    folder = src.parent / f"scout_{key_for(str(src))}"
    folder.mkdir(exist_ok=True)
    notes: list[tuple[float, FrameNote]] = []
    reads: list[SheetRead] = []
    for s in range(0, len(times), PER_SHEET):
        chunk = times[s:s + PER_SHEET]
        sheet = _sheet(src, chunk, folder / f"hoja_{s // PER_SHEET}.jpg")
        user = (f"Grabación de {int(total // 60)} min {int(total % 60)} s. Esta hoja va del segundo {int(chunk[0])} "
                f"al {int(chunk[-1])}." + (" Incluye el FINAL de la partida." if chunk[-1] >= total - 30 else ""))
        try:
            r = vision.ask(SYSTEM, user, sheet, SheetRead)
        except Exception as e:
            log.warning("[%s] hoja %d sin visión (%s)", cfg.id, s // PER_SHEET, e)
            continue
        reads.append(r)
        for fn in r.frames:
            if 1 <= fn.frame <= len(chunk):
                notes.append((chunk[fn.frame - 1], fn))
    if not reads:
        raise RuntimeError("ningún modelo de visión pudo ver la partida")

    def pick(attr: str) -> str:
        vals = [getattr(r, attr).strip() for r in reads if getattr(r, attr).strip()]
        return max(set(vals), key=vals.count) if vals else ""
    result = next((r.result for r in reversed(reads) if r.result), "")
    kda = next((r.kda for r in reversed(reads) if r.kda), "")
    rep = {"path": str(src), "name": src.name, "duration": round(total, 1), "game": pick("game"), "hero": pick("hero"),
           "result": result, "kda": kda, "peaks": peaks, "moments": _moments(notes, peaks, total),
           "summary": " ".join(r.summary for r in reads if r.summary)[:900],
           "at": datetime.now(timezone.utc).isoformat()}
    store().put(cfg.id, "recordings", key_for(str(src)), rep)
    for f in folder.glob("*.jpg"):
        f.unlink(missing_ok=True)
    best = rep["moments"][0]["what"][:60] if rep["moments"] else "sin jugadas claras"
    add_activity(cfg.id, "investigador",
                 f"Vi tu partida: {rep['hero'] or 'héroe sin identificar'} · {rep['result'] or 'resultado no visible'}"
                 f"{' · KDA ' + kda if kda else ''} · {len(rep['moments'])} jugada(s). La mejor: {best}",
                 to="director", kind="ok" if rep["moments"] else "ask")
    return rep


def frange(a: float, b: float, step: float):
    t = a
    while t < b:
        yield t
        t += step


def run(cfg: ChannelConfig) -> int:
    """Analiza las grabaciones del buzón que todavía no tienen reporte (una por ciclo: cuesta CPU)."""
    if cfg.production.kind != "gameplay" or settings().MODE == "dry":
        return 0
    inbox = Path(settings().GAMING_INBOX)
    if not inbox.exists():
        return 0
    for p in sorted(inbox.iterdir()):
        if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".3gp") and not report(cfg.id, str(p)):
            try:
                scout(cfg, p)
            except Exception as e:
                log.warning("[%s] no pude analizar %s (%s)", cfg.id, p.name, e)
                store().put(cfg.id, "recordings", key_for(str(p)), {"path": str(p), "name": p.name, "error": str(e)[:200],
                                                                    "moments": [], "at": datetime.now(timezone.utc).isoformat()})
                add_activity(cfg.id, "investigador", f"No pude ver tu partida {p.name}: {str(e)[:80]}", kind="bad")
            return 1
    return 0


def brief(rep: dict | None) -> str:
    """Texto para Investigador/Guionista: lo que REALMENTE pasó en la partida."""
    if not rep or rep.get("error"):
        return "(sin análisis de la partida)"
    ms = "\n".join(f"  - {m['start']:.0f}s → {m['end']:.0f}s · acción {m['action']}/10"
                   f"{' · ' + m['banner'] if m.get('banner') else ''} · {m['what'][:160]}" for m in rep["moments"])
    return (f"juego: {rep.get('game') or '?'} · héroe: {rep.get('hero') or 'sin identificar'} · "
            f"resultado: {rep.get('result') or 'no visible'} · KDA: {rep.get('kda') or 'no visible'} · "
            f"duración {int(rep['duration'] // 60)} min\n  resumen: {rep.get('summary', '')[:400]}\n  jugadas:\n{ms or '  (ninguna clara)'}")
