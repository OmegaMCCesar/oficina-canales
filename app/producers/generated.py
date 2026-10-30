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


def _cesar_voice(cfg: ChannelConfig, asset_id: str, folder: Path, portrait: bool):
    """Nota de voz de César normalizada a mp3. En shorts solo si dura ≤ 20 s."""
    from ..agents.human_touch import asset_file
    from ..tools.tts import duration
    src = asset_file(cfg.id, asset_id, folder)
    if not src:
        return None
    out = folder / "cesar_voz.mp3"
    try:
        ffmpeg._run(["-i", str(src), "-vn", "-af", "loudnorm=I=-16:TP=-1.5,aformat=sample_rates=44100:channel_layouts=mono",
                     "-t", "60", "-c:a", "libmp3lame", "-b:a", "128k", str(out)])
    finally:
        src.unlink(missing_ok=True)
    d = duration(out)
    if not d or (portrait and d > 20):
        log.info("nota de voz de César no usada (%.1fs)", d or 0)
        return None
    return out, d


def _splice(voice: Path, insert: Path, cut: float, out: Path) -> Path:
    fmt = "aformat=sample_rates=44100:channel_layouts=mono"
    fc = (f"[0:a]atrim=0:{cut:.3f},asetpts=PTS-STARTPTS,{fmt}[a0];[0:a]atrim=start={cut:.3f},asetpts=PTS-STARTPTS,{fmt}[a1];"
          f"[1:a]{fmt},apad=pad_dur=0.25[c];[a0][c][a1]concat=n=3:v=0:a=1[a]")
    ffmpeg._run(["-i", str(voice), "-i", str(insert), "-filter_complex", fc, "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "128k", str(out)])
    return out


def _photo(cfg: ChannelConfig, asset_id: str, folder: Path, k: int, w: int, h: int) -> Path | None:
    """Foto real de César completa sobre fondo desenfocado, con la etiqueta 'CASO REAL · CÉSAR'."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    from ..agents.human_touch import asset_file
    src = asset_file(cfg.id, asset_id, folder)
    if not src:
        return None
    ph = Image.open(src).convert("RGB")
    src.unlink(missing_ok=True)
    W, H = int(w * 1.15), int(h * 1.15)          # image_clip recorta ~15% al hacer zoom
    sc = max(W / ph.width, H / ph.height)
    bg = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(28))).enhance(0.45)
    fg = ph.copy()
    fg.thumbnail((int(w * 0.86), int(h * 0.62)))
    x, y = (W - fg.width) // 2, (H - fg.height) // 2
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle([x - 8, y - 8, x + fg.width + 8, y + fg.height + 8], radius=18, fill=(183, 242, 26))
    bg.paste(fg, (x, y))
    fs = max(24, w // 20)
    f = ffmpeg.font(fs)
    label = "CASO REAL · CÉSAR"
    tw = d.textlength(label, font=f)
    lx, ly = (W - tw) / 2, y - fs * 2.3
    d.rounded_rectangle([lx - fs * 0.6, ly - fs * 0.3, lx + tw + fs * 0.6, ly + fs * 1.4], radius=fs // 2, fill=(183, 242, 26))
    d.text((lx, ly), label, font=f, fill=(7, 16, 31))
    out = folder / f"foto_{k:02}.jpg"
    bg.save(out, quality=90)
    return out


def _fetch(url: str, folder: Path) -> Path | None:
    """Descarga una imagen (arte oficial de la wiki) con caché por URL; máx 10 MB."""
    import hashlib

    import httpx
    out = folder / ("web_" + hashlib.md5(url.encode()).hexdigest()[:12] + ".img")
    if out.exists():
        return out
    if url.startswith("/") and Path(url).exists():      # pruebas locales
        out.write_bytes(Path(url).read_bytes())
        return out
    r = httpx.get(url, headers={"User-Agent": "OficinaDeCanales/1.0 (@LCMCGAMES)"}, timeout=40, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > 10_000_000:
        return None
    out.write_bytes(r.content)
    return out


def _framed(src: Path, out: Path, w: int, h: int, label: str = "") -> Path:
    """Imagen completa (sin cortar al personaje) sobre su propia versión desenfocada; opcional etiqueta abajo."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    ph = Image.open(src).convert("RGB")
    W, H = int(w * 1.15), int(h * 1.15)
    sc = max(W / ph.width, H / ph.height)
    bg = ph.resize((int(ph.width * sc) + 1, int(ph.height * sc) + 1))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(26))).enhance(0.5)
    fg = ph.copy()
    landscape_fill = w > h and ph.width >= ph.height
    fg.thumbnail((int(w * (1.0 if landscape_fill else 0.9)), int(h * (0.86 if landscape_fill else 0.62))))
    x, y = (W - fg.width) // 2, (H - fg.height) // 2 - (int(h * 0.04) if h > w else 0)
    bg.paste(fg, (x, y))
    if label:
        d = ImageDraw.Draw(bg)
        fs = max(26, min(w, h) // 16)
        f = ffmpeg.font(fs)
        tw = d.textlength(label, font=f)
        lx, ly = (W - tw) / 2, y + fg.height + fs * 0.6
        d.rounded_rectangle([lx - fs * 0.6, ly - fs * 0.3, lx + tw + fs * 0.6, ly + fs * 1.4], radius=fs // 2, fill=(139, 124, 246))
        d.text((lx, ly), label, font=f, fill="white")
    bg.save(out, quality=90)
    return out


def _icon_card(src: Path, out: Path, w: int, h: int, name: str) -> Path:
    """Tarjeta de habilidad: ícono oficial grande al centro + nombre."""
    from PIL import Image, ImageDraw
    W, H = int(w * 1.15), int(h * 1.15)
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    top, bot = (16, 12, 40), (52, 36, 110)
    for yy in range(H):
        t = yy / H
        d.line([(0, yy), (W, yy)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    ic = Image.open(src).convert("RGBA")
    size = int(min(W, H) * 0.42)
    ic = ic.resize((size, size), Image.LANCZOS)
    landscape = W > H
    cx, cy = (int(W * 0.33), int(H * 0.44)) if landscape else (W // 2, int(H * 0.38))
    d.ellipse([cx - size * 0.62, cy - size * 0.62, cx + size * 0.62, cy + size * 0.62], fill=(139, 124, 246))
    bg.paste(ic, (cx - size // 2, cy - size // 2), ic)
    fs = max(30, min(W, H) // 13)
    f = ffmpeg.font(fs)
    lines = ffmpeg.wrap_lines(d, name.upper(), f, int(W * (0.38 if landscape else 0.8)))[:3]
    for i, line in enumerate(lines):
        if landscape:   # nombre a la derecha del ícono, lejos de los subtítulos
            d.text((int(W * 0.56), cy - (len(lines) * fs * 1.2) / 2 + i * fs * 1.2), line, font=f, fill=(255, 255, 255),
                   anchor="lt", stroke_width=4, stroke_fill="black")
        else:
            d.text((cx, cy + size * 0.8 + i * fs * 1.2), line, font=f, fill=(255, 255, 255), anchor="mt", stroke_width=4, stroke_fill="black")
    bg.save(out, quality=90)
    return out


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    assert sc and sc.scenes
    folder = MEDIA_DIR / cfg.id / job.id
    folder.mkdir(parents=True, exist_ok=True)
    portrait = job.idea.format == "short"
    w, h = (720, 1280) if portrait else (1280, 720)
    fps = 24 if portrait else 15          # largos: menos cuadros dibujados = render 40% más rápido
    p = cfg.production

    # 1) voz
    narration = " ".join(s["text"].strip() for s in sc.scenes)
    voice, words = _voice(cfg, narration, folder / "voz.mp3")
    from ..tools.tts import duration
    total_voice = duration(voice) or (words[-1][1] if words else 10)

    # 2) visuales por escena
    bounds = _scene_bounds(sc.scenes, words, total_voice)

    # voz real de César (toque humano): entra justo después del gancho
    cesar_seg = None
    vid = next((x["id"] for x in sc.human if x.get("kind") == "voice"), None)
    if vid:
        try:
            got = _cesar_voice(cfg, vid, folder, portrait)
            if got:
                cpath, cdur = got
                cdur += 0.25
                cut = bounds[0][1] if len(bounds) > 1 else 0.0
                voice = _splice(voice, cpath, cut, folder / "voz_total.mp3")
                words = [(a + cdur, b + cdur, wd) if a >= cut - 1e-3 else (a, b, wd) for a, b, wd in words]
                bounds = [(a + cdur, b + cdur) if i > 0 else (a, b) for i, (a, b) in enumerate(bounds)]
                total_voice += cdur
                cesar_seg = (cut, cdur)
        except Exception as e:
            log.warning("no pude insertar la voz de César (%s)", e)
    used: set = set()
    clips: list[Path] = []
    for k, (scn, (a, b)) in enumerate(zip(sc.scenes, bounds)):
        secs = max(0.7, b - a)
        clip = folder / f"clip_{k:02}.mp4"
        query = scn.get("stock_query") or scn.get("visual_prompt", "")[:60]
        lumi = scn.get("lumi") or None
        visual = scn.get("visual", "anim")
        made = False
        if visual in ("web", "icon") and scn.get("img_url"):
            try:
                src = _fetch(scn["img_url"], folder)
                if src:
                    img = (_icon_card(src, folder / f"icon_{k:02}.jpg", w, h, scn.get("on_screen") or "")
                           if visual == "icon" else _framed(src, folder / f"web_{k:02}.jpg", w, h))
                    compose.image_clip(img, secs, clip, w, h, k)
                    made = True
            except Exception as e:
                log.warning("arte oficial no disponible (%s); uso tarjeta", e)
        if not made and visual == "photo" and scn.get("photo_id"):
            try:
                ph = _photo(cfg, scn["photo_id"], folder, k, w, h)
                if ph:
                    compose.image_clip(ph, secs, clip, w, h, k)
                    made = True
            except Exception as e:
                log.warning("foto de César falló (%s)", e)
        if visual == "anim" and scn.get("anim"):
            try:
                params = json.loads(scn.get("anim_params_json") or "{}")
            except Exception:
                params = {}
            try:
                anim.render(scn["anim"], params if isinstance(params, dict) else {}, secs, clip, w, h, lumi, fps=fps)
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
                anim.render("titulo", {"texto": scn.get("on_screen") or scn["text"][:60]}, secs, clip, w, h, lumi, fps=fps)
            else:
                img = generate_image(f"{scn.get('visual_prompt', scn['text'])}. {p.image_style}", folder / f"img_{k:02}.jpg",
                                     "9:16" if portrait else "16:9", query=query, sources=p.visual_sources, used=used,
                                     card_text=scn.get("on_screen", ""))
                compose.image_clip(img, secs, clip, w, h, k, lumi=anim.lumi_path(lumi))
        clips.append(clip)
        if k == 0 and cesar_seg:
            cc = folder / "clip_cesar.mp4"
            anim.render("titulo", {"texto": "LA VOZ DE CÉSAR", "sub": "Técnico · 15 años en línea blanca"}, cesar_seg[1], cc, w, h, "confirmacion")
            clips.append(cc)

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
    if cesar_seg:
        ctas.append((cesar_seg[0] + 0.2, cesar_seg[0] + min(cesar_seg[1], 4.0), "CASO REAL"))
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
    out = compose.final_mix(silent, voice, music, ass, folder / "final.mp4", total,
                            preset="veryfast" if portrait else "superfast")

    for pat in ("web_*", "icon_*"):
        for f in folder.glob(pat):
            f.unlink(missing_ok=True)
    for f in folder.glob("clip_*"):
        f.unlink(missing_ok=True)   # liberar disco; final.mp4 se queda
    silent.unlink(missing_ok=True)

    job.video_path = str(out)
    job.status = "produced"
    save_job(job)
    log.info("[%s] producido %s (%.0fs, %d escenas)", cfg.id, out, total, len(clips))
    return job
