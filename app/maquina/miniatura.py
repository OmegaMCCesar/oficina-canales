"""Miniatura de YouTube (1280x720) con la plantilla de LCMC:
falla en letras enormes + aparato dibujado con la zona del problema + Lumi grande.
"""
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import marca as M
from .render import F, crear_fondo, resplandor, ajustar

W, H = 1280, 720
ROJO = (255, 59, 48)

APARATOS = {
    "lavadora": ["lavadora", "lavasecadora"],
    "secadora": ["secadora"],
    "refrigerador": ["refri", "refrigerador", "nevera", "heladera", "congelador", "frigobar"],
    "minisplit": ["minisplit", "mini split", "aire acondicionado", "clima"],
    "microondas": ["microondas"],
}


def detectar_aparato(texto: str) -> str:
    t = texto.lower()
    if "lavasecadora" in t:
        return "lavadora"
    for ap, claves in APARATOS.items():
        if any(c in t for c in claves):
            return ap
    return "otro"


def datos_miniatura(guion: dict) -> dict:
    """Usa guion['miniatura'] si la IA lo dio; si no, lo deduce del título."""
    m = dict(guion.get("miniatura") or {})
    titulo = guion.get("titulo", "")
    if not m.get("texto"):
        base = re.split(r"[:|¿?]", titulo)[0].strip() or titulo
        m["texto"] = " ".join(base.split()[:4])
    if not m.get("resaltar"):
        con_numero = [p for p in m["texto"].split() if re.search(r"\d", p)]
        m["resaltar"] = con_numero[0] if con_numero else m["texto"].split()[-1]
    m.setdefault("subtexto", "")
    m.setdefault("aparato", detectar_aparato(titulo + " " + m["texto"]))
    m.setdefault("pose", "sorpresa")
    codigo = re.search(r"\b([0-9][A-Z]{1,2}|[A-Z]{1,2}[0-9]{1,2}|[0-9]{1,2}[A-Z])\b", titulo.upper() + " " + m["texto"].upper())
    m.setdefault("codigo", codigo.group(1) if codigo else "")
    return m


# ---------------------------------------------------------------- aparatos dibujados
def _cuerpo(d, caja, r=34):
    x1, y1, x2, y2 = caja
    d.rounded_rectangle((x1 + 10, y1 + 14, x2 + 10, y2 + 14), r, fill=(0, 0, 0, 90))  # sombra
    d.rounded_rectangle(caja, r, fill=(236, 241, 248), outline=(255, 255, 255), width=4)


def _pantalla(d, caja, codigo):
    d.rounded_rectangle(caja, 12, fill=(10, 18, 30))
    if codigo:
        f = F(int((caja[3] - caja[1]) * 0.78))
        d.text(((caja[0] + caja[2]) / 2, (caja[1] + caja[3]) / 2), codigo, font=f, fill=ROJO, anchor="mm")


def dibujar_aparato(tipo: str, tam: tuple, codigo: str = ""):
    """Devuelve (imagen RGBA, zona del problema (x, y, radio))."""
    w, h = tam
    im = Image.new("RGBA", (w + 30, h + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if tipo in ("lavadora", "secadora"):
        _cuerpo(d, (0, 0, w, h))
        d.rounded_rectangle((0, 0, w, int(h * 0.2)), 34, fill=(214, 222, 234))
        d.rectangle((0, int(h * 0.12), w, int(h * 0.2)), fill=(214, 222, 234))
        pant = (int(w * 0.52), int(h * 0.045), int(w * 0.9), int(h * 0.155))
        _pantalla(d, pant, codigo)
        for i in range(3):
            cx = int(w * (0.12 + i * 0.11))
            d.ellipse((cx - 14, int(h * 0.1) - 14, cx + 14, int(h * 0.1) + 14), fill=(160, 172, 190))
        cx, cy, r = w / 2, h * 0.6, w * 0.34
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(180, 190, 205))
        r2 = r * 0.8
        vidrio = (30, 70, 140) if tipo == "lavadora" else (60, 60, 80)
        d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), fill=vidrio)
        d.chord((cx - r2, cy - r2, cx + r2, cy + r2), 200, 290, fill=(90, 140, 210))
        zona = ((pant[0] + pant[2]) / 2, (pant[1] + pant[3]) / 2, (pant[2] - pant[0]) * 0.7) if codigo \
            else (cx, cy, r * 1.05)
    elif tipo == "refrigerador":
        _cuerpo(d, (0, 0, w, h), 28)
        corte = int(h * 0.36)
        d.line((10, corte, w - 10, corte), fill=(170, 180, 196), width=6)
        for y1, y2 in ((int(h * 0.08), int(h * 0.3)), (int(h * 0.42), int(h * 0.7))):
            d.rounded_rectangle((int(w * 0.8), y1, int(w * 0.87), y2), 8, fill=(170, 180, 196))
        # copos arriba (frío), termómetro rojo abajo (tibio)
        import math
        sx, sy, sr = w * 0.4, corte * 0.5, corte * 0.3
        for k in range(3):
            a = math.pi / 3 * k
            d.line((sx - sr * math.cos(a), sy - sr * math.sin(a), sx + sr * math.cos(a), sy + sr * math.sin(a)),
                   fill=(0, 170, 230), width=10)
        tx, ty = w * 0.4, h * 0.68
        d.rounded_rectangle((tx - 14, ty - 80, tx + 14, ty + 40), 14, fill=(255, 255, 255), outline=(150, 160, 175), width=3)
        d.ellipse((tx - 28, ty + 20, tx + 28, ty + 76), fill=ROJO)
        d.rectangle((tx - 7, ty - 40, tx + 7, ty + 30), fill=ROJO)
        zona = (w * 0.45, h * 0.66, w * 0.42)
    elif tipo == "minisplit":
        hh = int(h * 0.42)
        _cuerpo(d, (0, 0, w, hh), 40)
        d.rounded_rectangle((int(w * 0.06), int(hh * 0.68), int(w * 0.94), int(hh * 0.86)), 10, fill=(170, 180, 196))
        _pantalla(d, (int(w * 0.72), int(hh * 0.2), int(w * 0.9), int(hh * 0.46)), codigo)
        for i, x in enumerate((0.3, 0.5, 0.7)):
            gx, gy = w * x, hh + 70 + i % 2 * 50
            d.polygon([(gx, gy - 34), (gx - 22, gy + 6), (gx + 22, gy + 6)], fill=(0, 170, 230))
            d.ellipse((gx - 22, gy - 14, gx + 22, gy + 30), fill=(0, 170, 230))
        zona = (w * 0.5, hh + 100, w * 0.4)
    elif tipo == "microondas":
        hh = int(h * 0.62)
        _cuerpo(d, (0, 0, w, hh), 26)
        d.rounded_rectangle((int(w * 0.06), int(hh * 0.1), int(w * 0.7), int(hh * 0.9)), 16, fill=(30, 40, 60))
        _pantalla(d, (int(w * 0.75), int(hh * 0.12), int(w * 0.94), int(hh * 0.3)), codigo)
        zona = (w * 0.38, hh * 0.5, w * 0.36)
    else:  # herramienta genérica
        d.ellipse((w * 0.1, h * 0.1, w * 0.9, h * 0.9), fill=(236, 241, 248))
        f = F(int(w * 0.5))
        d.text((w / 2, h / 2), "?", font=f, fill=M.AZUL, anchor="mm")
        zona = (w / 2, h / 2, w * 0.45)
    return im, zona


