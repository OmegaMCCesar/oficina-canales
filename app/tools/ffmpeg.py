"""Ensamblado de video con ffmpeg.

Usa el ffmpeg que trae imageio-ffmpeg (funciona en Render sin instalar nada).
Los textos se dibujan con PIL en PNG y se superponen (no depende de drawtext/fuentes del sistema).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover
    FFMPEG = "ffmpeg"

FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "DejaVuSans-Bold.ttf"


def font(size: int):
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _run(args: list[str]) -> None:
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr[-800:]}")


def wrap_lines(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def caption_png(text: str, w: int, h: int, out: Path, y_frac: float = 0.74, size: int = 58) -> Path:
    """PNG transparente del tamaño del video con el subtítulo centrado."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fnt = font(size)
    lines = wrap_lines(d, text, fnt, int(w * 0.86))[:4]
    lh = int(size * 1.25)
    y = int(h * y_frac) - (len(lines) * lh) // 2
    pad = 22
    box_w = max(d.textlength(l, font=fnt) for l in lines) if lines else 0
    d.rounded_rectangle([(w - box_w) / 2 - pad, y - pad / 2, (w + box_w) / 2 + pad, y + len(lines) * lh + pad / 2],
                        radius=24, fill=(0, 0, 0, 140))
    for l in lines:
        x = (w - d.textlength(l, font=fnt)) / 2
        d.text((x, y), l, font=fnt, fill="white", stroke_width=4, stroke_fill="black")
        y += lh
    img.save(out)
    return out


def fit_image(src: Path, w: int, h: int, out: Path) -> Path:
    """Recorta/escala la imagen al tamaño exacto (cover)."""
    img = Image.open(src).convert("RGB")
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
    left, top = (img.width - w) // 2, (img.height - h) // 2
    img.crop((left, top, left + w, top + h)).save(out, quality=92)
    return out


def scene_clip(image: Path, audio: Path, out: Path, subtitle: str, portrait: bool = True) -> Path:
    """Imagen + audio -> clip con zoom suave y subtítulo."""
    w, h = (1080, 1920) if portrait else (1920, 1080)
    base = fit_image(image, int(w * 1.1), int(h * 1.1), out.with_name(out.stem + "_base.jpg"))
    cap = caption_png(subtitle, w, h, out.with_name(out.stem + "_cap.png"), 0.76 if portrait else 0.85,
                      58 if portrait else 46)
    fc = (f"[0:v]zoompan=z='min(zoom+0.0007,1.10)':d=100000:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":s={w}x{h}:fps=30[bg];[bg][1:v]overlay=0:0[v]")
    _run(["-loop", "1", "-i", str(base), "-loop", "1", "-i", str(cap), "-i", str(audio),
          "-filter_complex", fc, "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)])
    return out


def concat(clips: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    _run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    lst.unlink(missing_ok=True)
    return out


def cut(src: Path, start: float, end: float, out: Path, to_portrait: bool = True, caption: str = "") -> Path:
    """Recorta un highlight de gameplay, lo lleva a 9:16 (crop centrado) y le pone rótulo."""
    w, h = (1080, 1920) if to_portrait else (1920, 1080)
    vf = "crop=ih*9/16:ih,scale=1080:1920" if to_portrait else "scale=1920:1080"
    args = ["-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(src)]
    if caption:
        cap = caption_png(caption, w, h, out.with_name(out.stem + "_cap.png"), 0.12, 60)
        args += ["-i", str(cap), "-filter_complex", f"[0:v]{vf}[v0];[v0][1:v]overlay=0:0[v]",
                 "-map", "[v]", "-map", "0:a?"]
    else:
        args += ["-vf", vf]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out)]
    _run(args)
    return out


def mix_voiceover(video: Path, voice: Path, out: Path, game_volume: float = 0.35) -> Path:
    _run(["-i", str(video), "-i", str(voice), "-filter_complex",
          f"[0:a]volume={game_volume}[g];[g][1:a]amix=inputs=2:duration=first[a]",
          "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(out)])
    return out


def frame_at(video: Path, seconds: float, out: Path) -> Path:
    _run(["-ss", f"{seconds:.2f}", "-i", str(video), "-frames:v", "1", str(out)])
    return out
