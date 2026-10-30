"""PRODUCTOR para canales generados (LCMC/Lumi) — versión con retención.

1. Una sola locución (Edge TTS gratis) con tiempo por palabra.
2. Cada escena dura exactamente lo que tarda en decirse; visual = clip de stock real
   (Pexels/Pixabay) o imagen (stock / FLUX / Gemini) con movimiento Ken Burns.
3. Texto en pantalla: gancho grande (0-2.5 s), subtítulos palabra por palabra, marca de agua,
   banner de marca (largos), CTA a mitad y final.
4. Música de fondo que baja sola cuando habla la voz.
5. Largos: tarjeta final de 5 s (suscríbete + siguiente video). Shorts: sin tarjeta (loop).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import MEDIA_DIR, ChannelConfig, settings
from ..db import save_job
from ..llm import generate_image
from ..models import VideoJob
from ..tools import anim, compose, ffmpeg

log = logging.getLogger("producer.generated")


def _scene_bounds(scenes: list[dict], words: list[tuple[float, float, str]], total: float) -> list[tuple[float, float]]:
    """Inicio/fin de cada escena según los tiempos de las palabras."""
    counts = [max(1, len(s["text"].split())) for s in scenes]
    n = len(words)
    bounds, acc = [], 0
    total_tokens = sum(counts)
    for i, c in enumerate(counts):
        a_idx = round(acc / total_tokens * n)
        acc += c
        b_idx = round(acc / total_tokens * n)
        start = words[min(a_idx, n - 1)][0] if n else total * (acc - c) / total_tokens
        end = (words[b_idx][0] if b_idx < n else total) if n else total * acc / total_tokens
        bounds.append((start, end))
    if bounds:
        bounds[0] = (0.0, bounds[0][1])
        bounds[-1] = (bounds[-1][0], total)
    return bounds


def _voice(cfg: ChannelConfig, text: str, out: Path):
    if settings().MODE == "dry":
        from ..providers.voice import estimate_words
        from ..tools import tts
        tts.synthesize(text, out)
        return out, estimate_words(text, tts.duration(out))
    from ..providers.voice import speak
    return speak(text, out, cfg.production.voice_gender, cfg.production.voice_rate)


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    assert sc and sc.scenes
    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    portrait = job.idea.format == "short"
    w, h = (720, 1280) if portrait else (1280, 720)
    p = cfg.production

    # 1) voz
    narration = " ".join(s["text"].strip() for s in sc.scenes)
    voice, words = _voice(cfg, narration, folder / "voz.mp3")
    from ..tools.tts import duration
    total_voice = duration(voice) or (words[-1][1] if words else 10)

    # 2) visuales por escena
    bounds = _scene_bounds(sc.scenes, words, total_voice)
    used: set = set()
    clips: list[Path] = []
    for k, (scn, (a, b)) in enumerate(zip(sc.scenes, bounds)):
        secs = max(0.7, b - a)
        clip = folder / f"clip_{k:02}.mp4"
        query = scn.get("stock_query") or scn.get("visual_prompt", "")[:60]
        lumi = scn.get("lumi") or None
        visual = scn.get("visual", "anim")
        made = False
        if visual == "anim" and scn.get("anim"):
            try:
                params = json.loads(scn.get("anim_params_json") or "{}")
            except Exception:
                params = {}
            try:
                anim.render(scn["anim"], params if isinstance(params, dict) else {}, secs, clip, w, h, lumi)
                made = True
            except Exception as e:
                log.warning("animación %s falló (%s)", scn.get("anim"), e)
        if not made and visual == "studio" and scn.get("studio_clip"):
            src = next((f for f in (anim.ASSETS / "studio").glob(scn["studio_clip"] + ".*")), None)
            if src:
                compose.stock_clip(src, secs, clip, w, h, lumi=anim.lumi_path(lumi))
                made = True
        if not made and settings().MODE != "dry" and p.prefer_stock_video and visual == "video":
            from ..providers.visuals import stock_video
            src = stock_video(query, folder / f"stock_{k:02}.mp4", portrait, used)
            if src:
                try:
                    compose.stock_clip(src, secs, clip, w, h, lumi=anim.lumi_path(lumi))
                    made = True
                except Exception as e:
                    log.warning("clip de stock falló (%s); uso imagen", e)
                finally:
                    src.unlink(missing_ok=True)
        if not made:
            if visual in ("anim", "studio", "video") and not scn.get("visual_prompt"):
                # sin imagen que pedir: tarjeta animada con la frase clave
                anim.render("titulo", {"texto": scn.get("on_screen") or scn["text"][:60]}, secs, clip, w, h, lumi)
            else:
                img = generate_image(f"{scn.get('visual_prompt', scn['text'])}. {p.image_style}", folder / f"img_{k:02}.jpg",
                                     "9:16" if portrait else "16:9", query=query, sources=p.visual_sources, used=used,
                                     card_text=scn.get("on_screen", ""))
                compose.image_clip(img, secs, clip, w, h, k, lumi=anim.lumi_path(lumi))
        clips.append(clip)

    # 3) tarjeta final (solo largos: en shorts rompe el loop)
    total = total_voice
    if not portrait:
        card = compose.card_png([sc.cta_end or "¡Suscríbete!"], folder / "endcard.png", w, h,
                                sub=sc.comment_question or p.brand_name, logo=compose.ASSETS / "brand" / f"{cfg.id}.png")
        clips.append(compose.image_clip(card, 5.0, folder / "clip_end.mp4", w, h, 1))
        total += 5.0
    silent = ffmpeg.concat(clips, folder / "video_mudo.mp4")

    # 4) texto en pantalla + capítulos
    ctas = []
    if not portrait and sc.cta_mid:
        mid = total_voice * 0.35
        ctas.append((mid, mid + 3.5, sc.cta_mid))
    ctas.append((max(0, total_voice - 3.0), total_voice + (5 if not portrait else 0.3), "SUSCRÍBETE" if not portrait else "SÍGUEME PARA MÁS"))
    ass = compose.build_ass(words, folder / "subs.ass", w, h, hook=sc.hook_text or sc.hook,
                            watermark=p.watermark, intro_banner=(p.brand_name if not portrait else ""),
                            intro_at=bounds[1][0] if len(bounds) > 1 else 3.0, ctas=ctas, accent=p.accent_ass,
                            chunk=3 if portrait else 5)
    if not portrait:
        step = max(1, len(sc.scenes) // 6)
        sc.chapters = [{"t": bounds[i][0], "title": sc.scenes[i]["text"][:48].rstrip(".,")}
                       for i in range(0, len(sc.scenes), step)][:8]

    # 5) música + mezcla final
    music = None
    try:
        music = compose.music_track(total, folder / "musica.mp3", p.music_mood)
    except Exception as e:
        log.warning("sin música (%s)", e)
    out = compose.final_mix(silent, voice, music, ass, folder / "final.mp4", total)

    for f in folder.glob("clip_*"):
        f.unlink(missing_ok=True)   # liberar disco; final.mp4 se queda
    silent.unlink(missing_ok=True)

    job.video_path = str(out)
    job.status = "produced"
    save_job(job)
    log.info("[%s] producido %s (%.0fs, %d escenas)", cfg.id, out, total, len(clips))
    return job