def circulo_alerta(d, zona, grosor=12):
    x, y, r = zona
    for k, a in ((grosor + 10, 70), (grosor, 255)):
        d.ellipse((x - r, y - r, x + r, y + r), outline=(*ROJO, a), width=k)


# ---------------------------------------------------------------- composición
def texto_grande(m):
    """Capa con la frase principal; la palabra a resaltar va en verde lima."""
    texto = m["texto"].upper()
    resaltar = m["resaltar"].upper()
    f, lineas = ajustar(texto, 138, 760, 2)
    alto = int(f.size * 1.08)
    im = Image.new("RGBA", (760, alto * len(lineas) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lineas):
        x = 0
        for p in ln.split():
            color = M.LIMA if resaltar and resaltar in p else M.BLANCO
            d.text((x, i * alto), p, font=f, fill=color, stroke_width=8, stroke_fill=(0, 6, 18))
            x += f.getlength(p + " ")
    return im


def generar_miniatura(guion: dict, destino: Path) -> Path:
    m = datos_miniatura(guion)
    fondo = crear_fondo(W, H).crop((60, 40, 60 + W, 40 + H))
    lienzo = Image.new("RGBA", (W, H))
    lienzo.paste(fondo, (0, 0))

    # franja diagonal de energía detrás del aparato
    franja = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(franja).polygon([(760, 0), (1010, 0), (790, H), (540, H)], fill=(*M.AZUL, 70))
    lienzo.alpha_composite(franja.filter(ImageFilter.GaussianBlur(30)))

    # aparato a la derecha con su zona marcada
    ap, zona = dibujar_aparato(m["aparato"], (330, 470), m.get("codigo", ""))
    ax, ay = 880, 110
    lienzo.alpha_composite(ap, (ax, ay))
    d = ImageDraw.Draw(lienzo)
    if m["aparato"] != "otro":
        circulo_alerta(d, (ax + zona[0], ay + zona[1], zona[2]))

    # Lumi grande al frente, entre el texto y el aparato
    tam = 400
    glow = resplandor(int(tam * 1.3), M.CIAN)
    lx, ly = 470, 345
    lienzo.alpha_composite(glow, (lx - glow.width // 2 + tam // 2, ly - glow.height // 2 + tam // 2))
    ruta = M.POSES_DIR / f"{m['pose']}.png"
    lumi = Image.open(ruta if ruta.exists() else M.POSES_DIR / "sorpresa.png").convert("RGBA").resize((tam, tam), Image.LANCZOS)
    lienzo.alpha_composite(lumi, (lx, ly))

    # texto principal y subtítulo
    t = texto_grande(m)
    lienzo.alpha_composite(t, (48, 95))
    if m["subtexto"]:
        f = F(46)
        sub = m["subtexto"].upper()
        ancho = int(f.getlength(sub)) + 56
        y = 95 + t.height + 6
        d.rounded_rectangle((52, y, 52 + ancho, y + 70), 35, fill=M.CIAN)
        d.text((52 + ancho / 2, y + 35), sub, font=f, fill=(0, 20, 40), anchor="mm")

    # marca
    d.text((52, 28), M.MARCA_1, font=F(40), fill=M.CIAN)
    d.text((52 + F(40).getlength(M.MARCA_1) + 14, 40), M.MARCA_2, font=F(26, "Medium"), fill=M.GRIS)

    destino.parent.mkdir(parents=True, exist_ok=True)
    lienzo.convert("RGB").save(destino, "JPEG", quality=92)
    return destino
