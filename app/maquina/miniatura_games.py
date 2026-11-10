"""FORMATO DE MINIATURAS · LCMC Games (1280x720).

Una sola gramática visual para todo el canal, con 3 composiciones:

  heroe    El héroe recortado GRANDE a un lado (sale del cuadro), el texto del otro.
           Guías, jugadas, «X imparable», builds.
  vs       Dos héroes enfrentados en diagonal con «VS» en medio. Counters y matchups.
  mascota  La mascota del canal reaccionando en grande + un ícono (flecha, alerta, corona).
           Trucos, errores, subir de rango, tops, Magic Chess.

Capas, de atrás hacia adelante:
  fondo del tema (degradado + arte del héroe teñido) → rayos de luz → relámpagos → chispas →
  sujeto con contorno brillante → acento → TÍTULO 3D (2 líneas: blanca + amarilla) → cinta de color →
  sello «LCMC Games» arriba a la izquierda → viñeta.

Reglas del texto (las aplica el Diseñador): línea 1 de 1-2 palabras, línea 2 de 1-2 palabras (la
palabra que vende va en amarillo), cinta de 2-4 palabras. Nunca repetir el título del video.
"""
from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1280, 720
AQUI = Path(__file__).resolve().parent.parent
FUENTES = AQUI / "assets" / "maquina" / "fuentes"
MASCOTA = AQUI / "assets" / "maquina" / "games"
TITULO_TTF = FUENTES / "LuckiestGuy-Regular.ttf"
CINTA_TTF = FUENTES / "LilitaOne-Regular.ttf"

# tema: (oscuro, medio, brillo del fondo, color de la cinta, color del relámpago)
TEMAS = {
    "azul":    ((6, 12, 48), (20, 60, 190), (60, 170, 255), (122, 46, 220), (140, 220, 255)),
    "rojo":    ((40, 4, 10), (150, 14, 30), (255, 70, 60), (215, 20, 40), (255, 150, 120)),
    "morado":  ((20, 6, 46), (90, 30, 170), (190, 110, 255), (122, 46, 220), (225, 180, 255)),
    "dorado":  ((26, 14, 4), (150, 80, 10), (255, 190, 60), (122, 46, 220), (255, 230, 150)),
}
AMARILLO = ((255, 236, 92), (255, 168, 0))      # degradado de la palabra que vende
BLANCO = ((255, 255, 255), (206, 222, 255))
NEGRO = (8, 8, 18)


def _f(ruta: Path, tam: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(ruta), tam)


def _rng(*semilla) -> random.Random:
    return random.Random(int(hashlib.md5("|".join(map(str, semilla)).encode()).hexdigest()[:8], 16))


