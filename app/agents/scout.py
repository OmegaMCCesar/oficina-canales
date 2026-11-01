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
           # todo lo que vio, cuadro por cuadro: el editor de partidas completas decide qué acelerar o quitar
           "timeline": [{"t": round(t, 1), "action": n.action, "what": n.what[:80], "banner": n.kill_banner}
                        for t, n in sorted(notes, key=lambda x: x[0])],
           "summary": " ".join(r.summary for r in reads if r.summary)[:900],
           "at": datetime.now(timezone.utc).isoformat()}
    prev = store().get(cfg.id, "recordings", key_for(str(src))) or {}
    if prev.get("cesar"):
        rep["cesar"] = prev["cesar"]          # lo que confirmó César no se pierde al volver a ver la partida
    store().put(cfg.id, "recordings", key_for(str(src)), rep)
    try:
        rep = refine(cfg, src, rep)
    except Exception as e:
        log.warning("[%s] sin segunda pasada (%s)", cfg.id, e)
    for f in folder.glob("*.jpg"):
        f.unlink(missing_ok=True)
    best = rep["moments"][0]["what"][:60] if rep["moments"] else "sin jugadas claras"
    add_activity(cfg.id, "investigador",
                 f"Vi tu partida: {rep['hero'] or 'héroe sin identificar'} · {rep['result'] or 'resultado no visible'}"
                 f"{' · KDA ' + kda if kda else ''} · {len(rep['moments'])} jugada(s). La mejor: {best}",
                 to="director", kind="ok" if rep["moments"] else "ask")
    return rep


# ---------- segunda pasada: precisión al segundo en cada jugada ----------
class Beat(BaseModel):
    frame: int            # número de cuadro (1..N)
    what: str             # lo que se ve EXACTAMENTE en ese cuadro ("Hanzo entra por la izquierda", "aparece DOUBLE KILL")


class Refined(BaseModel):
    action_start: int     # cuadro donde EMPIEZA la acción (pelea, persecución)
    action_end: int       # cuadro donde termina (enemigo muerto, se aleja)
    banner_frame: int = 0 # cuadro donde aparece el anuncio (Double/Triple Kill, Savage…); 0 si no hay
    banner: str = ""      # texto del anuncio tal cual
    is_play: bool = True  # False si en realidad no pasa nada interesante
    beats: list[Beat] = []
    summary: str = ""


SYSTEM_REFINE = """Eres el ANALISTA DE PARTIDAS. Ves cuadros CONSECUTIVOS, uno por segundo, de una jugada de Mobile
Legends / Magic Chess, numerados con su minuto:segundo. Ubica con precisión de un cuadro:
- dónde empieza la acción y dónde termina; - en qué cuadro sale el anuncio de racha (Double Kill, Triple Kill, Maniac,
Savage, Wiped Out, First Blood, Lord) y su texto exacto; - beats: lo que se ve en los cuadros clave (6-12), solo lo
visible (quién ataca, quién muere, si escapa). Si no pasa nada interesante, is_play=false. NO inventes."""

PER_REFINE = 30
RTILE = (384, 173)


def _dense_sheet(src: Path, times: list[float], out: Path) -> Path:
    tw, th = RTILE
    cols = 5
    rows = (len(times) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols + 4 * cols, th * rows + 4 * rows), (25, 25, 25))
    d = ImageDraw.Draw(sheet)
    f = ffmpeg.font(18)
    for k, t in enumerate(times):
        im = _frame(src, t, out.with_name(f"rf_{k}.jpg"))
        im.thumbnail(RTILE)
        x, y = (k % cols) * (tw + 4), (k // cols) * (th + 4)
        sheet.paste(im, (x, y))
        lab = f"{k+1} · {int(t // 60)}:{int(t % 60):02d}"
        d.rectangle([x, y, x + 10 + int(d.textlength(lab, font=f)), y + 24], fill=(255, 214, 40))
        d.text((x + 5, y + 2), lab, font=f, fill=(0, 0, 0))
    sheet.save(out, quality=86)
    return out


def refine(cfg: ChannelConfig, src: Path, rep: dict, top: int = 6) -> dict:
    """Vuelve a ver cada jugada a 1 cuadro por segundo para saber EXACTAMENTE dónde pasa (y qué se ve)."""
    from ..providers import vision
    total = rep["duration"]
    out_moments = []
    for m in sorted(rep.get("moments", []), key=lambda x: -x["action"])[:top]:
        center = (m["start"] + m["end"]) / 2
        a = max(0.0, min(m["start"] - 6, center - PER_REFINE / 2))
        times = [round(a + k, 1) for k in range(PER_REFINE) if a + k < total]
        if len(times) < 5:
            continue
        sheet = _dense_sheet(src, times, src.parent / f"refine_{int(a)}.jpg")
        try:
            r = vision.ask(SYSTEM_REFINE, f"{len(times)} cuadros, 1 por segundo, desde el segundo {int(a)}. "
                                          f"El Analista creía ver: {m['what'][:160]}", sheet, Refined)
        except Exception as e:
            log.warning("[%s] no pude afinar la jugada %.0fs (%s)", cfg.id, center, e)
            continue
        finally:
            sheet.unlink(missing_ok=True)
        if not r.is_play:
            continue
        at = lambda fr: times[max(1, min(len(times), fr)) - 1]
        s0, s1 = at(r.action_start), at(r.action_end)
        if s1 < s0:
            s0, s1 = s1, s0
        bt = at(r.banner_frame) if r.banner_frame else None
        start = max(0.0, s0 - 2)
        end = min(total, max(s1, bt or s1) + 2.5)
        out_moments.append({"start": round(start, 1), "end": round(end, 1), "action": m["action"],
                            "banner": r.banner or m.get("banner", ""), "banner_at": bt,
                            "what": r.summary or m["what"], "refined": True,
                            "beats": [{"t": at(b.frame), "what": b.what[:100]} for b in r.beats
                                      if 1 <= b.frame <= len(times)][:12]})
    if out_moments:
        rep["moments"] = sorted(out_moments, key=lambda x: (-x["action"], x["start"]))
        rep["refined_at"] = datetime.now(timezone.utc).isoformat()
        store().put(cfg.id, "recordings", key_for(str(src)), rep)
        add_activity(cfg.id, "investigador", f"Afiné {len(out_moments)} jugada(s) segundo a segundo en {src.name}",
                     to="guionista", kind="ok")
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
        old = report(cfg.id, str(p))
        # reportes de antes del editor de partidas completas no traen la línea de tiempo: se vuelve a ver
        if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".3gp") and (not old or ("timeline" not in old and not old.get("error"))):
            try:
                scout(cfg, p)
            except Exception as e:
                log.warning("[%s] no pude analizar %s (%s)", cfg.id, p.name, e)
                store().put(cfg.id, "recordings", key_for(str(p)), {"path": str(p), "name": p.name, "error": str(e)[:200],
                                                                    "moments": [], "at": datetime.now(timezone.utc).isoformat()})
                add_activity(cfg.id, "investigador", f"No pude ver tu partida {p.name}: {str(e)[:80]}", kind="bad")
            return 1
        if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".3gp") and old and old.get("moments") \
                and not old.get("refined_at") and not old.get("error"):
            try:
                refine(cfg, p, old)
            except Exception as e:
                log.warning("[%s] no pude afinar %s (%s)", cfg.id, p.name, e)
            return 1
    return 0


