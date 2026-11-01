"""EDITOR DE PARTIDA COMPLETA (video largo con la grabación de César).

Formato de los canales de gameplay que retienen (cold open + partida sin tiempos muertos + clímax + cierre):
1. COLD OPEN (8-12 s): la mejor jugada primero, con rótulo «MIRA CÓMO TERMINA ESTO».
2. LA PARTIDA en orden, editada:
   - jugadas (acción alta, anuncios de racha): velocidad normal, con rótulo y acercamiento en la kill;
   - tramos tranquilos (farmeo, caminar): acelerados ×2.5 con un «▶▶» chiquito;
   - pantallas de carga, tienda, esperando revivir: se quitan.
3. COMENTARIO: la voz del canal entra en las jugadas (el juego baja de volumen) y el resto respira con su audio.
4. CIERRE: el final de la partida a velocidad normal + llamado a suscribirse.
Todo a 1280×720 30 fps ultrafast para que quepa en el servidor gratis (0.15 CPU, 512 MB).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import MEDIA_DIR, ChannelConfig
from ..db import add_activity, save_job
from ..memory import trim
from ..models import VideoJob
from ..tools import ffmpeg
from ..tools.tts import duration

log = logging.getLogger("producer.fullmatch")

W, H = 1280, 720
FAST = 2.5
DEAD = re.compile(r"carga|loading|tienda|shop|muert|esperando|revivi|respawn|men[uú]|selecci[oó]n|draft|pantalla de inicio", re.I)
ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p", "-r", "30",
       "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k"]


def plan(rep: dict, comments: list[dict]) -> list[dict]:
    """Lista de tramos {start, end, kind: play|fast|cut, caption, banner_t} a partir de lo que vio el Analista."""
    total = float(rep["duration"])
    tl = sorted(rep.get("timeline") or [], key=lambda x: x["t"])
    if not tl:   # reportes viejos: solo jugadas; lo demás se acelera
        tl = [{"t": (m["start"] + m["end"]) / 2, "action": m["action"], "what": m["what"], "banner": m.get("banner", "")}
              for m in rep.get("moments", [])]
    # cada cuadro manda desde la mitad con el anterior hasta la mitad con el siguiente
    segs: list[dict] = []
    for i, f in enumerate(tl):
        a = 0.0 if i == 0 else (tl[i - 1]["t"] + f["t"]) / 2
        b = total if i == len(tl) - 1 else (f["t"] + tl[i + 1]["t"]) / 2
        kind = "play" if (f["action"] >= 6 or f.get("banner")) else ("cut" if (f["action"] <= 2 and DEAD.search(f["what"])) else "fast")
        segs.append({"start": a, "end": b, "kind": kind, "caption": "", "banner_t": None})
    if not segs:
        segs = [{"start": 0.0, "end": total, "kind": "fast", "caption": "", "banner_t": None}]
    # jugadas y comentarios del guion: siempre a velocidad normal (con colchón)
    forced = [(m["start"] - 2, m["end"] + 2, m.get("banner") or "", m) for m in rep.get("moments", [])]
    forced += [(c["start"] - 1, c["end"] + 1, "", c) for c in comments]
    for a, b, banner, m in forced:
        segs = _force(segs, max(0.0, a), min(total, b))
        for sg in segs:
            if sg["kind"] == "play" and sg["start"] < b and sg["end"] > a:
                if banner and not sg["caption"]:
                    sg["caption"] = banner.upper()
                    sg["banner_t"] = max(sg["start"], min(sg["end"] - 2.5, m["end"] - 6))
                if m.get("caption") and not sg["caption"]:
                    sg["caption"] = m["caption"]
    # el final de la partida a velocidad normal (resultado y KDA)
    segs = _force(segs, max(0.0, total - 15), total)
    merged: list[dict] = []
    for sg in segs:
        if sg["end"] - sg["start"] < 0.4:
            continue
        if merged and merged[-1]["kind"] == sg["kind"] and not sg["caption"] and sg["kind"] != "play":
            merged[-1]["end"] = sg["end"]
        else:
            merged.append(dict(sg))
    # tramos acelerados muy largos: se parten en pedazos de ≤ 90 s de origen (ffmpeg y memoria más tranquilos)
    out: list[dict] = []
    for sg in merged:
        if sg["kind"] == "fast" and sg["end"] - sg["start"] > 90:
            t = sg["start"]
            while t < sg["end"] - 0.4:
                out.append({**sg, "start": t, "end": min(sg["end"], t + 90)})
                t += 90
        else:
            out.append(sg)
    return out


def _force(segs: list[dict], a: float, b: float) -> list[dict]:
    """Marca [a, b] como «play» partiendo los tramos que lo cruzan."""
    out = []
    for sg in segs:
        if sg["end"] <= a or sg["start"] >= b or sg["kind"] == "play":
            out.append(sg)
            continue
        if sg["start"] < a:
            out.append({**sg, "end": a})
        out.append({**sg, "start": max(sg["start"], a), "end": min(sg["end"], b), "kind": "play"})
        if sg["end"] > b:
            out.append({**sg, "start": b})
    return sorted(out, key=lambda x: x["start"])


def _atempo(speed: float) -> str:
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:.3f}")
    return ",".join(parts)


def _render(src: Path, sg: dict, out: Path, k: int) -> Path:
    speed = FAST if sg["kind"] == "fast" else 1.0
    base = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    vf = f"setpts=PTS/{speed},{base}" if speed != 1 else base
    badge = "▶▶ x2.5" if speed != 1 else sg["caption"]
    args = ["-ss", f"{sg['start']:.2f}", "-to", f"{sg['end']:.2f}", "-i", str(src)]
    if badge:
        cap = ffmpeg.caption_png(badge, W, H, out.with_name(f"cap_{k}.png"), 0.08 if speed != 1 else 0.16,
                                 22 if speed != 1 else 44)
        fc = f"[0:v]{vf}[b];[b][1:v]overlay=0:0[v]"
        if sg.get("banner_t") is not None and speed == 1:   # acercamiento en la kill
            t0 = sg["banner_t"] - sg["start"]
            fc = (f"[0:v]{base},split[a][z];[z]crop=iw/1.18:ih/1.18,scale={W}:{H}[zz];"
                  f"[a][zz]overlay=0:0:enable='between(t,{t0:.2f},{t0 + 2.5:.2f})'[b0];[b0][1:v]overlay=0:0[v]")
        args += ["-i", str(cap), "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
    else:
        args += ["-vf", vf, "-map", "0:v", "-map", "0:a?"]
    if speed != 1:
        args += ["-af", _atempo(speed)]
    ffmpeg._run([*args, *ENC, str(out)])
    return out


def _with_voice(clip: Path, text: str, out: Path, cfg: ChannelConfig, k: int) -> Path:
    from ..providers.voice import speak
    vo, _ = speak(text, out.with_name(f"vo_{k}.mp3"), cfg.production.voice_gender, cfg.production.voice_rate)
    ffmpeg._run(["-i", str(clip), "-i", str(vo), "-filter_complex",
                 "[0:a]volume=0.30[g];[1:a]apad[v];[g][v]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)])
    return out


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    from ..agents import scout
    src = Path(job.idea.source_media)
    rep = scout.report(cfg.id, str(src))
    if not rep or not rep.get("moments"):
        raise ValueError("la partida no tiene análisis del Analista")
    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    comments = job.script.highlights
    segs = plan(rep, comments)
    kept = sum((s["end"] - s["start"]) / (FAST if s["kind"] == "fast" else 1) for s in segs if s["kind"] != "cut")
    add_activity(cfg.id, "productor", f"Editando la partida completa: {len(segs)} tramos → ~{int(kept // 60)} min "
                 f"(de {int(rep['duration'] // 60)} min grabados)", to="director")
    clips: list[Path] = []
    # 1) cold open: la mejor jugada primero
    best = max(rep["moments"], key=lambda m: m["action"])
    hook = {"start": max(0.0, best["end"] - 11), "end": best["end"], "kind": "play",
            "caption": "MIRA CÓMO TERMINA ESTO", "banner_t": None}
    c = _render(src, hook, folder / "s_hook.mp4", 0)
    if job.script.hook:
        c = _with_voice(c, job.script.hook, folder / "s_hook_v.mp4", cfg, 0)
    clips.append(c)
    # 2) la partida editada, con el comentario en su lugar
    pending = sorted(comments, key=lambda h: h["start"])
    for k, sg in enumerate(segs, start=1):
        if sg["kind"] == "cut":
            continue
        c = _render(src, sg, folder / f"s_{k:03}.mp4", k)
        say = [h for h in pending if sg["kind"] == "play" and sg["start"] - 1 <= h["start"] < sg["end"]]
        if say:
            pending = [h for h in pending if h not in say]
            c = _with_voice(c, " ".join(h.get("voiceover", "") for h in say), folder / f"s_{k:03}_v.mp4", cfg, k)
        clips.append(c)
        if k % 6 == 0:
            trim()
    # 3) cierre con llamado
    if job.script.cta and clips:
        clips[-1] = _with_voice(clips[-1], job.script.cta, folder / "s_end_v.mp4", cfg, 999)
    out = ffmpeg.concat(clips, folder / "final.mp4")
    for c in folder.glob("s_*.mp4"):
        c.unlink(missing_ok=True)
    job.video_path, job.status = str(out), "produced"
    save_job(job)
    log.info("[%s] partida completa editada: %s (%.0f s)", cfg.id, out, duration(out) or 0)
    return job