# ------------------------------------------------------------------ fondo
def _degradado(c1, c2, w=W, h=H, radial=(0.62, 0.45)) -> Image.Image:
    """Degradado radial: brillo c2 en el centro dado, c1 en las orillas."""
    cx, cy = radial
    peq = Image.new("RGB", (w // 8, h // 8))
    px = peq.load()
    for y in range(peq.height):
        for x in range(peq.width):
            d = math.hypot(x / peq.width - cx, (y / peq.height - cy) * 0.8)
            t = max(0.0, min(1.0, d / 0.75)) ** 1.2
            px[x, y] = tuple(int(c2[i] * (1 - t) + c1[i] * t) for i in range(3))
    return peq.resize((w, h), Image.BICUBIC)


def _tenir(arte: Image.Image, oscuro, brillo) -> Image.Image:
    """Mapa de degradado: el arte toma los colores del tema (así todas las miniaturas combinan)."""
    gris = ImageOps.grayscale(arte)
    return ImageOps.colorize(gris, black=oscuro, white=brillo, mid=tuple((a + b) // 2 for a, b in zip(oscuro, brillo)))


def _cubrir(img: Image.Image, w=W, h=H) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))


def _rayos(centro, color, rng, n=14) -> Image.Image:
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    cx, cy = centro
    a0 = rng.random() * math.pi
    for k in range(n):
        a = a0 + k * 2 * math.pi / n
        ancho = 0.07 + rng.random() * 0.06
        r = 1600
        d.polygon([(cx, cy), (cx + r * math.cos(a - ancho), cy + r * math.sin(a - ancho)),
                   (cx + r * math.cos(a + ancho), cy + r * math.sin(a + ancho))], fill=(*color, 52))
    return capa.filter(ImageFilter.GaussianBlur(5))


def _relampago(d: ImageDraw.ImageDraw, x, y, x2, y2, rng, grosor):
    puntos = [(x, y)]
    pasos = 9
    for i in range(1, pasos):
        t = i / pasos
        puntos.append((x + (x2 - x) * t + rng.uniform(-38, 38), y + (y2 - y) * t + rng.uniform(-24, 24)))
    puntos.append((x2, y2))
    d.line(puntos, fill=(255, 255, 255, 255), width=grosor, joint="curve")
    # ramitas
    for i in range(2, pasos - 1, 3):
        bx, by = puntos[i]
        d.line([(bx, by), (bx + rng.uniform(-90, 90), by + rng.uniform(30, 110))], fill=(255, 255, 255, 220),
               width=max(1, grosor // 2))


def _energia(color, rng, zonas) -> Image.Image:
    """Relámpagos con brillo + chispas, solo en las zonas dadas (para no ensuciar el texto)."""
    nucleo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(nucleo)
    for (x1, y1, x2, y2) in zonas:
        for _ in range(2):
            _relampago(d, rng.uniform(x1, x2), y1, rng.uniform(x1, x2), y2, rng, rng.choice([5, 6, 8]))
    a = nucleo.getchannel("A")
    capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for radio, veces in ((28, 2), (10, 2), (4, 1)):
        brillo = Image.new("RGBA", (W, H), (*color, 0))
        brillo.putalpha(a.filter(ImageFilter.GaussianBlur(radio)).point(lambda v: min(255, v * 3)))
        for _ in range(veces):
            capa.alpha_composite(brillo)
    capa.alpha_composite(nucleo)
    # chispas
    ch = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(ch)
    for _ in range(90):
        x, y, r = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(1.2, 3.8)
        dc.ellipse((x - r, y - r, x + r, y + r), fill=(*color, int(rng.uniform(120, 255))))
    bok = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    db = ImageDraw.Draw(bok)
    for _ in range(14):
        x, y, r = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(14, 40)
        db.ellipse((x - r, y - r, x + r, y + r), fill=(*color, int(rng.uniform(40, 90))))
    capa.alpha_composite(bok.filter(ImageFilter.GaussianBlur(6)))
    capa.alpha_composite(ch.filter(ImageFilter.GaussianBlur(0.8)))
    return capa


def _vineta(img: Image.Image) -> Image.Image:
    m = Image.new("L", (W // 4, H // 4), 0)
    px = m.load()
    for y in range(m.height):
        for x in range(m.width):
            d = math.hypot(x / m.width - 0.5, y / m.height - 0.5) / 0.7
            px[x, y] = int(255 * min(1, max(0, d - 0.55) / 0.45) * 0.75)
    m = m.resize((W, H), Image.BICUBIC)
    negro = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    negro.putalpha(m)
    img.alpha_composite(negro)
    return img


# ------------------------------------------------------------------ sujetos
def _tiene_recorte(img: Image.Image) -> bool:
    if img.mode != "RGBA":
        return False
    a = img.getchannel("A")
    return a.getextrema()[0] < 20 and sum(1 for v in a.resize((32, 32)).getdata() if v < 20) > 60


def _preparar_sujeto(img: Image.Image, alto: int, lado: str) -> Image.Image:
    """Recorte con transparencia → tal cual. Arte rectangular → se desvanece hacia el lado del texto."""
    img = img.convert("RGBA")
    if not _tiene_recorte(img):
        bbox = img.getbbox() or (0, 0, img.width, img.height)
        img = img.crop(bbox)
        m = Image.new("L", img.size, 255)
        dm = ImageDraw.Draw(m)
        ancho = int(img.width * 0.45)
        for i in range(ancho):
            a = int(255 * (i / ancho) ** 1.6)
            x = img.width - 1 - i if lado == "izq" else i
            dm.line([(x, 0), (x, img.height)], fill=a)
        for i in range(int(img.height * 0.18)):   # también abajo
            y = img.height - 1 - i
            dm.line([(0, y), (img.width, y)], fill=int(255 * (i / (img.height * 0.18))))
        img.putalpha(ImageChops.multiply(img.getchannel("A"), m))
    s = alto / img.height
    return img.resize((max(1, int(img.width * s)), alto), Image.LANCZOS)


def _contorno(suj: Image.Image, color, ancho=10, blur=14, fuerza=2) -> Image.Image:
    """Halo de color alrededor del sujeto (luz de borde, como en la referencia)."""
    pad = ancho + blur * 2
    a = Image.new("L", (suj.width + pad * 2, suj.height + pad * 2), 0)
    a.paste(suj.getchannel("A"), (pad, pad))
    a = a.filter(ImageFilter.MaxFilter(ancho // 2 * 2 + 1)).filter(ImageFilter.GaussianBlur(blur))
    halo = Image.new("RGBA", a.size, (*color, 0))
    halo.putalpha(a)
    out = Image.new("RGBA", a.size, (0, 0, 0, 0))
    for _ in range(fuerza):
        out.alpha_composite(halo)
    out.alpha_composite(suj, (pad, pad))
    return out, pad


def _pegar(lienzo, suj, x, y, color, sombra=True):
    con, pad = _contorno(suj, color)
    if sombra:
        s = Image.new("RGBA", con.size, (0, 0, 0, 0))
        s.putalpha(con.getchannel("A").point(lambda v: int(v * 0.55)).filter(ImageFilter.GaussianBlur(16)))
        lienzo.alpha_composite(s, (int(x - pad + 14), int(y - pad + 18)))
    lienzo.alpha_composite(con, (int(x - pad), int(y - pad)))


def _mascota(pose: str) -> Image.Image | None:
    p = MASCOTA / f"{pose}.png"
    if not p.exists():
        p = MASCOTA / "apuntando.png"
    return Image.open(p).convert("RGBA") if p.exists() else None


# ------------------------------------------------------------------ texto
def _degradado_v(size, c1, c2) -> Image.Image:
    g = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        g.putpixel((0, y), tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    return g.resize(size, Image.BICUBIC)


def _linea_3d(texto: str, tam: int, colores, max_w: int) -> Image.Image:
    f = _f(TITULO_TTF, tam)
    while f.getlength(texto) > max_w and tam > 40:
        tam -= 4
        f = _f(TITULO_TTF, tam)
    trazo = max(6, tam // 11)
    prof = max(6, tam // 9)
    bb = f.getbbox(texto)
    w, h = bb[2] - bb[0] + trazo * 2 + prof + 8, bb[3] - bb[1] + trazo * 2 + prof + 8
    ox, oy = trazo - bb[0] + 2, trazo - bb[1] + 2
    capa = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    # extrusión 3D (de lo más profundo a lo más cercano)
    for k in range(prof, 0, -1):
        d.text((ox + k * 0.55, oy + k), texto, font=f, fill=NEGRO, stroke_width=trazo, stroke_fill=NEGRO)
    d.text((ox, oy), texto, font=f, fill=NEGRO, stroke_width=trazo, stroke_fill=NEGRO)
    # relleno con degradado
    mascara = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mascara).text((ox, oy), texto, font=f, fill=255)
    relleno = _degradado_v((w, h), *colores).convert("RGBA")
    relleno.putalpha(mascara)
    capa.alpha_composite(relleno)
    # brillo en la mitad de arriba
    luz = Image.new("L", (w, h), 0)
    ImageDraw.Draw(luz).rectangle((0, 0, w, oy + (bb[3] - bb[1]) * 0.42), fill=70)
    luz = ImageChops.multiply(luz, mascara)
    blanco = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    blanco.putalpha(luz)
    capa.alpha_composite(blanco)
    return capa


def _cinta(texto: str, color, max_w: int, tam=50) -> Image.Image:
    f = _f(CINTA_TTF, tam)
    while f.getlength(texto) > max_w - 80 and tam > 26:
        tam -= 2
        f = _f(CINTA_TTF, tam)
    tw = int(f.getlength(texto))
    alto = int(tam * 1.45)
    incl = alto // 3
    w = tw + 70 + incl
    capa = Image.new("RGBA", (w + 12, alto + 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    forma = [(incl, 0), (w, 0), (w - incl, alto), (0, alto)]
    d.polygon([(x + 8, y + 8) for x, y in forma], fill=(0, 0, 0, 160))
    d.polygon(forma, fill=color, outline=NEGRO, width=5)
    d.text((w / 2, alto / 2 + 2), texto, font=f, fill="white", anchor="mm", stroke_width=4, stroke_fill=NEGRO)
    return capa


def bloque_titulo(linea1: str, linea2: str, cinta: str, color_cinta, max_w=640, tam=150, giro=-4) -> Image.Image:
    capas = []
    if linea1:
        capas.append(_linea_3d(linea1.upper(), int(tam * 0.82) if linea2 else tam, BLANCO, max_w))
    if linea2:
        capas.append(_linea_3d(linea2.upper(), tam, AMARILLO, max_w))
    if cinta:
        capas.append(_cinta(cinta.upper(), color_cinta, max_w))
    ancho = max(c.width for c in capas)
    alto = sum(c.height for c in capas) - 18 * (len(capas) - 1)
    bloque = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    y = 0
    for c in capas:
        bloque.alpha_composite(c, ((ancho - c.width) // 2, y))
        y += c.height - 18
    return bloque.rotate(-giro, resample=Image.BICUBIC, expand=True)


def _sello(lienzo: Image.Image):
    """Sello «LCMC Games» arriba a la izquierda, siempre igual (marca del canal)."""
    t1 = _linea_3d("LCMC", 54, BLANCO, 400)
    t2 = _cinta("GAMES", (255, 176, 0), 300, tam=26)
    s = Image.new("RGBA", (max(t1.width, t2.width) + 10, t1.height + t2.height - 10), (0, 0, 0, 0))
    s.alpha_composite(t1, ((s.width - t1.width) // 2, 0))
    s.alpha_composite(t2, ((s.width - t2.width) // 2, t1.height - 14))
    s = s.rotate(4, resample=Image.BICUBIC, expand=True)
    lienzo.alpha_composite(s, (22, 14))


# ------------------------------------------------------------------ acentos
def _acento(lienzo: Image.Image, tipo: str, x: int, y: int, escala=1.0):
    d = ImageDraw.Draw(lienzo)
    if tipo == "subir":
        for i, (dx, s) in enumerate(((0, 1.0), (110, 0.75), (-95, 0.6))):
            w, h = int(70 * s * escala), int(170 * s * escala)
            cx, cy = x + dx, y + i * 30
            flecha = [(cx, cy - h // 2), (cx + w, cy - h // 2 + w), (cx + w // 2, cy - h // 2 + w), (cx + w // 2, cy + h // 2),
                      (cx - w // 2, cy + h // 2), (cx - w // 2, cy - h // 2 + w), (cx - w, cy - h // 2 + w)]
            d.polygon([(px + 6, py + 6) for px, py in flecha], fill=(0, 0, 0, 140))
            d.polygon(flecha, fill=(120, 255, 90) if i == 0 else (255, 214, 40), outline=NEGRO, width=5)
    elif tipo == "alerta":
        r = int(110 * escala)
        tri = [(x, y - r), (x + r, y + r * 0.75), (x - r, y + r * 0.75)]
        d.polygon([(px + 8, py + 8) for px, py in tri], fill=(0, 0, 0, 150))
        d.polygon(tri, fill=(255, 52, 40), outline=NEGRO, width=8)
        d.text((x, y + r * 0.2), "!", font=_f(TITULO_TTF, int(r * 1.2)), fill="white", anchor="mm",
               stroke_width=6, stroke_fill=NEGRO)
    elif tipo == "flecha":
        pts = []
        for i in range(21):
            t = i / 20
            pts.append((x - 120 * escala + 240 * escala * t, y + 70 * escala - 150 * escala * math.sin(t * math.pi * 0.8)))
        d.line(pts, fill=NEGRO, width=int(46 * escala), joint="curve")
        d.line(pts, fill=(255, 40, 40), width=int(30 * escala), joint="curve")
        (ax, ay), (bx, by) = pts[-1], pts[-3]
        ang = math.atan2(ay - by, ax - bx)
        L = 60 * escala
        punta = [(ax + L * math.cos(ang), ay + L * math.sin(ang)),
                 (ax + L * 0.8 * math.cos(ang + 2.3), ay + L * 0.8 * math.sin(ang + 2.3)),
                 (ax + L * 0.8 * math.cos(ang - 2.3), ay + L * 0.8 * math.sin(ang - 2.3))]
        d.polygon(punta, fill=(255, 40, 40), outline=NEGRO, width=6)
    elif tipo == "vs":
        v0 = _linea_3d("VS", int(230 * escala), AMARILLO, 600).rotate(8, resample=Image.BICUBIC, expand=True)
        v = Image.new("RGBA", (v0.width + 160, v0.height + 160), (0, 0, 0, 0))
        v.alpha_composite(v0, (80, 80))
        brillo = Image.new("RGBA", v.size, (255, 120, 0, 0))
        brillo.putalpha(v.getchannel("A").filter(ImageFilter.GaussianBlur(26)))
        lienzo.alpha_composite(brillo, (x - v.width // 2, y - v.height // 2))
        lienzo.alpha_composite(brillo, (x - v.width // 2, y - v.height // 2))
        lienzo.alpha_composite(v, (x - v.width // 2, y - v.height // 2))
    elif tipo == "corona":
        r = int(90 * escala)
        corona = [(x - r, y + r // 2), (x - r, y - r // 3), (x - r // 2, y), (x, y - r), (x + r // 2, y),
                  (x + r, y - r // 3), (x + r, y + r // 2)]
        d.polygon([(px + 7, py + 7) for px, py in corona], fill=(0, 0, 0, 150))
        d.polygon(corona, fill=(255, 200, 30), outline=NEGRO, width=7)
        for px in (x - r // 2, x, x + r // 2):
            d.ellipse((px - 11, y + r // 6 - 11, px + 11, y + r // 6 + 11), fill=(140, 60, 255), outline=NEGRO, width=3)


# ------------------------------------------------------------------ antes / después (mismo héroe, dos mitades)
def _escena_dividida(tema_k: str, heroe: Image.Image, fondo, rng, frio: bool, pos=(0, 0)) -> Image.Image:
    oscuro, medio, brillo, _, c_rayo = TEMAS[tema_k]
    lienzo = _degradado(oscuro, medio, radial=(0.5, 0.42)).convert("RGBA")
    if fondo is not None:
        t = _tenir(_cubrir(fondo.convert("RGB")).filter(ImageFilter.GaussianBlur(4)), oscuro, brillo).convert("RGBA")
        t.putalpha(100)
        lienzo.alpha_composite(t)
    lienzo.alpha_composite(_rayos((W // 2, int(H * 0.4)), brillo, rng, n=16))
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(g).ellipse((W // 2 - 360, 20, W // 2 + 360, 720), fill=(*brillo, 130))
    lienzo.alpha_composite(g.filter(ImageFilter.GaussianBlur(90)))
    zonas = [(40, 0, 380, int(H * 0.6))] if frio else [(W - 380, 0, W - 40, int(H * 0.6))]
    lienzo.alpha_composite(_energia(c_rayo, rng, zonas))
    suj = heroe
    if frio:   # el mismo héroe, «antes»: frío, sin color demoníaco
        rgb = ImageOps.colorize(ImageOps.grayscale(suj.convert("RGB")), black=(4, 10, 40), white=(200, 228, 255),
                                mid=(60, 110, 200)).convert("RGBA")
        rgb.putalpha(suj.getchannel("A"))
        suj = Image.blend(suj, rgb, 0.85)
    x, y = pos
    _pegar(lienzo, suj, x, y, c_rayo)
    return lienzo


def _centro_cara(img: Image.Image) -> tuple[int, int]:
    """Estimación de la cabeza: centro de masa de la franja 18-34 % superior del sujeto."""
    a = img.getchannel("A")
    y0, y1 = int(img.height * 0.18), int(img.height * 0.34)
    franja = a.crop((0, y0, img.width, y1)).resize((img.width // 4, 1), Image.BOX)
    vals = list(franja.getdata())
    tot = sum(vals) or 1
    cx = sum(i * v for i, v in enumerate(vals)) / tot * 4
    return int(cx), (y0 + y1) // 2


def generar_dividido(spec: dict, destino: Path, heroe: Image.Image, fondo=None) -> Path:
    """Concepto «antes y después»: el héroe partido en diagonal, izquierda fría (antes) y derecha roja (ahora)."""
    rng = _rng(spec.get("linea1"), spec.get("linea2"), "dividido")
    h = heroe.convert("RGBA")
    alto = int(spec.get("alto_heroe", 860))
    h = h.resize((int(h.width * alto / h.height), alto), Image.LANCZOS)
    x_arr, x_aba = int(W * 0.60), int(W * 0.42)          # el corte diagonal
    # la cara del héroe justo sobre el corte: mitad fría, mitad demonio
    if spec.get("cara"):          # posición de la cara en el recorte (fracciones), si se conoce
        cx, cy = int(spec["cara"][0] * h.width), int(spec["cara"][1] * h.height)
    else:
        cx, cy = _centro_cara(h)
    y = int(spec.get("y_heroe", -40))
    cara_y = y + cy
    linea_x = x_arr - (x_arr - x_aba) * cara_y / H
    pos = (int(linea_x - cx + int(spec.get("cara_offset", 70))), y)   # la cara un poco del lado «ahora», para que se reconozca
    izq = _escena_dividida(spec.get("tema_antes", "azul"), h, fondo, rng, True, pos)
    der = _escena_dividida(spec.get("tema_ahora", "rojo"), h, fondo, _rng("der", spec.get("linea2")), False, pos)
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).polygon([(x_arr, 0), (W, 0), (W, H), (x_aba, H)], fill=255)
    lienzo = izq.copy()
    lienzo.paste(der, (0, 0), m)
    # el tajo: línea blanca con brillo
    tajo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(tajo).line([(x_arr, -10), (x_aba, H + 10)], fill=(255, 255, 255, 255), width=7)
    for radio, col in ((22, (255, 60, 60)), (10, (255, 255, 255))):
        b = Image.new("RGBA", (W, H), (*col, 0))
        b.putalpha(tajo.getchannel("A").filter(ImageFilter.GaussianBlur(radio)).point(lambda v: min(255, v * 3)))
        lienzo.alpha_composite(b)
    lienzo.alpha_composite(tajo)
    _vineta(lienzo)
    t = bloque_titulo(spec.get("linea1", ""), spec.get("linea2", ""), spec.get("cinta", ""),
                      TEMAS["rojo"][3], max_w=900, tam=int(spec.get("tam_titulo", 92)), giro=-3)
    if t.width > 960:
        r = 960 / t.width
        t = t.resize((int(t.width * r), int(t.height * r)), Image.LANCZOS)
    lienzo.alpha_composite(t, ((W - t.width) // 2, H - t.height - 4))
    _sello(lienzo)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    lienzo.convert("RGB").save(destino, "JPEG", quality=93)
    return destino


# ------------------------------------------------------------------ composición
def generar(spec: dict, destino: Path, heroe: Image.Image | None = None, heroe2: Image.Image | None = None,
            fondo: Image.Image | None = None) -> Path:
    """spec: {layout: heroe|vs|mascota|dividido, linea1, linea2, cinta, tema: azul|rojo|morado|dorado,
              acento: ninguno|flecha|subir|alerta|corona, pose: <pose de la mascota>, lado: izq|der}"""
    layout = spec.get("layout", "heroe")
    if layout == "dividido" and heroe is not None:
        return generar_dividido(spec, destino, heroe, fondo)
    tema = TEMAS.get(spec.get("tema", "azul"), TEMAS["azul"])
    oscuro, medio, brillo, c_cinta, c_rayo = tema
    rng = _rng(spec.get("linea1"), spec.get("linea2"), layout)
    lado = spec.get("lado", "izq")                 # dónde va el sujeto principal
    if heroe is None and layout in ("heroe", "vs"):
        layout = "mascota"

    # 1) fondo
    centro = (0.3 if lado == "izq" else 0.7, 0.45)
    lienzo = _degradado(oscuro, medio, radial=centro).convert("RGBA")
    arte = fondo or heroe
    if arte is not None:
        teñido = _tenir(_cubrir(arte.convert("RGB")).filter(ImageFilter.GaussianBlur(3)), oscuro, brillo).convert("RGBA")
        teñido.putalpha(110)
        lienzo.alpha_composite(teñido)
    cx = int(W * centro[0])
    lienzo.alpha_composite(_rayos((cx, int(H * 0.42)), brillo, rng))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - 330, 40, cx + 330, 700), fill=(*brillo, 120))
    lienzo.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))
    # relámpagos entre el sujeto y el texto, y en la esquina libre (no detrás del sujeto, ahí no se ven)
    zonas = [(430, 0, 640, int(H * 0.55)), (980, 0, W - 40, int(H * 0.3))] if lado == "izq" \
        else [(640, 0, 850, int(H * 0.55)), (40, 0, 300, int(H * 0.3))]
    if layout == "vs":
        zonas = [(520, 0, 760, H)]
    lienzo.alpha_composite(_energia(c_rayo, rng, zonas))

    # 2) sujetos
    texto_x = (W * 0.70, H * 0.60) if lado == "izq" else (W * 0.32, H * 0.60)
    if layout == "heroe":
        s = _preparar_sujeto(heroe, 820, lado)
        x = -int(s.width * 0.12) if lado == "izq" else W - int(s.width * 0.88)
        _pegar(lienzo, s, x, H - s.height + 60, c_rayo)
        if spec.get("pose"):
            m = _mascota(spec["pose"])
            if m:
                m = m.resize((300, 300), Image.LANCZOS)
                if lado != "izq":
                    m = ImageOps.mirror(m)
                _pegar(lienzo, m, W - 300 if lado == "izq" else 0, H - 290, (255, 255, 255), sombra=True)
    elif layout == "vs":
        a = _preparar_sujeto(heroe, 760, "izq")
        _pegar(lienzo, a, -int(a.width * 0.15), H - a.height + 40, (90, 170, 255))
        if heroe2 is not None:
            b = _preparar_sujeto(heroe2, 760, "der")
            _pegar(lienzo, b, W - int(b.width * 0.85), H - b.height + 40, (255, 80, 160))
        corte = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(corte).line([(700, 0), (580, H)], fill=(255, 255, 255, 230), width=8)
        lienzo.alpha_composite(corte.filter(ImageFilter.GaussianBlur(2)))
        _acento(lienzo, "vs", W // 2, int(H * 0.36))
        texto_x = (W * 0.5, H * 0.80)
    else:  # mascota
        m = _mascota(spec.get("pose", "apuntando"))
        if m:
            m = m.resize((640, 640), Image.LANCZOS)
            if lado != "izq":
                m = ImageOps.mirror(m)
            _pegar(lienzo, m, -30 if lado == "izq" else W - 610, H - 600, c_rayo)

    acento = spec.get("acento", "ninguno")
    if acento in ("flecha", "subir", "alerta", "corona") and layout != "vs":
        ax = 470 if lado == "izq" else 810
        _acento(lienzo, acento, ax, 200, 0.9)

    # 3) título
    ancho_txt = 1080 if layout == "vs" else 640
    t = bloque_titulo(spec.get("linea1", ""), spec.get("linea2", ""), spec.get("cinta", ""), c_cinta,
                      max_w=ancho_txt, tam=140 if layout == "vs" else 150)
    if t.width > ancho_txt + 60:
        r = (ancho_txt + 60) / t.width
        t = t.resize((int(t.width * r), int(t.height * r)), Image.LANCZOS)
    tx, ty = int(texto_x[0] - t.width / 2), int(texto_x[1] - t.height / 2)
    tx = max(10, min(W - t.width - 10, tx))
    ty = max(110, min(H - t.height - 6, ty))
    lienzo.alpha_composite(t, (tx, ty))

    # 4) marca y viñeta
    _vineta(lienzo)
    _sello(lienzo)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    lienzo.convert("RGB").save(destino, "JPEG", quality=93)
    return destino
