"""PRODUCTOR «MÁQUINA LUMI»: la plantilla que César armó para LCMC (fondo azul noche, Lumi con sus poses,
título con barra de acento, viñetas que entran, subtítulo con la palabra resaltada, barra de progreso).

- LCMC: escenas {tipo, pose, titulo, puntos, text}.
- LCMC Games (videos de héroe): la misma plantilla con tema morado/dorado; en lugar de Lumi va el arte del
  héroe y, cuando se explica una habilidad u objeto, su ícono en el recuadro de imagen.
Render por cuadros con PIL → ffmpeg (1 hilo). En el servidor gratis: largos a 20 fps, cortos a 30 fps.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import MEDIA_DIR, ChannelConfig
from ..db import add_activity, save_job
from ..memory import trim
from ..models import VideoJob
from ..tools.tts import duration

log = logging.getLogger("producer.maquina")


def uses_maquina(cfg: ChannelConfig, job: VideoJob) -> bool:
    if cfg.production.style == "lumi" and not job.idea.source_media:
        return True
    return bool(job.idea.facts) and getattr(cfg.production, "hero_template", "") == "maquina"


def _escenas(cfg: ChannelConfig, job: VideoJob, folder: Path) -> list[dict]:
    from ..maquina import marca as M
    from .generated import _fetch
    out = []
    for k, s in enumerate(job.script.scenes):
        text = (s.get("text") or s.get("narracion") or "").strip()
        if not text:
            continue
        e = {"tipo": s.get("tipo") or ("gancho" if k == 0 else "explicacion"), "narracion": text,
             "titulo": s.get("titulo") or s.get("on_screen") or s.get("big") or "",
             "puntos": list(s.get("puntos") or ([s["detail"]] if s.get("detail") else []))[:3],
             "pose": s.get("pose") if s.get("pose") in M.POSES else "feliz"}
        if job.idea.facts:   # héroe: la mascota de LCMC Games presenta; el arte o el ícono va en el recuadro
            e["pose"] = _mascot_pose(k, len(job.script.scenes), s, out[-1]["pose"] if out else "")
            art = s.get("bg_url") if s.get("visual") in ("icon", "clip", "item") else s.get("img_url")
            art = art or (job.idea.media.get("splash") or [None])[k % max(1, len(job.idea.media.get("splash") or [1]))]
            try:
                p = _fetch(art, folder) if art else None
                if p:
                    e["imagen"] = str(p.resolve())
            except Exception as ex:
                log.info("sin arte para la escena %d (%s)", k, ex)
            icon = s.get("img_url") if s.get("visual") == "icon" else None
            if s.get("visual") == "item" and s.get("item"):
                try:
                    from ..agents.hero_research import item_icon
                    icon = item_icon(s["item"])
                except Exception:
                    icon = None
            if icon:
                try:
                    p = _fetch(icon, folder)
                    if p:
                        e["imagen"] = str(_icon_panel(p, p.with_suffix(".panel.png")))
                except Exception:
                    pass
            if not e["titulo"]:
                e["titulo"] = " ".join(text.split()[:5]).rstrip(".,")
        out.append(e)
    return out


ACTION = ["tajo", "combo", "embestida", "magia", "fuego", "giro", "corriendo", "invocacion"]
TALK = ["decidido", "pensando", "apuntando", "tranquilo", "listo", "feliz", "sentado_serio"]


def _mascot_pose(k: int, n: int, s: dict, prev: str) -> str:
    """Pose de la mascota según la escena: acción en el gancho y en habilidades; pregunta → pensando; cierre feliz."""
    text = (s.get("text") or "").lower()
    if k == 0:
        return "giro"
    if k == n - 1:
        return "feliz" if prev != "feliz" else "poder"
    if "?" in text and k > 1:
        return "pensando" if prev != "pensando" else "preocupado"
    if s.get("visual") in ("icon", "clip") or any(w in text for w in ("habilidad", "ulti", "daño", "combo", "golpe")):
        pool = ACTION
    elif any(w in text for w in ("historia", "nació", "abismo", "reino", "familia", "pasado", "lore")):
        pool = ["tranquilo", "sentado_serio", "pensando", "perfil", "decidido"]
    elif any(w in text for w in ("error", "cuidado", "no hagas", "evita", "débil", "counter")):
        pool = ["enojado", "preocupado", "apuntando"]
    else:
        pool = TALK
    pick = pool[k % len(pool)]
    return pick if pick != prev else pool[(k + 1) % len(pool)]


def _icon_panel(src: Path, out: Path, w: int = 980, h: int = 460) -> Path:
    """Ícono chico (habilidad u objeto) a su tamaño, centrado en un panel del tema: nada estirado ni pixelado."""
    from PIL import Image, ImageDraw, ImageFilter
    from ..maquina import marca as M
    bg = Image.new("RGB", (w, h), tuple(min(255, c + 18) for c in M.FONDO))
    d = ImageDraw.Draw(bg)
    ic = Image.open(src).convert("RGBA")
    side = min(int(h * 0.78), max(ic.width, ic.height) * 3)      # hasta 3x su tamaño original, no más
    ic.thumbnail((side, side), Image.LANCZOS) if max(ic.size) > side else None
    if max(ic.size) < side:
        ic = ic.resize((int(ic.width * side / max(ic.size)), int(ic.height * side / max(ic.size))), Image.LANCZOS)
    cx, cy = w // 2, h // 2
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    r = int(max(ic.size) * 0.62)
    ImageDraw.Draw(glow).ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*M.CIAN, 110))
    bg.paste(glow.filter(ImageFilter.GaussianBlur(28)), (0, 0), glow.filter(ImageFilter.GaussianBlur(28)))
    d.ellipse((cx - r * 0.86, cy - r * 0.86, cx + r * 0.86, cy + r * 0.86), outline=M.VERDE, width=6)
    bg.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    bg.save(out)
    return out


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    from ..maquina import marca as M
    from ..maquina import render as R
    from ..providers.voice import speak
    from ..tools import compose
    M.usar_tema("games" if cfg.id == "gaming" else "lcmc")
    R.F.cache_clear()
    folder = MEDIA_DIR / cfg.id / job.id
    trabajo = folder / "maquina"
    trabajo.mkdir(parents=True, exist_ok=True)
    escenas = _escenas(cfg, job, trabajo)
    if not escenas:
        raise ValueError("el guion no tiene escenas")
    fmt = "corto" if job.idea.format == "short" else "largo"
    add_activity(cfg.id, "productor", f"Máquina Lumi: narrando {len(escenas)} escenas…")
    nars = []
    for i, e in enumerate(escenas):
        mp3, words = speak(e["narracion"], trabajo / f"voz_{i:02d}.mp3", cfg.production.voice_gender, cfg.production.voice_rate)
        nars.append({"audio": mp3, "duracion": duration(mp3) or 1.0,
                     "palabras": [(w[2], w[0]) for w in words], "real": True})
    total = sum(n["duracion"] + R.COLA for n in nars) + 0.25
    musica = None
    try:
        musica = compose.music_track(total + 2, trabajo / "musica.mp3", cfg.production.music_mood)
    except Exception as ex:
        log.info("sin música (%s)", ex)
    fps = 30 if fmt == "corto" else 20
    add_activity(cfg.id, "productor", f"Máquina Lumi: armando el video ({int(total // 60)} min {int(total % 60)} s, {fps} fps)…")
    trim()
    out, dur = R.renderizar(escenas, nars, fmt, folder / "final.mp4", trabajo, musica, fps=fps)
    trim()
    # miniatura de la plantilla (LCMC): falla en letras enormes + aparato dibujado + Lumi
    if cfg.id != "gaming" and job.script.extra.get("miniatura"):
        try:
            from ..maquina.miniatura import generar_miniatura
            th = generar_miniatura({"titulo": job.script.title, "miniatura": job.script.extra["miniatura"]},
                                   folder / "miniatura_maquina.jpg")
            job.thumbnail_paths, job.thumbnail_chosen = [str(th)], str(th)
            job.qa = {**job.qa, "thumb_by_maquina": True}
        except Exception as ex:
            log.warning("miniatura de la máquina falló (%s)", ex)
    for f in trabajo.glob("*.wav"):
        f.unlink(missing_ok=True)
    job.video_path, job.status = str(out), "produced"
    save_job(job)
    log.info("[%s] Máquina Lumi: %s (%.0f s)", cfg.id, out, dur)
    return job
