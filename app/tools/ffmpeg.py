"""Ensamblado de video con ffmpeg."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def scene_clip(image: Path, audio: Path, out: Path, subtitle: str, portrait: bool = True) -> Path:
    """Imagen + audio -> clip con zoom suave y subtítulo centrado."""
    w, h = (1080, 1920) if portrait else (1920, 1080)
    sub_y = "h*0.78" if portrait else "h*0.85"
    vf = (
        f"scale={w*1.1:.0f}:{h*1.1:.0f},"
        f"zoompan=z='min(zoom+0.0008,1.12)':d=100000:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,"
        f"drawtext=text='{_esc(subtitle)}':fontsize={56 if portrait else 44}:fontcolor=white:"
        f"borderw=4:bordercolor=black:x=(w-text_w)/2:y={sub_y}:line_spacing=10"
    )
    _run(["-loop", "1", "-i", str(image), "-i", str(audio), "-vf", vf, "-c:v", "libx264", "-tune",
          "stillimage", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)])
    return out


def concat(clips: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    _run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    lst.unlink(missing_ok=True)
    return out


def cut(src: Path, start: float, end: float, out: Path, to_portrait: bool = True, caption: str = "") -> Path:
    """Recorta un highlight de gameplay y lo lleva a 9:16 (crop centrado) con rótulo."""
    vf = []
    if to_portrait:
        vf.append("crop=ih*9/16:ih,scale=1080:1920")
    if caption:
        vf.append(f"drawtext=text='{_esc(caption)}':fontsize=60:fontcolor=white:borderw=4:bordercolor=black:"
                  f"x=(w-text_w)/2:y=h*0.12")
    args = ["-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(src)]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac", str(out)]
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
