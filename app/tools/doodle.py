"""Estudio DOODLE: caricatura 2D sobre fondo blanco, contorno negro grueso y colores planos.

Estilo de referencia que eligió César: imágenes casi fijas que cambian cada 2-4 s, un protagonista
muy expresivo (Lumi redibujado) y objetos simples que ilustran lo que se dice, con recursos encima
(X roja, palomita, burbuja de pensamiento, chispas...).

Una TOMA (shot) es un dict:
  {"lumi": "preocupado", "lumi_pos": "derecha|izquierda|centro|ninguno",
   "prop": "refrigerador", "estado": "normal|falla|hielo|goteo|caliente|abierto|inflado|quemada|desconectado|frio",
   "overlay": "x|check|chispas|pregunta|alerta|burbuja|flecha|humo|ninguno",
   "burbuja": "capacitor", "texto": "≤3 palabras", "valor": "0 µF"}
Render barato: 3 cuadros clave por toma (objeto -> Lumi -> recursos "pop") + parpadeo, unidos con ffmpeg.
"""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .ffmpeg import FFMPEG, font

SS = 2   # supersampling para bordes suaves

BLACK = (22, 22, 28)
WHITE = (255, 255, 255)
PAPER = (255, 255, 255)
NAVY = (14, 28, 54)
CYAN = (63, 208, 255)
EYE = (205, 250, 255)
BLUE = (38, 146, 255)
LIME = (183, 242, 26)
RED = (232, 52, 52)
GREEN = (52, 190, 84)
YELLOW = (255, 214, 40)
ORANGE = (255, 140, 40)
GREY = (205, 210, 218)
DGREY = (110, 116, 128)
STEEL = (178, 188, 200)
ICE = (175, 225, 255)
WATER = (70, 170, 255)
COPPER = (205, 120, 60)

PROPS: dict = {}
OVERLAYS = ["x", "check", "chispas", "pregunta", "alerta", "burbuja", "flecha", "humo", "ninguno"]
EXPRESSIONS = ["feliz", "riendo", "sorprendido", "preocupado", "pensando", "enojado", "triste", "explicando", "celebrando"]
# nombres anteriores de Lumi -> expresión doodle
LEGACY = {"analizando": "pensando", "sorpresa": "sorprendido", "idea": "explicando", "confirmacion": "feliz",
          "advertencia": "preocupado", "listo": "feliz", "aprendizaje": "explicando", "diagnostico": "pensando",
          "solucion": "feliz", "celebracion": "celebrando", "hero": "feliz"}
# qué objetos son coherentes con cada equipo
APPLIANCE_PROPS = {
    "refrigerador": {"refrigerador", "compresor", "capacitor", "termometro", "multimetro", "ventilador", "resistencia",
                     "termostato", "tarjeta", "enchufe", "herramientas", "dinero", "reloj", "microfono", "foco"},
    "minisplit": {"minisplit", "compresor", "capacitor", "termometro", "multimetro", "ventilador", "tarjeta", "enchufe",
                  "herramientas", "dinero", "reloj", "microfono", "foco"},
    "lavadora": {"lavadora", "bomba", "multimetro", "tarjeta", "capacitor", "enchufe", "herramientas", "dinero", "reloj",
                 "microfono", "foco", "motor"},
    "secadora": {"secadora", "resistencia", "termostato", "multimetro", "tarjeta", "enchufe", "herramientas", "dinero",
                 "reloj", "microfono", "foco", "motor"},
}


def _prop(name):
    def deco(fn):
        PROPS[name] = fn
        return fn
    return deco


# ------------------------------------------------------------------ utilidades

def _ow(W, H):
    return max(6, int(min(W, H) * 0.0085)) * SS // 2 * 2 // 1


