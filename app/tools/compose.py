"""Composición final del video: subtítulos palabra por palabra (ASS), gancho en pantalla,
marca de agua, banner de intro, CTA, tarjeta final y música de fondo con 'ducking'.

Todo el texto en pantalla va en un solo archivo .ass que el filtro `subtitles` (libass) dibuja;
así no dependemos de drawtext ni de fuentes del sistema.
"""
from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .ffmpeg import FFMPEG, FONT_PATH, _run, fit_image, font, wrap_lines

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _t(s: float) -> str:
    s = max(0.0, s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:05.2f}"


def _esc(txt: str) -> str:
    return txt.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[tuple[float, float, str]], out: Path, w: int, h: int, *, hook: str = "",
              watermark: str = "", intro_banner: str = "", intro_at: float = 0.0,
              ctas: list[tuple[float, float, str]] | None = None, accent: str = "&H0044D7FF&",
              chunk: int = 3) -> Path:
    """accent en formato ASS &HAABBGGRR& (amarillo cálido por defecto)."""
    portrait = h > w
    fs = 62 if portrait else 54
    margin_v = int(h * (0.20 if portrait else 0.10))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{fs},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,6,2,2,40,40,{margin_v},1
Style: Hook,DejaVu Sans,{int(fs*1.05)},&H00101010,&H00101010,{accent},{accent},1,0,0,0,100,100,0,0,3,14,0,8,50,50,{int(h*0.12)},1
Style: Mark,DejaVu Sans,{int(fs*0.42)},&H40303030,&H40303030,&H40FFFFFF,&H00000000,1,0,0,0,100,100,0,0,1,2,0,7,28,28,28,1
Style: Banner,DejaVu Sans,{int(fs*0.62)},&H00101010,&H00101010,{accent},{accent},1,0,0,0,100,100,0,0,3,12,0,1,40,40,{int(h*0.06)},1
Style: Cta,DejaVu Sans,{int(fs*0.7)},&H00FFFFFF,&H00FFFFFF,&H000000CC,&H000000CC,1,0,0,0,100,100,0,0,3,14,0,8,40,40,{int(h*0.30)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev: list[str] = []
    end_all = (words[-1][1] if words else 5.0) + 0.3
    if watermark:
        ev.append(f"Dialogue: 0,{_t(0)},{_t(end_all + 8)},Mark,,0,0,0,,{_esc(watermark)}")
    if hook:
        ev.append(f"Dialogue: 2,{_t(0)},{_t(min(2.8, end_all))},Hook,,0,0,0,,{{\\fad(80,200)}}{_esc(hook.upper())}")
    if intro_banner:
        ev.append(f"Dialogue: 2,{_t(intro_at)},{_t(intro_at + 3.2)},Banner,,0,0,0,,"
                  f"{{\\move(-600,{h - int(h*0.06)},40,{h - int(h*0.06)},0,350)\\fad(0,250)}}{_esc(intro_banner)}")
    for (a, b, txt) in ctas or []:
        ev.append(f"Dialogue: 3,{_t(a)},{_t(b)},Cta,,0,0,0,,{{\\fad(150,150)\\t(0,300,\\fscx112\\fscy112)\\t(300,600,\\fscx100\\fscy100)}}{_esc(txt)}")

    # subtítulos: grupos de `chunk` palabras; la palabra que suena va resaltada
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, (_, _, wd) in enumerate(words):
        cur.append(i)
        if len(cur) >= chunk or re.search(r"[.!?,:;]$", wd):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for g in groups:
        for j, i in enumerate(g):
            st = words[i][0]
            en = words[g[j + 1]][0] if j + 1 < len(g) else (words[i][1] + 0.05)
            parts = []
            for k in g:
                wd = _esc(words[k][2]).upper()
                parts.append(f"{{\\c{accent}\\fscx108\\fscy108}}{wd}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if k == i else wd)
            ev.append(f"Dialogue: 1,{_t(st)},{_t(en)},Cap,,0,0,0,,{' '.join(parts)}")
    out.write_text(header + "\n".join(ev) + "\n", encoding="utf-8")
    return out


def _lumi_overlay(w: int, h: int) -> str:
    size = int(min(w, h) * 0.30)
    y = int(h * (0.80 if h > w else 0.66))
    return f"[1:v]scale={size}:{size}[lu];[bg][lu]overlay=x={int(w*0.04)}:y='{y}+8*sin(2.4*t)'"


def stock_clip(src: Path, seconds: float, out: Path, w: int, h: int, lumi: Path | None = None) -> Path:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1"
    args = ["-stream_loop", "-1", "-i", str(src)]
    if lumi:
        args += ["-loop", "1", "-i", str(lumi), "-filter_complex", f"[0:v]{vf}[bg];{_lumi_overlay(w, h)}[v]", "-map", "[v]"]
    else:
        args += ["-vf", vf]
    _run([*args, "-t", f"{max(seconds, 0.6):.2f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
          "-pix_fmt", "yuv420p", str(out)])
    return out


def image_clip(img: Path, seconds: float, out: Path, w: int, h: int, k: int = 0, lumi: Path | None = None) -> Path:
    """Imagen con movimiento (Ken Burns): alterna zoom in/out y paneo para que nunca esté estática."""
    base = fit_image(img, int(w * 1.15), int(h * 1.15), out.with_name(out.stem + "_base.jpg"))
    frames = max(1, int(seconds * 30))
    if k % 3 == 0:
        z, x, y = "min(zoom+0.0011,1.15)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif k % 3 == 1:
        z, x, y = f"max(1.15-0.0011*on,1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.1", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    vf = f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps=30,setsar=1"
    args = ["-loop", "1", "-i", str(base)]
    if lumi:
        args += ["-loop", "1", "-i", str(lumi), "-filter_complex", f"[0:v]{vf}[bg];{_lumi_overlay(w, h)}[v]", "-map", "[v]"]
    else:
        args += ["-vf", vf]
    _run([*args, "-t", f"{max(seconds, 0.6):.2f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])
    return out


def card_png(lines: list[str], out: Path, w: int, h: int, sub: str = "", logo: Path | None = None) -> Path:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    top, bot = (7, 16, 31), (8, 70, 110)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    y = int(h * 0.30)
    if logo and logo.exists():
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((int(w * 0.45), int(h * 0.25)))
        img.paste(lg, ((w - lg.width) // 2, int(h * 0.08)), lg)
        y = int(h * 0.08) + lg.height + 40
    fb = font(int(w * 0.085))
    for line in lines:
        for l in wrap_lines(d, line, fb, int(w * 0.86)):
            d.text(((w - d.textlength(l, font=fb)) / 2, y), l, font=fb, fill=(183, 242, 26), stroke_width=4, stroke_fill="black")
            y += int(w * 0.11)
    if sub:
        fs = font(int(w * 0.05))
        y += 30
        for l in wrap_lines(d, sub, fs, int(w * 0.84)):
            d.text(((w - d.textlength(l, font=fs)) / 2, y), l, font=fs, fill="white")
            y += int(w * 0.07)
    img.save(out)
    return out


def music_track(seconds: float, out: Path, mood: str = "calm") -> Path:
    """Pista de fondo: un mp3 de assets/music/<mood>/ si existe; si no, una base ambiental generada."""
    folder = ASSETS / "music" / mood
    tracks = sorted(folder.glob("*.mp3")) if folder.exists() else []
    if not tracks and (ASSETS / "music").exists():
        tracks = sorted((ASSETS / "music").rglob("*.mp3"))
    if tracks:
        _run(["-stream_loop", "-1", "-i", str(random.choice(tracks)), "-t", f"{seconds:.2f}",
              "-af", f"afade=t=in:d=1,afade=t=out:st={max(0, seconds-2):.2f}:d=2", "-c:a", "libmp3lame", str(out)])
        return out
    notes = [261.63, 329.63, 392.0, 493.88] if mood == "calm" else [220.0, 277.18, 329.63, 440.0]
    inputs, labels = [], []
    for i, f in enumerate(notes):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=44100:duration={seconds:.2f}"]
        labels.append(f"[{i}:a]")
    trem = "0.25" if mood == "calm" else "2.0"
    fc = (f"{''.join(labels)}amix=inputs={len(notes)}:normalize=1,lowpass=f=900,tremolo=f={trem}:d=0.5,"
          f"aecho=0.8:0.7:600:0.25,afade=t=in:d=1.5,afade=t=out:st={max(0, seconds-2):.2f}:d=2,volume=0.6[a]")
    _run([*inputs, "-filter_complex", fc, "-map", "[a]", "-c:a", "libmp3lame", str(out)])
    return out


def final_mix(video: Path, voice: Path, music: Path | None, ass: Path, out: Path, total: float,
              music_vol: float = 0.12, preset: str = "veryfast") -> Path:
    fontsdir = str(FONT_PATH.parent)
    vf = f"[0:v]subtitles={ass}:fontsdir={fontsdir}[v]"
    if music:
        fc = (f"{vf};[1:a]apad,asplit=2[vo][sc];[2:a]volume={music_vol}[mu];"
              f"[mu][sc]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=400[duck];"
              f"[vo][duck]amix=inputs=2:duration=longest:normalize=0[a]")
        args = ["-i", str(video), "-i", str(voice), "-i", str(music), "-filter_complex", fc]
    else:
        args = ["-i", str(video), "-i", str(voice), "-filter_complex", f"{vf};[1:a]apad[a]"]
    _run([*args, "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", preset,
          "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    return out
