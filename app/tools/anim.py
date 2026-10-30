"""ESTUDIO ANIMADOR de la oficina: animaciones técnicas propias, sin costo.

Cada plantilla dibuja cuadro por cuadro con PIL (paleta LCMC) y se codifica con ffmpeg.
Lumi aparece en escena con la expresión que pida el guion y un leve movimiento.

Plantillas (nombre -> parámetros opcionales):
  ciclo_refrigeracion  highlight: compresor|condensador|capilar|evaporador ; falla: texto
  lavadora             modo: lavado|centrifugado|desagüe|atorada
  minisplit            goteo: true/false ; flujo: frio|caliente
  multimetro           valor: "12.4" ; unidad: "Ω|V|A|µF" ; estado: ok|falla
  termometro           desde: 5 ; hasta: 18 ; unidad: "°C"
  lista                titulo: "3 causas" ; items: ["...", "..."]
  titulo               texto: "..." ; sub: "..."
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .ffmpeg import FFMPEG, font, wrap_lines

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# Paleta LCMC (README de marca)
NAVY = (7, 16, 31)
NAVY2 = (14, 32, 58)
BLUE = (8, 184, 240)
LIME = (183, 242, 26)
WHITE = (242, 244, 247)
HOT = (255, 92, 64)
COLD = (64, 170, 255)
GREY = (120, 140, 165)

TEMPLATES = ["ciclo_refrigeracion", "lavadora", "minisplit", "multimetro", "termometro", "lista", "titulo"]
LUMI_EXPRESSIONS = ["feliz", "analizando", "pensando", "sorpresa", "idea", "confirmacion", "advertencia", "listo",
                    "aprendizaje", "diagnostico", "solucion", "celebracion"]


def lumi_path(expr: str | None) -> Path | None:
    if not expr:
        return None
    p = ASSETS / "lumi" / f"{expr}.png"
    return p if p.exists() else (ASSETS / "lumi" / "feliz.png" if (ASSETS / "lumi" / "feliz.png").exists() else None)


def _bg(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), NAVY)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 4):
        t = y / h
        c = tuple(int(NAVY[i] + (NAVY2[i] - NAVY[i]) * t) for i in range(3))
        d.rectangle([0, y, w, y + 4], fill=c)
    # rejilla técnica sutil
    step = w // 12
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(18, 40, 70), width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(18, 40, 70), width=1)
    return img


def _label(d: ImageDraw.ImageDraw, xy, text: str, size: int, color=WHITE, anchor="mm", box=False):
    f = font(size)
    if box:
        tw = d.textlength(text, font=f)
        x, y = xy
        d.rounded_rectangle([x - tw / 2 - 14, y - size * 0.75, x + tw / 2 + 14, y + size * 0.75], radius=12,
                            fill=(7, 16, 31), outline=color, width=3)
    d.text(xy, text, font=f, fill=color, anchor=anchor, stroke_width=2, stroke_fill=NAVY)


def _ease(t: float) -> float:
    return 1 - (1 - min(max(t, 0), 1)) ** 3


# ---------------------------------------------------------------- plantillas

def _ciclo(d, w, h, t, p, s):
    hl = (p.get("highlight") or "").lower()
    x0, x1 = int(w * 0.20), int(w * 0.84)
    y0, y1 = int(h * 0.27), int(h * 0.55)
    path = [(x0, y1), (x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    seg_len = [math.dist(path[i], path[i + 1]) for i in range(4)]
    total = sum(seg_len)
    for i in range(4):
        col = HOT if i in (0, 1) else COLD if i == 3 else LIME
        d.line([path[i], path[i + 1]], fill=col, width=int(w * 0.018))
    # refrigerante circulando
    n = 22
    for k in range(n):
        pos = ((k / n) + t * 0.18) % 1.0 * total
        acc = 0
        for i in range(4):
            if pos <= acc + seg_len[i]:
                f = (pos - acc) / seg_len[i]
                x = path[i][0] + (path[i + 1][0] - path[i][0]) * f
                y = path[i][1] + (path[i + 1][1] - path[i][1]) * f
                col = HOT if i in (0, 1) else COLD if i == 3 else LIME
                r = w * 0.014
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)
                break
            acc += seg_len[i]
    comps = {"compresor": ((x0, y1), "COMPRESOR"), "condensador": (((x0 + x1) / 2, y0), "CONDENSADOR"),
             "capilar": ((x1, (y0 + y1) / 2), "CAPILAR"), "evaporador": (((x0 + x1) / 2, y1), "EVAPORADOR")}
    for key, ((cx, cy), name) in comps.items():
        on = key == hl
        pulse = 1 + (0.12 * math.sin(t * 6) if on else 0)
        r = w * 0.07 * pulse
        d.rounded_rectangle([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7], radius=14, fill=NAVY2,
                            outline=LIME if on else BLUE, width=6 if on else 3)
        _label(d, (cx, cy + r * 0.7 + s * 0.6), name, int(s * 0.55), LIME if on else WHITE)
    if p.get("falla"):
        _label(d, (w / 2, h * 0.20), str(p["falla"]).upper()[:28], int(s * 0.7), HOT, box=True)
    _label(d, (w * 0.30, (y0 + y1) / 2), "ALTA", int(s * 0.5), HOT)
    _label(d, (w * 0.70, (y0 + y1) / 2 + s), "BAJA", int(s * 0.5), COLD)


def _lavadora(d, w, h, t, p, s):
    modo = (p.get("modo") or "lavado").lower()
    cx, cy = w / 2, h * 0.42
    bw, bh = w * 0.62, w * 0.78
    d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], radius=28, fill=(230, 236, 242),
                        outline=BLUE, width=6)
    d.rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy - bh / 2 + bh * 0.16], fill=(200, 210, 222))
    for i in range(3):
        d.ellipse([cx - bw * 0.36 + i * bw * 0.12, cy - bh * 0.43, cx - bw * 0.30 + i * bw * 0.12, cy - bh * 0.37],
                  fill=LIME if i == int(t * 2) % 3 else GREY)
    R = bw * 0.36
    dc = (cx, cy + bh * 0.06)
    d.ellipse([dc[0] - R, dc[1] - R, dc[0] + R, dc[1] + R], fill=NAVY2, outline=(90, 100, 115), width=10)
    speed = {"centrifugado": 9.0, "lavado": 2.2, "desagüe": 1.0, "atorada": 0.0}.get(modo, 2.2)
    ang = t * speed + (0.25 * math.sin(t * 20) if modo == "atorada" else 0)
    if modo in ("lavado", "desagüe"):
        lvl = 0.35 if modo == "lavado" else max(0.0, 0.35 - t * 0.12)
        wy = dc[1] + R - 2 * R * lvl
        pts = [(dc[0] - R + x, wy + 6 * math.sin(x / 18 + t * 5)) for x in range(0, int(2 * R) + 1, 8)]
        poly = pts + [(dc[0] + R, dc[1] + R), (dc[0] - R, dc[1] + R)]
        mask = Image.new("L", d.im.size, 0)
        ImageDraw.Draw(mask).polygon(poly, fill=255)
        circ = Image.new("L", d.im.size, 0)
        ImageDraw.Draw(circ).ellipse([dc[0] - R + 8, dc[1] - R + 8, dc[0] + R - 8, dc[1] + R - 8], fill=255)
        from PIL import ImageChops
        m = ImageChops.multiply(mask, circ)
        d._image.paste(Image.new("RGB", d.im.size, (40, 120, 220)), (0, 0), m)
    for k in range(5):
        a = ang + k * 2 * math.pi / 5
        rr = R * (0.72 if speed > 5 else 0.5 + 0.12 * math.sin(a * 2))
        x, y = dc[0] + rr * math.cos(a), dc[1] + rr * math.sin(a)
        col = [HOT, LIME, BLUE, WHITE, (240, 180, 60)][k]
        d.ellipse([x - R * 0.13, y - R * 0.1, x + R * 0.13, y + R * 0.1], fill=col)
    titles = {"centrifugado": "CENTRIFUGADO", "lavado": "LAVADO", "desagüe": "DESAGÜE", "atorada": "¡TAMBOR ATORADO!"}
    _label(d, (w / 2, h * 0.15), titles.get(modo, modo.upper()), int(s * 0.75), HOT if modo == "atorada" else LIME, box=True)


def _minisplit(d, w, h, t, p, s):
    goteo = str(p.get("goteo", "true")).lower() in ("true", "1", "si", "sí")
    caliente = (p.get("flujo") or "frio") == "caliente"
    x0, x1 = w * 0.10, w * 0.90
    y0, y1 = h * 0.24, h * 0.36
    d.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=(236, 240, 245), outline=BLUE, width=5)
    d.line([(x0 + 30, y1 - 22), (x1 - 30, y1 - 22)], fill=GREY, width=8)
    d.ellipse([x1 - 60, y0 + 20, x1 - 40, y0 + 40], fill=LIME)
    col = HOT if caliente else COLD
    for k in range(6):
        off = (t * 0.9 + k / 6) % 1.0
        y = y1 + off * h * 0.26
        xs = x0 + (x1 - x0) * (0.15 + 0.14 * k)
        amp = 18 * math.sin(off * 6 + k)
        d.line([(xs + amp, y), (xs + amp + 40, y + 30)], fill=col + (0,) if False else col, width=6)
    if goteo:
        for k in range(3):
            ph = (t * 0.8 + k / 3) % 1.0
            dx = x0 + (x1 - x0) * (0.25 + 0.25 * k)
            dy = y1 + ph * h * 0.29
            r = w * 0.018
            d.polygon([(dx, dy - r * 2), (dx - r, dy), (dx + r, dy)], fill=(70, 160, 255))
            d.ellipse([dx - r, dy - r, dx + r, dy + r], fill=(70, 160, 255))
        d.rounded_rectangle([w * 0.18, h * 0.66, w * 0.82, h * 0.69], radius=10, fill=(40, 120, 220))
        _label(d, (w / 2, h * 0.16), "GOTEO", int(s * 0.75), HOT, box=True)
    else:
        _label(d, (w / 2, h * 0.16), "AIRE " + ("CALIENTE" if caliente else "FRÍO"), int(s * 0.75), LIME, box=True)


def _multimetro(d, w, h, t, p, s):
    val = str(p.get("valor", "0.0"))
    unidad = str(p.get("unidad", "Ω"))
    ok = (p.get("estado") or "ok") == "ok"
    cx, cy = w / 2, h * 0.38
    mw, mh = w * 0.56, w * 0.80
    d.rounded_rectangle([cx - mw / 2, cy - mh / 2, cx + mw / 2, cy + mh / 2], radius=34, fill=(250, 196, 30),
                        outline=NAVY, width=8)
    d.rounded_rectangle([cx - mw * 0.40, cy - mh * 0.40, cx + mw * 0.40, cy - mh * 0.12], radius=12, fill=(160, 190, 150))
    try:
        target = float(val.replace(",", "."))
        shown = target * _ease(t / 1.2)
        txt = f"{shown:.1f}" if abs(target) < 1000 else f"{int(shown)}"
    except ValueError:
        txt = val if t > 0.6 else "----"
    d.text((cx + mw * 0.34, cy - mh * 0.26), txt, font=font(int(s * 1.3)), fill=NAVY, anchor="rm")
    d.text((cx + mw * 0.36, cy - mh * 0.26), unidad, font=font(int(s * 0.6)), fill=NAVY, anchor="lm")
    d.ellipse([cx - mw * 0.24, cy + mh * 0.0, cx + mw * 0.24, cy + mh * 0.36], fill=(40, 40, 46), outline=NAVY, width=5)
    a = -math.pi / 2 + 0.6 * math.sin(1.0)
    d.line([(cx, cy + mh * 0.18), (cx + mw * 0.18 * math.cos(a), cy + mh * 0.18 + mw * 0.18 * math.sin(a))], fill=WHITE, width=8)
    for k, col in enumerate([(220, 40, 40), (30, 30, 30)]):
        px = cx + (k * 2 - 1) * mw * 0.16
        d.ellipse([px - 16, cy + mh * 0.43, px + 16, cy + mh * 0.47], fill=col)
        sway = 10 * math.sin(t * 2 + k)
        d.line([(px, cy + mh * 0.47), (px + (k * 2 - 1) * w * 0.12 + sway, h * 0.66)], fill=col, width=7)
    if t > 1.2:
        _label(d, (w / 2, h * 0.14), "✔ CORRECTO" if ok else "✖ FUERA DE RANGO", int(s * 0.75), LIME if ok else HOT, box=True)


def _termometro(d, w, h, t, p, s):
    a, b = float(p.get("desde", 5)), float(p.get("hasta", 18))
    u = p.get("unidad", "°C")
    cur = a + (b - a) * _ease(t / 2.0)
    cx = w * 0.42
    top, bot = h * 0.20, h * 0.58
    d.rounded_rectangle([cx - w * 0.06, top, cx + w * 0.06, bot], radius=40, fill=(230, 236, 242), outline=BLUE, width=5)
    d.ellipse([cx - w * 0.11, bot - w * 0.04, cx + w * 0.11, bot + w * 0.18], fill=HOT if cur > 10 else COLD)
    lo, hi = min(a, b) - 5, max(a, b) + 5
    lvl = bot - (cur - lo) / (hi - lo) * (bot - top - 30)
    d.rounded_rectangle([cx - w * 0.03, lvl, cx + w * 0.03, bot + 10], radius=20, fill=HOT if cur > 10 else COLD)
    _label(d, (w * 0.75, h * 0.42), f"{cur:.0f}{u}", int(s * 1.4), HOT if cur > 10 else COLD)


def _lista(d, w, h, t, p, s):
    titulo = str(p.get("titulo", ""))
    items = [str(x) for x in (p.get("items") or [])][:5]
    _label(d, (w / 2, h * 0.16), titulo.upper()[:30], int(s * 0.85), LIME, box=bool(titulo))
    per = max(0.5, 2.4 / max(1, len(items)))
    y = h * 0.28
    f = font(int(s * 0.62))
    for i, it in enumerate(items):
        k = _ease((t - i * per) / 0.35)
        if k <= 0:
            continue
        x = w * 0.10 - (1 - k) * w * 0.5
        d.ellipse([x, y, x + s * 0.9, y + s * 0.9], fill=BLUE)
        d.text((x + s * 0.45, y + s * 0.45), str(i + 1), font=font(int(s * 0.55)), fill=NAVY, anchor="mm")
        lines = wrap_lines(d, it, f, int(w * 0.70))[:2]
        for j, ln in enumerate(lines):
            d.text((x + s * 1.2, y + j * s * 0.75), ln, font=f, fill=WHITE, stroke_width=2, stroke_fill=NAVY)
        y += s * (1.0 + 0.75 * len(lines))


def _titulo(d, w, h, t, p, s):
    txt = str(p.get("texto", ""))
    sub = str(p.get("sub", ""))
    k = _ease(t / 0.5)
    f = font(int(s * 1.1))
    lines = wrap_lines(d, txt.upper(), f, int(w * 0.84))[:4]
    y = h * 0.38 - (1 - k) * 60
    for ln in lines:
        d.text((w / 2, y), ln, font=f, fill=LIME if ln == lines[0] else WHITE, anchor="mm", stroke_width=3, stroke_fill=NAVY)
        y += s * 1.3
    if sub:
        d.text((w / 2, y + s * 0.3), sub, font=font(int(s * 0.55)), fill=BLUE, anchor="mm")


DRAW = {"ciclo_refrigeracion": _ciclo, "lavadora": _lavadora, "minisplit": _minisplit, "multimetro": _multimetro,
        "termometro": _termometro, "lista": _lista, "titulo": _titulo}


REAL_MAX = 8.0                                   # segundos dibujados cuadro por cuadro; el resto se repite
LOOPING = {"ciclo_refrigeracion", "lavadora", "minisplit"}   # animaciones cíclicas: se repiten; las demás se congelan


def render(template: str, params: dict, seconds: float, out: Path, w: int, h: int, lumi: str | None = None,
           fps: int = 24) -> Path:
    """Escenas largas (videos de 8-10 min): dibuja solo los primeros REAL_MAX s y extiende sin redibujar."""
    if seconds <= REAL_MAX + 0.5:
        return _render(template, params, seconds, out, w, h, lumi, fps)
    part = out.with_name(out.stem + "_a.mp4")
    _render(template, params, REAL_MAX, part, w, h, lumi, fps)
    try:
        if template in LOOPING:
            cmd = ["-stream_loop", "-1", "-i", str(part), "-t", f"{seconds:.2f}", "-c", "copy", str(out)]
        else:
            cmd = ["-i", str(part), "-vf", f"tpad=stop_mode=clone:stop_duration={seconds - REAL_MAX + 0.5:.2f}",
                   "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "superfast", "-pix_fmt", "yuv420p", str(out)]
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-threads", "1", *cmd], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-300:])
    finally:
        part.unlink(missing_ok=True)
    return out


def _render(template: str, params: dict, seconds: float, out: Path, w: int, h: int, lumi: str | None = None,
            fps: int = 24) -> Path:
    draw = DRAW.get(template, _titulo)
    s = int(min(w, h) * 0.075)
    base = _bg(w, h)
    lumi_img = None
    lp = lumi_path(lumi)
    if lp:
        size = int(min(w, h) * 0.30)
        lumi_img = Image.open(lp).convert("RGBA").resize((size, size), Image.LANCZOS)
    frames = max(1, int(seconds * fps))
    proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
                             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-r", "30", "-c:v", "libx264", "-preset",
                             "veryfast", "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
    try:
        for i in range(frames):
            t = i / fps
            img = base.copy()
            d = ImageDraw.Draw(img)
            d._image = img
            draw(d, w, h, t, params or {}, s)
            if lumi_img is not None:
                bob = int(8 * math.sin(t * 2.4))
                x = int(w * 0.04)
                y = int(h * (0.80 if h > w else 0.66)) + bob
                img.paste(lumi_img, (x, y), lumi_img)
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg anim falló ({template})")
    return out


# ---------------------------------------------------------------- storyboard

APPLIANCE_TEMPLATES = {
    # qué plantillas son coherentes con cada equipo
    "refrigerador": {"ciclo_refrigeracion", "multimetro", "termometro", "lista", "titulo"},
    "minisplit": {"minisplit", "ciclo_refrigeracion", "multimetro", "termometro", "lista", "titulo"},
    "lavadora": {"lavadora", "multimetro", "lista", "titulo"},
    "secadora": {"multimetro", "termometro", "lista", "titulo"},
    "general": set(TEMPLATES),
}


def frame(template: str, params: dict, t: float, w: int, h: int, lumi: str | None = None) -> Image.Image:
    """Un solo cuadro de la animación (para el storyboard)."""
    draw = DRAW.get(template, _titulo)
    s = int(min(w, h) * 0.075)
    img = _bg(w, h)
    d = ImageDraw.Draw(img)
    d._image = img
    draw(d, w, h, t, params or {}, s)
    lp = lumi_path(lumi)
    if lp:
        size = int(min(w, h) * 0.30)
        lu = Image.open(lp).convert("RGBA").resize((size, size), Image.LANCZOS)
        img.paste(lu, (int(w * 0.04), int(h * (0.80 if h > w else 0.66))), lu)
    return img


def storyboard(scenes: list[dict], out: Path, portrait: bool = True, cols: int = 5) -> Path:
    """Hoja de storyboard: un cuadro por escena con número y frase narrada."""
    import json as _json
    tw, th = (216, 384) if portrait else (320, 180)
    pad, cap = 10, 58
    rows = (len(scenes) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + cap + pad) + pad), (10, 14, 24))
    d = ImageDraw.Draw(sheet)
    f = font(13)
    fb = font(15)
    for i, sc in enumerate(scenes):
        x = pad + (i % cols) * (tw + pad)
        y = pad + (i // cols) * (th + cap + pad)
        try:
            prm = _json.loads(sc.get("anim_params_json") or "{}")
        except Exception:
            prm = {}
        if sc.get("visual") == "anim" and sc.get("anim"):
            im = frame(sc["anim"], prm if isinstance(prm, dict) else {}, 1.6, 720 if portrait else 1280,
                       1280 if portrait else 720, sc.get("lumi") or None)
        else:
            im = frame("titulo", {"texto": f"[{sc.get('visual', '?')}] " + (sc.get('studio_clip') or sc.get('visual_prompt') or '')[:50]},
                       1.0, 720 if portrait else 1280, 1280 if portrait else 720, sc.get("lumi") or None)
        sheet.paste(im.resize((tw, th)), (x, y))
        d.rectangle([x, y, x + 34, y + 24], fill=(183, 242, 26))
        d.text((x + 17, y + 12), str(i + 1), font=fb, fill=(7, 16, 31), anchor="mm")
        lines = wrap_lines(d, sc.get("text", ""), f, tw - 6)[:3]
        for j, ln in enumerate(lines):
            d.text((x + 3, y + th + 4 + j * 17), ln, font=f, fill=(220, 226, 235))
    sheet.save(out, quality=82)
    return out