def _fit(box, aspect):
    """Rectángulo centrado dentro de box con relación ancho/alto = aspect."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if bw / bh > aspect:
        w, h = bh * aspect, bh
    else:
        w, h = bw, bw / aspect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _rr(d, xy, fill, ow, r=0.12):
    x0, y0, x1, y1 = xy
    d.rounded_rectangle(xy, radius=int(min(x1 - x0, y1 - y0) * r), fill=fill, outline=BLACK, width=ow)


def _line(d, pts, color, width, ow):
    """Trazo grueso con contorno negro (doble pasada)."""
    d.line(pts, fill=BLACK, width=int(width + ow * 1.6), joint="curve")
    for p in (pts[0], pts[-1]):
        r = (width + ow * 1.6) / 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=BLACK)
    d.line(pts, fill=color, width=int(width), joint="curve")
    for p in (pts[0], pts[-1]):
        r = width / 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def _drops(d, x0, x1, y, size, ow, n=3, color=WATER):
    for i in range(n):
        cx = x0 + (x1 - x0) * (i + 0.5) / n
        cy = y + (i % 2) * size * 1.3
        pts = [(cx, cy - size)] + [(cx + size * 0.6 * math.cos(a), cy + size * 0.6 * math.sin(a))
                                   for a in [math.radians(t) for t in range(-20, 201, 20)]]
        d.polygon(pts, fill=color, outline=BLACK, width=ow)


def _waves(d, x0, x1, y, h, color, ow, n=3):
    for i in range(n):
        cx = x0 + (x1 - x0) * (i + 0.5) / n
        pts = [(cx + math.sin(k / 3) * h * 0.18, y - k * h / 12) for k in range(13)]
        _line(d, pts, color, ow * 1.2, ow * 0.6)


def _frost(d, xy, ow):
    x0, y0, x1, y1 = xy
    rnd = random.Random(7)
    for _ in range(7):
        cx, cy = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
        r = (x1 - x0) * rnd.uniform(0.07, 0.13)
        d.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7], fill=ICE, outline=BLACK, width=max(2, ow // 2))


# ------------------------------------------------------------------ objetos

@_prop("refrigerador")
def p_refri(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.55)
    w, h = x1 - x0, y1 - y0
    body = (240, 243, 247)
    if estado == "abierto":
        _rr(d, (x0, y0, x1, y1), (225, 240, 255), ow, 0.06)
        for k in range(1, 4):
            yy = y0 + h * (0.35 + k * 0.15)
            d.line([(x0 + w * 0.08, yy), (x1 - w * 0.08, yy)], fill=BLACK, width=ow)
        d.ellipse([x0 + w * 0.4, y0 + h * 0.36, x0 + w * 0.6, y0 + h * 0.44], fill=YELLOW, outline=BLACK, width=ow // 2)
        _rr(d, (x1 - w * 0.02, y0 + h * 0.34, x1 + w * 0.45, y1 - h * 0.02), body, ow, 0.06)   # puerta abierta
    else:
        _rr(d, (x0, y0, x1, y1), body, ow, 0.08)
        d.line([(x0, y0 + h * 0.34), (x1, y0 + h * 0.34)], fill=BLACK, width=ow)
        for (a, b) in ((0.08, 0.26), (0.42, 0.62)):
            _rr(d, (x1 - w * 0.2, y0 + h * a, x1 - w * 0.12, y0 + h * b), DGREY, ow // 2, 0.4)
    d.rectangle([x0 + w * 0.08, y1, x0 + w * 0.2, y1 + h * 0.03], fill=BLACK)
    d.rectangle([x1 - w * 0.2, y1, x1 - w * 0.08, y1 + h * 0.03], fill=BLACK)
    if estado == "hielo":
        _frost(d, (x0 + w * 0.1, y0 + h * 0.05, x1 - w * 0.25, y0 + h * 0.3), ow)
    if estado == "goteo":
        _drops(d, x0 + w * 0.1, x1 - w * 0.1, y1 + h * 0.08, w * 0.07, ow // 2)
    if estado in ("caliente", "falla"):
        _waves(d, x0 + w * 0.1, x1 - w * 0.1, y0 - h * 0.02, h * 0.18, ORANGE, ow)
    if estado == "frio":
        _waves(d, x0 + w * 0.1, x1 - w * 0.1, y0 - h * 0.02, h * 0.18, CYAN, ow)


@_prop("lavadora")
def p_lavadora(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.85)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0, y0 + h * 0.18, x1, y1), (240, 243, 247), ow, 0.06)
    _rr(d, (x0 + w * 0.02, y0, x1 - w * 0.02, y0 + h * 0.2), (225, 230, 238), ow, 0.15)   # panel trasero
    for k in range(3):
        cx = x0 + w * (0.25 + k * 0.25)
        r = h * 0.05
        d.ellipse([cx - r, y0 + h * 0.1 - r, cx + r, y0 + h * 0.1 + r], fill=DGREY if k else BLUE, outline=BLACK, width=ow // 2)
    if estado == "abierto":
        _rr(d, (x0 + w * 0.1, y0 - h * 0.25, x1 - w * 0.1, y0 + h * 0.2), (200, 225, 245), ow, 0.1)  # tapa arriba
        d.ellipse([x0 + w * 0.18, y0 + h * 0.28, x1 - w * 0.18, y0 + h * 0.5], fill=(150, 165, 185), outline=BLACK, width=ow)
    else:
        d.line([(x0 + w * 0.06, y0 + h * 0.3), (x1 - w * 0.06, y0 + h * 0.3)], fill=BLACK, width=ow)
    if estado in ("goteo", "agua"):
        _drops(d, x0 + w * 0.1, x1 - w * 0.1, y1 + h * 0.08, w * 0.06, ow // 2)
    if estado == "falla":
        for k in (-1, 1):
            d.line([(x0 - w * 0.08 * k + (w if k < 0 else 0), y0 + h * 0.5), (x0 - w * 0.16 * k + (w if k < 0 else 0), y0 + h * 0.45)],
                   fill=BLACK, width=ow)


@_prop("secadora")
def p_secadora(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.85)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0, y0, x1, y1), (240, 243, 247), ow, 0.06)
    d.line([(x0, y0 + h * 0.2), (x1, y0 + h * 0.2)], fill=BLACK, width=ow)
    r = min(w, h) * 0.28
    cx, cy = (x0 + x1) / 2, y0 + h * 0.58
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(160, 175, 195), outline=BLACK, width=ow)
    d.ellipse([cx - r * 0.7, cy - r * 0.7, cx + r * 0.7, cy + r * 0.7], fill=(120, 135, 160), outline=BLACK, width=ow // 2)
    if estado in ("caliente", "falla"):
        _waves(d, x0 + w * 0.15, x1 - w * 0.15, y0 - h * 0.02, h * 0.2, ORANGE, ow)
    if estado == "frio":
        _waves(d, x0 + w * 0.15, x1 - w * 0.15, y0 - h * 0.02, h * 0.2, CYAN, ow)


@_prop("minisplit")
def p_minisplit(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 2.8)
    w, h = x1 - x0, y1 - y0
    cy = (y0 + y1) / 2 - h * 0.4
    yy0, yy1 = cy - h * 0.35, cy + h * 0.35
    _rr(d, (x0, yy0, x1, yy1), (244, 246, 250), ow, 0.3)
    d.line([(x0 + w * 0.08, yy1 - h * 0.18), (x1 - w * 0.08, yy1 - h * 0.18)], fill=BLACK, width=ow)
    d.ellipse([x1 - w * 0.1, yy0 + h * 0.15, x1 - w * 0.07, yy0 + h * 0.15 + w * 0.03], fill=GREEN, outline=BLACK, width=ow // 3)
    if estado == "goteo":
        _drops(d, x0 + w * 0.2, x1 - w * 0.2, yy1 + h * 0.4, w * 0.035, ow // 2, n=4)
    for state, color in (("frio", CYAN), ("caliente", ORANGE), ("falla", ORANGE)):
        if estado == state:
            for k in range(4):
                xx = x0 + w * (0.2 + k * 0.2)
                pts = [(xx + math.sin(j / 2.5) * w * 0.02, yy1 + h * 0.1 + j * h * 0.13) for j in range(9)]
                _line(d, pts, color, ow * 1.1, ow * 0.6)


@_prop("multimetro")
def p_multimetro(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.62)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0, y0, x1, y1), YELLOW, ow, 0.12)
    _rr(d, (x0 + w * 0.12, y0 + h * 0.08, x1 - w * 0.12, y0 + h * 0.32), (190, 214, 170), ow, 0.08)
    val = str(extra.get("valor") or ("OL" if estado == "falla" else "12.4"))
    f = font(int(h * 0.12))
    d.text(((x0 + x1) / 2, y0 + h * 0.2), val[:8], font=f, fill=BLACK, anchor="mm")
    r = w * 0.24
    cx, cy = (x0 + x1) / 2, y0 + h * 0.6
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(60, 60, 70), outline=BLACK, width=ow)
    d.line([(cx, cy), (cx + r * 0.7, cy - r * 0.5)], fill=WHITE, width=ow)
    for k, col in ((-1, RED), (1, BLACK)):
        px = cx + k * w * 0.2
        d.ellipse([px - w * 0.05, y1 - h * 0.12, px + w * 0.05, y1 - h * 0.04], fill=col, outline=BLACK, width=ow // 2)
        _line(d, [(px, y1 - h * 0.08), (px + k * w * 0.25, y1 + h * 0.08), (px + k * w * 0.5, y1 + h * 0.02)], col, ow, ow * 0.5)


@_prop("capacitor")
def p_capacitor(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.55)
    w, h = x1 - x0, y1 - y0
    top = y0 + h * 0.18
    body = (240, 200, 200) if estado in ("inflado", "falla") else STEEL
    _rr(d, (x0, top, x1, y1), body, ow, 0.18)
    if estado in ("inflado", "falla"):
        d.chord([x0 + w * 0.05, top - h * 0.12, x1 - w * 0.05, top + h * 0.12], 180, 360, fill=body, outline=BLACK, width=ow)
    else:
        d.ellipse([x0, top - h * 0.05, x1, top + h * 0.05], fill=(205, 212, 222), outline=BLACK, width=ow)
    for k in (-1, 0, 1):
        px = (x0 + x1) / 2 + k * w * 0.25
        _rr(d, (px - w * 0.05, y0, px + w * 0.05, top), (230, 190, 90), ow // 2, 0.2)
    d.text(((x0 + x1) / 2, y0 + h * 0.62), "µF", font=font(int(h * 0.1)), fill=BLACK, anchor="mm")


@_prop("compresor")
def p_compresor(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.0)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0, y1 - h * 0.12, x1, y1), DGREY, ow, 0.3)
    d.chord([x0 + w * 0.08, y0 + h * 0.15, x1 - w * 0.08, y1 + h * 0.45], 180, 360, fill=(40, 42, 50), outline=BLACK, width=ow)
    d.rectangle([x0 + w * 0.08, y0 + h * 0.6, x1 - w * 0.08, y1 - h * 0.12], fill=(40, 42, 50), outline=BLACK, width=ow)
    for k in (0.3, 0.55):
        _line(d, [(x0 + w * k, y0 + h * 0.35), (x0 + w * k, y0 + h * 0.08), (x0 + w * k - w * 0.15, y0)], COPPER, ow * 1.3, ow * 0.6)
    if estado in ("caliente", "falla"):
        _waves(d, x0 + w * 0.3, x1 - w * 0.1, y0 + h * 0.1, h * 0.3, ORANGE, ow)


@_prop("termometro")
def p_termometro(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.35)
    w, h = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2
    tw = w * 0.38
    _rr(d, (cx - tw / 2, y0, cx + tw / 2, y1 - w * 0.5), WHITE, ow, 0.5)
    hot = estado in ("caliente", "falla")
    lvl = 0.25 if estado == "frio" else 0.75 if hot else 0.45
    d.rectangle([cx - tw * 0.22, y0 + (y1 - y0 - w * 0.5) * (1 - lvl), cx + tw * 0.22, y1 - w * 0.4],
                fill=RED if hot or estado != "frio" else BLUE)
    r = w * 0.4
    d.ellipse([cx - r, y1 - 2 * r, cx + r, y1], fill=RED if estado != "frio" else BLUE, outline=BLACK, width=ow)
    if extra.get("valor"):
        d.text((x1 + w * 0.15, y0 + h * 0.35), str(extra["valor"])[:8], font=font(int(h * 0.11)), fill=BLACK, anchor="lm")


@_prop("enchufe")
def p_enchufe(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.3)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x1 - w * 0.32, y0 + h * 0.2, x1, y1 - h * 0.2), WHITE, ow, 0.15)   # contacto
    for k in (-1, 1):
        d.rectangle([x1 - w * 0.2 + k * w * 0.05 - w * 0.015, y0 + h * 0.42, x1 - w * 0.2 + k * w * 0.05 + w * 0.015, y0 + h * 0.56], fill=BLACK)
    off = w * 0.18 if estado == "desconectado" else 0
    px = x1 - w * 0.36 - off
    for k in (-1, 1):
        d.rectangle([px, y0 + h * 0.49 + k * h * 0.06 - h * 0.015, px + w * 0.08, y0 + h * 0.49 + k * h * 0.06 + h * 0.015], fill=STEEL, outline=BLACK, width=ow // 3)
    _rr(d, (px - w * 0.18, y0 + h * 0.35, px, y0 + h * 0.65), (60, 60, 70), ow, 0.2)
    _line(d, [(px - w * 0.18, y0 + h * 0.5), (x0 + w * 0.2, y0 + h * 0.5), (x0, y0 + h * 0.75)], (60, 60, 70), ow * 1.2, ow * 0.5)
    if estado == "desconectado":
        for k in (-1, 0, 1):
            d.line([(px + w * 0.12, y0 + h * 0.5 + k * h * 0.12), (px + w * 0.16, y0 + h * 0.5 + k * h * 0.16)], fill=BLACK, width=ow // 2)


@_prop("herramientas")
def p_herramientas(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.0)
    w, h = x1 - x0, y1 - y0
    _line(d, [(x0 + w * 0.15, y1 - h * 0.15), (x1 - w * 0.25, y0 + h * 0.25)], STEEL, ow * 2.2, ow * 0.6)
    d.ellipse([x1 - w * 0.38, y0 + h * 0.05, x1 - w * 0.08, y0 + h * 0.35], fill=STEEL, outline=BLACK, width=ow)
    d.ellipse([x1 - w * 0.29, y0 + h * 0.14, x1 - w * 0.17, y0 + h * 0.26], fill=PAPER, outline=BLACK, width=ow // 2)
    _line(d, [(x1 - w * 0.15, y1 - h * 0.15), (x0 + w * 0.35, y0 + h * 0.35)], STEEL, ow * 0.9, ow * 0.5)
    _rr(d, (x1 - w * 0.3, y1 - h * 0.32, x1 - w * 0.05, y1 - h * 0.05), RED, ow, 0.3)


@_prop("tarjeta")
def p_tarjeta(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.4)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0, y0, x1, y1), (40, 140, 80), ow, 0.05)
    for k in range(3):
        _rr(d, (x0 + w * (0.1 + k * 0.3), y0 + h * 0.2, x0 + w * (0.3 + k * 0.3), y0 + h * 0.45), (30, 30, 36), ow // 2, 0.08)
    for k in range(6):
        d.line([(x0 + w * 0.08, y0 + h * (0.55 + k * 0.06)), (x1 - w * 0.08, y0 + h * (0.55 + k * 0.06))], fill=(200, 170, 80), width=max(2, ow // 3))
    if estado in ("quemada", "falla"):
        d.ellipse([x0 + w * 0.55, y0 + h * 0.15, x0 + w * 0.85, y0 + h * 0.55], fill=(30, 25, 20), outline=BLACK, width=ow // 2)


@_prop("ventilador")
def p_ventilador(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.0)
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 238, 244), outline=BLACK, width=ow)
    for k in range(3):
        a = math.radians(k * 120 + 20)
        pts = [(cx, cy), (cx + r * 0.85 * math.cos(a - 0.35), cy + r * 0.85 * math.sin(a - 0.35)),
               (cx + r * 0.85 * math.cos(a + 0.25), cy + r * 0.85 * math.sin(a + 0.25))]
        d.polygon(pts, fill=BLUE, outline=BLACK, width=ow // 2)
    d.ellipse([cx - r * 0.15, cy - r * 0.15, cx + r * 0.15, cy + r * 0.15], fill=DGREY, outline=BLACK, width=ow // 2)


@_prop("motor")
def p_motor(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.3)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0 + w * 0.1, y0 + h * 0.2, x1 - w * 0.25, y1 - h * 0.1), (90, 100, 115), ow, 0.25)
    for k in range(4):
        d.line([(x0 + w * (0.25 + k * 0.12), y0 + h * 0.25), (x0 + w * (0.25 + k * 0.12), y1 - h * 0.15)], fill=BLACK, width=ow // 2)
    _rr(d, (x1 - w * 0.25, y0 + h * 0.45, x1, y0 + h * 0.6), STEEL, ow, 0.3)
    if estado == "falla":
        _waves(d, x0 + w * 0.2, x1 - w * 0.3, y0 + h * 0.15, h * 0.3, ORANGE, ow)


@_prop("bomba")
def p_bomba(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.2)
    w, h = x1 - x0, y1 - y0
    d.ellipse([x0 + w * 0.15, y0 + h * 0.2, x0 + w * 0.65, y1 - h * 0.1], fill=(240, 240, 240), outline=BLACK, width=ow)
    _rr(d, (x0 + w * 0.55, y0 + h * 0.35, x1, y0 + h * 0.6), (60, 60, 70), ow, 0.3)
    _rr(d, (x0, y0 + h * 0.45, x0 + w * 0.2, y0 + h * 0.6), STEEL, ow // 2, 0.3)
    if estado in ("falla", "atorada"):
        d.ellipse([x0 + w * 0.3, y0 + h * 0.4, x0 + w * 0.5, y0 + h * 0.6], fill=(120, 90, 60), outline=BLACK, width=ow // 2)


@_prop("resistencia")
def p_resistencia(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.6)
    w, h = x1 - x0, y1 - y0
    pts = [(x0 + w * 0.05, y0 + h * 0.5)]
    for k in range(9):
        pts.append((x0 + w * (0.12 + k * 0.095), y0 + h * (0.2 if k % 2 else 0.8)))
    pts.append((x1 - w * 0.05, y0 + h * 0.5))
    _line(d, pts, ORANGE if estado in ("caliente", "falla") else STEEL, ow * 1.6, ow * 0.6)
    if estado == "falla":
        d.line([(x0 + w * 0.5, y0 + h * 0.1), (x0 + w * 0.55, y0 + h * 0.9)], fill=PAPER, width=ow * 2)


@_prop("termostato")
def p_termostato(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.9)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0 + w * 0.15, y0, x1 - w * 0.15, y0 + h * 0.55), (240, 240, 244), ow, 0.15)
    r = w * 0.18
    cx, cy = (x0 + x1) / 2, y0 + h * 0.27
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DGREY, outline=BLACK, width=ow)
    _line(d, [(cx, y0 + h * 0.55), (cx, y0 + h * 0.75), (x0 + w * 0.15, y1)], (60, 60, 70), ow, ow * 0.5)


@_prop("dinero")
def p_dinero(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.9)
    w, h = x1 - x0, y1 - y0
    for k in (0.06, 0):
        _rr(d, (x0 + w * k, y0 + h * k * 2, x1 - w * (0.06 - k), y1 - h * (0.12 - k * 2)), (140, 210, 120), ow, 0.1)
    d.ellipse([x0 + w * 0.38, y0 + h * 0.22, x0 + w * 0.62, y0 + h * 0.7], fill=(110, 185, 95), outline=BLACK, width=ow // 2)
    d.text((x0 + w * 0.5, y0 + h * 0.46), "$", font=font(int(h * 0.3)), fill=BLACK, anchor="mm")


@_prop("reloj")
def p_reloj(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 1.0)
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PAPER, outline=BLACK, width=ow)
    for k in range(12):
        a = math.radians(k * 30)
        d.line([(cx + r * 0.8 * math.cos(a), cy + r * 0.8 * math.sin(a)), (cx + r * 0.9 * math.cos(a), cy + r * 0.9 * math.sin(a))], fill=BLACK, width=ow // 2)
    d.line([(cx, cy), (cx, cy - r * 0.6)], fill=BLACK, width=ow)
    d.line([(cx, cy), (cx + r * 0.45, cy + r * 0.1)], fill=RED, width=ow)


@_prop("foco")
def p_foco(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.7)
    w, h = x1 - x0, y1 - y0
    d.ellipse([x0, y0, x1, y0 + h * 0.7], fill=YELLOW, outline=BLACK, width=ow)
    _rr(d, (x0 + w * 0.3, y0 + h * 0.66, x1 - w * 0.3, y1), STEEL, ow, 0.2)
    for k in range(5):
        a = math.radians(200 + k * 35)
        cx, cy = (x0 + x1) / 2, y0 + h * 0.35
        d.line([(cx + w * 0.62 * math.cos(a), cy + w * 0.62 * math.sin(a)), (cx + w * 0.8 * math.cos(a), cy + w * 0.8 * math.sin(a))], fill=BLACK, width=ow)


@_prop("microfono")
def p_microfono(d, box, estado, ow, extra):
    x0, y0, x1, y1 = _fit(box, 0.55)
    w, h = x1 - x0, y1 - y0
    _rr(d, (x0 + w * 0.2, y0, x1 - w * 0.2, y0 + h * 0.55), (70, 75, 90), ow, 0.5)
    d.arc([x0, y0 + h * 0.2, x1, y0 + h * 0.75], 0, 180, fill=BLACK, width=ow)
    d.line([((x0 + x1) / 2, y0 + h * 0.75), ((x0 + x1) / 2, y1 - h * 0.05)], fill=BLACK, width=ow)
    d.line([(x0 + w * 0.2, y1 - h * 0.03), (x1 - w * 0.2, y1 - h * 0.03)], fill=BLACK, width=ow)


# ------------------------------------------------------------------ Lumi (redibujado)

def _teardrop(cx, cy, r):
    D = 1.5 * r
    phi = math.degrees(math.acos(r / D))
    pts = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
           for a in range(int(90 + phi), int(90 - phi + 360) + 1, 4)]
    return pts + [(cx, cy + D)]


def _ring(cx, cy, r, front: bool):
    a_, b_, tilt = 1.55 * r, 0.42 * r, math.radians(-16)
    pts = []
    for k in range(0, 181):
        t = math.radians(k * 2)
        if (math.sin(t) > 0) != front:
            continue
        x, y = a_ * math.cos(t), b_ * math.sin(t)
        pts.append((cx + x * math.cos(tilt) - y * math.sin(tilt), cy + r * 0.15 + x * math.sin(tilt) + y * math.cos(tilt), x))
    return pts


def _draw_ring_half(d, pts, ow, width):
    """Anillo de Lumi: contorno negro continuo y degradado azul -> lima de izquierda a derecha."""
    if len(pts) < 2:
        return
    a_ = 1.55 * (width / 0.16)
    xy = [p[:2] for p in pts]
    d.line(xy, fill=BLACK, width=int(width + ow * 1.8), joint="curve")
    for i in range(0, len(pts) - 1, 3):
        chunk = pts[i:i + 4]
        frac = max(0.0, min(1.0, (chunk[0][2] / a_ + 1) / 2))
        col = tuple(int(BLUE[j] + (LIME[j] - BLUE[j]) * frac) for j in range(3))
        d.line([c[:2] for c in chunk], fill=col, width=int(width), joint="curve")


def _bolt(d, cx, cy, r, ow):
    """Rayo de Lumi sobre la cabeza (como en su diseño original)."""
    s = r
    pts = [(cx + 0.30 * s, cy - 1.95 * s), (cx - 0.28 * s, cy - 1.18 * s), (cx + 0.02 * s, cy - 1.18 * s),
           (cx - 0.20 * s, cy - 0.70 * s), (cx + 0.36 * s, cy - 1.38 * s), (cx + 0.06 * s, cy - 1.38 * s),
           (cx + 0.30 * s, cy - 1.95 * s)]
    d.polygon(pts, fill=CYAN, outline=BLACK, width=ow)


def _arm(d, sx, sy, ex, ey, r, ow):
    _line(d, [(sx, sy), ((sx + ex) / 2, (sy + ey) / 2 + r * 0.05), (ex, ey)], NAVY, r * 0.22, ow * 0.9)
    hr = r * 0.16
    d.ellipse([ex - hr, ey - hr, ex + hr, ey + hr], fill=CYAN, outline=BLACK, width=ow)


def draw_lumi(d, cx, cy, r, expr: str, ow: int, point_to: tuple | None = None, blink: bool = False, flip: bool = False):
    """Lumi en estilo doodle. (cx, cy) = centro de la cabeza; r = radio."""
    expr = LEGACY.get(expr, expr) if expr else "feliz"
    if expr not in EXPRESSIONS:
        expr = "feliz"
    sgn = -1 if flip else 1
    # brazos detrás según la pose
    L, R = (cx - 0.92 * r, cy + 0.35 * r), (cx + 0.92 * r, cy + 0.35 * r)
    arms = []
    if expr == "celebrando":
        arms = [(L, (cx - 1.55 * r, cy - 0.7 * r)), (R, (cx + 1.55 * r, cy - 0.7 * r))]
    elif expr == "explicando" or point_to:
        tgt = point_to or (cx - sgn * 2.2 * r, cy - 0.1 * r)
        side = L if tgt[0] < cx else R
        dx, dy = tgt[0] - side[0], tgt[1] - side[1]
        n = math.hypot(dx, dy) or 1
        arms = [(side, (side[0] + dx / n * 0.85 * r, side[1] + dy / n * 0.85 * r))]
        other = R if side is L else L
        arms.append((other, (other[0] + (0.25 if other is R else -0.25) * r, other[1] + 0.55 * r)))
    elif expr == "pensando":
        arms = [(R if not flip else L, (cx + sgn * 0.35 * r, cy + 0.75 * r)), (L if not flip else R, (cx - sgn * 1.15 * r, cy + 0.9 * r))]
    elif expr in ("preocupado", "triste", "sorprendido"):
        arms = [(L, (cx - 1.2 * r, cy + 0.95 * r)), (R, (cx + 1.2 * r, cy + 0.95 * r))]
    elif expr == "enojado":
        arms = [(L, (cx - 0.7 * r, cy + 1.05 * r)), (R, (cx + 0.7 * r, cy + 1.05 * r))]
    else:
        arms = [(L, (cx - 1.25 * r, cy + 0.85 * r)), (R, (cx + 1.3 * r, cy + 0.55 * r))]
    back = _ring(cx, cy, r, front=False)
    _draw_ring_half(d, back, ow, r * 0.16)
    for a, b in arms:
        _arm(d, a[0], a[1], b[0], b[1], r, ow)
    _bolt(d, cx, cy, r, ow)
    d.polygon(_teardrop(cx, cy, r), fill=NAVY, outline=BLACK, width=ow)
    d.ellipse([cx - 0.55 * r, cy - 0.62 * r, cx - 0.25 * r, cy - 0.38 * r], fill=(40, 62, 100))   # brillo
    # cara
    ex, ey, es = 0.36 * r, -0.05 * r, 0.16 * r
    lw = max(3, int(r * 0.075))
    eyes_closed = blink or expr in ("feliz", "riendo", "celebrando")
    for k in (-1, 1):
        x = cx + k * ex
        if expr == "sorprendido" and not blink:
            d.ellipse([x - es * 1.15, cy + ey - es * 1.15, x + es * 1.15, cy + ey + es * 1.15], fill=EYE)
        elif expr in ("pensando",) and not blink:
            d.ellipse([x - es, cy + ey - es * 0.9, x + es, cy + ey + es * 0.9], fill=EYE)
            d.ellipse([x + es * 0.1, cy + ey - es * 0.85, x + es * 0.75, cy + ey - es * 0.2], fill=NAVY)
        elif expr in ("preocupado", "triste", "enojado", "explicando") and not blink:
            d.ellipse([x - es * 0.85, cy + ey - es, x + es * 0.85, cy + ey + es], fill=EYE)
        elif eyes_closed:
            d.arc([x - es, cy + ey - es, x + es, cy + ey + es], 200, 340, fill=EYE, width=lw)
        else:
            d.ellipse([x - es, cy + ey - es, x + es, cy + ey + es], fill=EYE)
        # cejas
        if expr in ("preocupado", "triste"):
            d.line([(x - k * es * 1.1, cy + ey - es * 1.5), (x + k * es * 0.9, cy + ey - es * 2.1)], fill=EYE, width=lw)
        if expr == "enojado":
            d.line([(x - k * es * 1.1, cy + ey - es * 2.2), (x + k * es * 0.9, cy + ey - es * 1.4)], fill=EYE, width=lw)
    my = cy + 0.38 * r
    mw = 0.32 * r
    if expr in ("riendo", "celebrando"):
        d.chord([cx - mw, my - mw * 0.55, cx + mw, my + mw * 0.75], 0, 180, fill=EYE)
        d.chord([cx - mw * 0.5, my + mw * 0.1, cx + mw * 0.5, my + mw * 0.7], 0, 180, fill=(230, 90, 110))
    elif expr == "sorprendido":
        d.ellipse([cx - mw * 0.35, my - mw * 0.25, cx + mw * 0.35, my + mw * 0.5], fill=EYE)
    elif expr in ("preocupado",):
        pts = [(cx - mw + k * mw / 4, my + (mw * 0.12 if k % 2 else -mw * 0.12)) for k in range(9)]
        d.line(pts, fill=EYE, width=lw)
    elif expr in ("enojado", "triste"):
        d.arc([cx - mw, my, cx + mw, my + mw * 1.1], 200, 340, fill=EYE, width=lw)
    elif expr == "pensando":
        d.line([(cx - mw * 0.2, my + mw * 0.1), (cx + mw * 0.6, my - mw * 0.05)], fill=EYE, width=lw)
    else:
        d.arc([cx - mw, my - mw * 0.9, cx + mw, my + mw * 0.5], 20, 160, fill=EYE, width=lw)
    # detalles por emoción
    if expr == "preocupado":
        sx, sy = cx + 1.05 * r, cy - 0.55 * r
        d.polygon([(sx, sy - r * 0.22)] + [(sx + r * 0.12 * math.cos(math.radians(a)), sy + r * 0.12 * math.sin(math.radians(a)))
                                           for a in range(-30, 211, 20)], fill=ICE, outline=BLACK, width=max(2, ow // 2))
    if expr == "triste":
        tx, ty = cx - ex, cy + ey + es * 1.8
        d.ellipse([tx - r * 0.06, ty, tx + r * 0.06, ty + r * 0.16], fill=ICE, outline=BLACK, width=max(2, ow // 3))
    if expr == "enojado":
        for k in range(3):
            a = math.radians(-60 + k * 25)
            px, py = cx + 1.25 * r * math.cos(a), cy - 1.0 * r + 1.25 * r * math.sin(a) * 0.3
            d.line([(px, py), (px + r * 0.18 * math.cos(a), py + r * 0.18 * math.sin(a))], fill=RED, width=ow)
    front = _ring(cx, cy, r, front=True)
    _draw_ring_half(d, front, ow, r * 0.16)


# ------------------------------------------------------------------ recursos (overlays)

def _x(d, box, ow):
    x0, y0, x1, y1 = box
    m = (x1 - x0) * 0.12
    for a, b in (((x0 + m, y0 + m), (x1 - m, y1 - m)), ((x1 - m, y0 + m), (x0 + m, y1 - m))):
        _line(d, [a, b], RED, ow * 3.2, ow)


def _check(d, box, ow):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    _line(d, [(x0 + w * 0.2, y0 + h * 0.55), (x0 + w * 0.42, y0 + h * 0.78), (x0 + w * 0.85, y0 + h * 0.2)], GREEN, ow * 3.2, ow)


def _spark(d, cx, cy, s, ow, color=YELLOW):
    pts = []
    for k in range(16):
        a = math.radians(k * 22.5)
        rr = s if k % 2 == 0 else s * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=color, outline=BLACK, width=max(2, ow // 2))


def _qmark(d, x, y, s, ow, ch="?", color=None):
    f = font(int(s))
    d.text((x, y), ch, font=f, fill=color or (255, 255, 255), anchor="mm", stroke_width=max(3, ow), stroke_fill=BLACK)


def _alerta(d, cx, cy, s, ow):
    pts = [(cx, cy - s), (cx + s * 1.05, cy + s * 0.75), (cx - s * 1.05, cy + s * 0.75)]
    d.polygon(pts, fill=YELLOW, outline=BLACK, width=ow)
    d.text((cx, cy + s * 0.12), "!", font=font(int(s * 1.1)), fill=BLACK, anchor="mm")


def _bubble(d, box, ow, inner: str | None, extra):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    rnd = random.Random(3)
    for k in range(9):
        a = math.radians(k * 40)
        cx, cy = (x0 + x1) / 2 + w * 0.36 * math.cos(a), (y0 + y1) / 2 + h * 0.3 * math.sin(a)
        r = min(w, h) * rnd.uniform(0.2, 0.26)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PAPER, outline=BLACK, width=ow)
    d.ellipse([x0 + w * 0.12, y0 + h * 0.12, x1 - w * 0.12, y1 - h * 0.12], fill=PAPER)
    if inner and inner in PROPS:
        PROPS[inner](d, (x0 + w * 0.22, y0 + h * 0.18, x1 - w * 0.22, y1 - h * 0.2), extra.get("estado_burbuja", "normal"), max(3, ow * 2 // 3), extra)


def _humo(d, box, ow):
    x0, y0, x1, y1 = box
    for k in range(4):
        cx = x0 + (x1 - x0) * (0.25 + k * 0.17)
        cy = y0 - (y1 - y0) * (0.05 + (k % 2) * 0.1)
        r = (x1 - x0) * 0.1
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(190, 190, 196), outline=BLACK, width=max(2, ow // 2))


def _arrow(d, frm, to, ow):
    _line(d, [frm, to], RED, ow * 2.2, ow)
    a = math.atan2(to[1] - frm[1], to[0] - frm[0])
    s = ow * 6
    pts = [to, (to[0] - s * math.cos(a - 0.5), to[1] - s * math.sin(a - 0.5)), (to[0] - s * math.cos(a + 0.5), to[1] - s * math.sin(a + 0.5))]
    d.polygon(pts, fill=RED, outline=BLACK, width=ow // 2)


def _sign(d, W, H, text, ow, portrait):
    f = font(int((H if not portrait else W) * (0.075 if not portrait else 0.08)))
    tw = d.textlength(text, font=f)
    fs = f.size
    cx = W / 2
    y = H * (0.07 if not portrait else 0.08)
    d.rounded_rectangle([cx - tw / 2 - fs * 0.6, y - fs * 0.25, cx + tw / 2 + fs * 0.6, y + fs * 1.25], radius=int(fs * 0.3),
                        fill=PAPER, outline=BLACK, width=ow)
    d.text((cx, y + fs * 0.5), text, font=f, fill=BLACK, anchor="mm")


# ------------------------------------------------------------------ composición

def _layout(W, H, shot):
    portrait = H > W
    pos = shot.get("lumi_pos") or "derecha"
    has_prop = bool(shot.get("prop") and shot["prop"] in PROPS)
    has_lumi = bool(shot.get("lumi")) and pos != "ninguno"
    if portrait:
        prop_box = (W * 0.12, H * 0.15, W * 0.88, H * 0.39) if has_lumi else (W * 0.08, H * 0.18, W * 0.92, H * 0.6)
        r = W * 0.105
        lumi_c = (W * 0.5, H * 0.535) if has_prop else (W * 0.5, H * 0.4)
        if not has_prop:
            r = W * 0.2
    else:
        left = pos == "izquierda"
        prop_box = (W * (0.42 if left else 0.06), H * 0.17, W * (0.94 if left else 0.58), H * 0.8) if has_lumi else \
            (W * 0.2, H * 0.15, W * 0.8, H * 0.82)
        r = H * 0.15
        lumi_c = ((W * 0.22 if left else W * 0.77), H * 0.47) if has_prop else (W * 0.5, H * 0.45)
        if not has_prop:
            r = H * 0.2
    return portrait, prop_box, (lumi_c[0], lumi_c[1], r), has_prop, has_lumi


def compose(shot: dict, w: int, h: int, stage: int = 3, blink: bool = False) -> Image.Image:
    """stage 1 = solo objeto, 2 = + Lumi, 3 = + recursos y texto."""
    W, H = w * SS, h * SS
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    ow = max(8, int(min(W, H) * 0.0115))
    portrait, pbox, (lx, ly, lr), has_prop, has_lumi = _layout(W, H, shot)
    estado = shot.get("estado") or "normal"
    extra = {"valor": shot.get("valor", ""), "estado_burbuja": shot.get("estado_burbuja", "normal")}
    # sombra suave en el piso
    if has_prop:
        x0, y0, x1, y1 = pbox
        d.ellipse([x0 + (x1 - x0) * 0.15, y1 - (y1 - y0) * 0.02, x1 - (x1 - x0) * 0.15, y1 + (y1 - y0) * 0.05], fill=(236, 238, 242))
        PROPS[shot["prop"]](d, pbox, estado, ow, extra)
    overlay = shot.get("overlay") or "ninguno"
    if stage >= 2 and has_lumi:
        if not portrait or not has_prop:
            d.ellipse([lx - lr * 1.2, ly + lr * 1.45, lx + lr * 1.2, ly + lr * 1.7], fill=(236, 238, 242))
        point = None
        if has_prop and (shot.get("lumi") in ("explicando",) or overlay == "flecha"):
            point = ((pbox[0] + pbox[2]) / 2, (pbox[1] + pbox[3]) / 2)
        draw_lumi(d, lx, ly, lr, shot.get("lumi"), ow, point_to=point, blink=blink, flip=lx < W / 2)
    if stage >= 3:
        x0, y0, x1, y1 = pbox
        if overlay == "x" and has_prop:
            _x(d, _fit(pbox, 1.0), ow)
        elif overlay == "check":
            s = min(x1 - x0, y1 - y0) * 0.45
            _check(d, (x1 - s * 0.9, y0, x1 + s * 0.1, y0 + s), ow)
        elif overlay == "chispas":
            rnd = random.Random(5)
            for _ in range(4):
                _spark(d, rnd.uniform(x0, x1), rnd.uniform(y0, y0 + (y1 - y0) * 0.6), min(W, H) * 0.035, ow)
        elif overlay == "pregunta":
            _qmark(d, lx + lr * 1.25, ly - lr * 1.5, lr * 1.2, ow, "?", YELLOW)
        elif overlay == "alerta":
            _alerta(d, lx - lr * 1.5 if lx > W / 2 else lx + lr * 1.5, ly - lr * 1.4, lr * 0.55, ow)
        elif overlay == "humo" and has_prop:
            _humo(d, pbox, ow)
        elif overlay == "flecha" and has_prop:
            to = ((x0 + x1) / 2 + (x1 - x0) * 0.2, (y0 + y1) / 2)
            frm = (to[0] + (x1 - x0) * 0.35, to[1] - (y1 - y0) * 0.35)
            _arrow(d, frm, to, ow)
        elif overlay == "burbuja" and has_lumi:
            bw = lr * 2.6
            bx = lx - bw * 1.05 if lx > W / 2 else lx + bw * 0.05
            by = ly - lr * 2.9
            if portrait:
                bx, by = lx + lr * 0.2, ly - lr * 3.0
            bx = max(ow * 2, min(W - bw - ow * 2, bx))
            by = max(ow * 2, by)
            for k, s in enumerate((0.12, 0.18)):
                cxx, cyy = lx + (bx + bw / 2 - lx) * (0.3 + k * 0.25), ly - lr * (1.3 + k * 0.4)
                d.ellipse([cxx - lr * s, cyy - lr * s, cxx + lr * s, cyy + lr * s], fill=PAPER, outline=BLACK, width=ow // 2)
            _bubble(d, (bx, by, bx + bw, by + bw * 0.8), ow, shot.get("burbuja"), extra)
        if shot.get("lumi") in ("celebrando", "celebracion") and has_lumi:
            rnd = random.Random(9)
            for _ in range(14):
                cx, cy = lx + rnd.uniform(-2.5, 2.5) * lr, ly + rnd.uniform(-2.2, 0.4) * lr
                c = rnd.choice([RED, YELLOW, LIME, BLUE, CYAN])
                d.rectangle([cx - ow, cy - ow * 0.6, cx + ow, cy + ow * 0.6], fill=c, outline=BLACK, width=max(1, ow // 4))
        txt = (shot.get("texto") or "").strip().upper()[:24]
        if txt:
            _sign(d, W, H, txt, ow, portrait)
    return img.resize((w, h), Image.LANCZOS)


def render(shots: list[dict], seconds: float, out: Path, w: int, h: int, fps: int = 30) -> Path:
    """Escena = 1-3 tomas repartidas en el tiempo. Cada toma: objeto -> Lumi -> recursos (pop) + parpadeo."""
    shots = [s for s in (shots or []) if isinstance(s, dict)] or [{"lumi": "feliz"}]
    per = max(0.9, seconds / len(shots))
    folder = out.parent
    seq: list[tuple[Path, float]] = []
    k = 0
    for i, sh in enumerate(shots):
        dur = per if i < len(shots) - 1 else max(0.6, seconds - per * (len(shots) - 1))
        steps = [(1, False, 0.1), (2, False, 0.1)] if sh.get("prop") else [(2, False, 0.12)]
        used = sum(s[2] for s in steps)
        full = dur - used
        if full > 2.6 and sh.get("lumi") and LEGACY.get(sh["lumi"], sh["lumi"]) not in ("feliz", "riendo", "celebrando"):
            a = full * 0.55
            steps += [(3, False, a), (3, True, 0.12), (3, False, full - a - 0.12)]
        else:
            steps += [(3, False, full)]
        for stage, blink, t in steps:
            p = folder / f"{out.stem}_k{k:03}.png"
            compose(sh, w, h, stage, blink).save(p)
            seq.append((p, max(0.04, t)))
            k += 1
    lst = folder / f"{out.stem}_lista.txt"
    lines = []
    for p, t in seq:
        lines += [f"file '{p.name}'", f"duration {t:.3f}"]
    lines.append(f"file '{seq[-1][0].name}'")
    lst.write_text("\n".join(lines) + "\n")
    try:
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-threads", "1", "-f", "concat", "-safe", "0", "-i", lst.name,
                            "-vf", f"fps={fps},format=yuv420p", "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "superfast",
                            out.name], cwd=str(folder), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-300:])
    finally:
        lst.unlink(missing_ok=True)
        for p, _ in seq:
            p.unlink(missing_ok=True)
    return out


def sheet(out: Path) -> Path:
    """Hoja de muestra: expresiones de Lumi + objetos (para que César revise el estilo)."""
    tiles = [{"lumi": e, "lumi_pos": "centro"} for e in EXPRESSIONS]
    tiles += [{"prop": p, "lumi": "", "estado": "normal"} for p in PROPS]
    cw, ch = 320, 240
    cols = 6
    rows = (len(tiles) + cols - 1) // cols
    S = Image.new("RGB", (cols * cw, rows * (ch + 30)), (245, 246, 248))
    d = ImageDraw.Draw(S)
    for i, t in enumerate(tiles):
        im = compose(t, 640, 480).resize((cw, ch))
        x, y = (i % cols) * cw, (i // cols) * (ch + 30)
        S.paste(im, (x, y))
        d.text((x + cw / 2, y + ch + 15), t.get("lumi") or t.get("prop"), font=font(18), fill=BLACK, anchor="mm")
    S.save(out, quality=88)
    return out