def brief(rep: dict | None) -> str:
    """Texto para Investigador/Guionista: lo que REALMENTE pasó en la partida."""
    if not rep or rep.get("error"):
        return "(sin análisis de la partida)"
    def _m(m: dict) -> str:
        banner = ""
        if m.get("banner"):
            banner = f" · {m['banner']}" + (f" (aparece en {m['banner_at']:.0f}s)" if m.get("banner_at") else "")
        tag = f" [CÉSAR · {m.get('kind', 'jugada')}]" if m.get("by_cesar") else (" [PRECISO]" if m.get("refined") else " [aproximado]")
        line = f"  - {m['start']:.0f}s → {m['end']:.0f}s · acción {m['action']}/10{banner} · {m['what'][:160]}{tag}"
        beats = "".join(f"\n      {b['t']:.0f}s: {b['what']}" for b in m.get("beats", []))
        return line + beats
    ms = "\n".join(_m(m) for m in moments_of(rep))
    if has_marks(rep):
        ms = ("  (MARCADAS POR CÉSAR: son la verdad; los tiempos y lo que pasa salen de él. "
              "kind=short → un short por cada una; kind=largo → va en el largo)\n" + ms)
    c = rep.get("cesar") or {}
    hero, result, kda = c.get("hero") or rep.get("hero"), c.get("result") or rep.get("result"), c.get("kda") or rep.get("kda")
    conf = " (CONFIRMADO POR CÉSAR, úsalo en títulos)" if c else ""
    return (f"juego: {rep.get('game') or '?'} · héroe: {hero or 'sin identificar'} · "
            f"resultado: {result or 'no visible'} · KDA: {kda or 'no visible'}{conf}"
            + (f" · nota de César: {c['note']}" if c.get("note") else "") + " · "
            f"duración {int(rep['duration'] // 60)} min\n  resumen: {rep.get('summary', '')[:400]}\n  jugadas:\n{ms or '  (ninguna clara)'}")


# ---------- marcas de César (la verdad: la IA confunde kills de otros con las suyas) ----------
_T = r"(\d{1,2})[:.](\d{2})"
MARK_RE = re.compile(rf"^\s*{_T}\s*(?:-|–|a|al|hasta)\s*{_T}\s*[|:,-]?\s*(short|largo|jugada)?\s*[|:,-]?\s*(.*)$", re.I)


def parse_marks(text: str) -> tuple[list[dict], list[str]]:
    """Una línea por momento: «02:30-02:55 | short | mi triple kill en la jungla». Devuelve (marcas, líneas malas)."""
    marks, bad = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = MARK_RE.match(line)
        if not m:
            bad.append(line.strip()[:80])
            continue
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        if b <= a:
            bad.append(line.strip()[:80])
            continue
        marks.append({"start": float(a), "end": float(b), "kind": (m.group(5) or "jugada").lower(),
                      "what": m.group(6).strip()[:200] or "jugada"})
    return sorted(marks, key=lambda x: x["start"]), bad


def moments_of(rep: dict | None) -> list[dict]:
    """Jugadas a usar: las que marcó César (si hay); si no, las del Analista."""
    if not rep:
        return []
    marks = rep.get("cesar_marks") or []
    if marks:
        return [{"start": m["start"], "end": m["end"], "action": 10, "banner": "", "banner_at": None,
                 "what": m["what"], "kind": m["kind"], "refined": True, "by_cesar": True,
                 "beats": [{"t": m["start"], "what": m["what"]}]} for m in marks]
    return rep.get("moments", [])


def has_marks(rep: dict | None) -> bool:
    return bool(rep and rep.get("cesar_marks"))
