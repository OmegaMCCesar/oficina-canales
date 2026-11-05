"""Paso 3: arma el video (fondo animado, Lumi, textos, subtítulos) y lo une con la voz.

Funciona igual para horizontal (YouTube) y vertical (TikTok/Shorts); solo cambia el layout.
"""
import math
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import marca as M
from ..tools.ffmpeg import FFMPEG

FADE = 0.35        # segundos de entrada de cada escena
COLA = 0.45        # silencio al final de cada escena (respiro)


# ---------------------------------------------------------------- utilidades
@lru_cache(maxsize=64)
def F(tam, peso="Bold"):
    return ImageFont.truetype(M.fuente(peso), tam)


def ease_out_back(x):
    x = min(max(x, 0), 1)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def ease_out(x):
    x = min(max(x, 0), 1)
    return 1 - (1 - x) ** 3


def envolver(texto, fuente, ancho):
    lineas, actual = [], ""
    for p in texto.split():
        prueba = (actual + " " + p).strip()
        if fuente.getlength(prueba) <= ancho or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)
    return lineas


def ajustar(texto, tam, ancho, max_lineas, peso="Bold"):
    """Reduce la letra hasta que el texto quepa en max_lineas."""
    while tam > 24:
        f = F(tam, peso)
        ls = envolver(texto, f, ancho)
        if len(ls) <= max_lineas:
            return f, ls
        tam -= 4
    f = F(tam, peso)
    return f, envolver(texto, f, ancho)[:max_lineas]


def con_opacidad(capa, a):
    if a >= 0.999:
        return capa
    c = capa.copy()
    c.putalpha(c.getchannel("A").point(lambda v: int(v * a)))
    return c


def triangulo_alerta(tam, color=M.AMARILLO):
    im = Image.new("RGBA", (tam, tam), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(tam / 2, 4), (tam - 4, tam - 6), (4, tam - 6)], fill=color)
    f = F(int(tam * 0.55))
    d.text((tam / 2, tam * 0.62), "!", font=f, fill=(20, 20, 20), anchor="mm")
    return im


# ---------------------------------------------------------------- fondo
def crear_fondo(W, H):
    """Fondo azul noche con resplandores de marca, más grande que el cuadro para poder moverlo.
    Se calcula a 1/4 de resolución (los resplandores son suaves) y se agranda: ~16 veces menos memoria."""
    w, h = int(W * 1.2), int(H * 1.2)
    q = 4
    sw, sh = w // q, h // q
    y, x = np.mgrid[0:sh, 0:sw].astype(np.float32)
    base = np.zeros((sh, sw, 3), np.float32) + np.array(M.FONDO, np.float32)

    def glow(cx, cy, r, color, fuerza):
        d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / r
        return (np.exp(-d ** 2) * fuerza)[..., None] * np.array(color, np.float32)

    base += glow(sw * 0.18, sh * 0.30, max(sw, sh) * 0.38, M.AZUL, 0.35)
    base += glow(sw * 0.85, sh * 0.80, max(sw, sh) * 0.32, M.VERDE, 0.12)
    base += glow(sw * 0.70, sh * 0.15, max(sw, sh) * 0.25, M.CIAN, 0.10)
    im = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).resize((w, h), Image.BICUBIC).convert("RGBA")
    del base, x, y
    # rejilla técnica muy tenue (líneas finas dibujadas a resolución completa)
    capa = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    col = (*M.CIAN, 9)
    for gx in range(0, w, 64):
        d.line((gx, 0, gx, h), fill=col, width=1)
    for gy in range(0, h, 64):
        d.line((0, gy, w, gy), fill=col, width=1)
    im.alpha_composite(capa)
    return im


def resplandor(tam, color):
    y, x = np.mgrid[0:tam, 0:tam].astype(np.float32)
    d = np.sqrt((x - tam / 2) ** 2 + (y - tam / 2) ** 2) / (tam / 2)
    a = np.clip(1 - d, 0, 1) ** 2.2 * 150
    im = np.zeros((tam, tam, 4), np.uint8)
    im[..., :3] = color
    im[..., 3] = a.astype(np.uint8)
    return Image.fromarray(im)


# ---------------------------------------------------------------- layouts
def layout(formato, W, H, con_imagen):
    if formato == "largo":
        L = dict(lumi_c=(440, 575), lumi_t=640, txt_x=860, txt_y=200, txt_w=980,
                 titulo=84, titulo_lineas=2, punto=46, sub_y=960, sub_t=50, sub_w=1600,
                 sub_palabras=7, img_box=(860, 400, 1840, 860), marca=(64, 48))
    else:
        L = dict(lumi_c=(540, 1215), lumi_t=600, txt_x=70, txt_y=280, txt_w=940,
                 titulo=96, titulo_lineas=3, punto=54, sub_y=1600, sub_t=68, sub_w=960,
                 sub_palabras=4, img_box=(80, 560, 1000, 1080), marca=(80, 170))
        if con_imagen:
            L.update(lumi_c=(800, 1290), lumi_t=440)
    return L


# ---------------------------------------------------------------- capas de texto
def capa_titulo(texto, L, acento):
    f, lineas = ajustar(texto, L["titulo"], L["txt_w"], L["titulo_lineas"])
    alto = int(f.size * 1.18)
    im = Image.new("RGBA", (L["txt_w"] + 40, alto * len(lineas) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # barra de acento a la izquierda del título
    d.rounded_rectangle((0, 10, 10, alto * len(lineas) - 6), 5, fill=acento)
    for i, ln in enumerate(lineas):
        d.text((32, i * alto), ln, font=f, fill=M.BLANCO)
    return im


def capa_punto(texto, L, acento, ancho):
    f, lineas = ajustar(texto, L["punto"], ancho - 90, 2, "SemiBold")
    alto = int(f.size * 1.25)
    h = alto * len(lineas) + 34
    im = Image.new("RGBA", (ancho, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, ancho - 1, h - 1), 22, fill=(255, 255, 255, 18),
                        outline=(*acento, 90), width=2)
    r = f.size * 0.32
    cy = 17 + alto / 2
    d.ellipse((30 - r, cy - r, 30 + r, cy + r), fill=acento)
    for i, ln in enumerate(lineas):
        d.text((62, 17 + i * alto), ln, font=f, fill=(235, 242, 255))
    return im


def capa_imagen(ruta, caja):
    x1, y1, x2, y2 = caja
    w, h = x2 - x1, y2 - y1
    im = Image.open(ruta).convert("RGB")
    esc = max(w / im.width, h / im.height) * 1.1   # 10% extra para el zoom lento
    im = im.resize((int(im.width * esc), int(im.height * esc)), Image.LANCZOS)
    return im, (w, h)


def marco_imagen(foto, tam, t, dur):
    """Ken Burns: acercamiento lento sobre la ilustración."""
    w, h = tam
    z = 1 + 0.08 * (t / max(dur, 0.1))
    cw, ch = int(foto.width / z / 1.1 * 1.0), int(foto.height / z / 1.1)
    cw, ch = max(cw, w // 2), max(ch, h // 2)
    x = (foto.width - cw) // 2
    y = (foto.height - ch) // 2
    rec = foto.crop((x, y, x + cw, y + ch)).resize((w, h), Image.BILINEAR).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), 36, fill=255)
    rec.putalpha(mask)
    return rec


# ---------------------------------------------------------------- subtítulos
def trozos_subtitulo(narracion, palabras_t, dur_voz, n):
    """Divide la narración en bloques de n palabras con su tiempo de inicio de cada palabra."""
    palabras = narracion.split()
    if palabras_t and len(palabras_t) >= len(palabras) * 0.8:
        tiempos = [p[1] for p in palabras_t][: len(palabras)]
        while len(tiempos) < len(palabras):
            tiempos.append(tiempos[-1] + 0.3)
    else:  # reparto proporcional al largo de cada palabra
        total = sum(len(p) + 2 for p in palabras) or 1
        acc, tiempos = 0.0, []
        for p in palabras:
            tiempos.append(0.1 + acc / total * dur_voz)
            acc += len(p) + 2
    bloques = []
    i = 0
    while i < len(palabras):
        j = min(i + n, len(palabras))
        # corta antes en puntuación para que se lea natural
        for k in range(i + 1, j):
            if palabras[k - 1][-1] in ".,;:?!" and k - i >= 2:
                j = k
                break
        bloques.append((palabras[i:j], tiempos[i:j]))
        i = j
    return bloques


def capa_subtitulo(palabras, activa, L, vertical):
    f = F(L["sub_t"], "ExtraBold" if vertical else "Bold")
    texto = " ".join(palabras)
    lineas = envolver(texto, f, L["sub_w"] - 80)
    alto = int(f.size * 1.22)
    ancho = int(max(f.getlength(l) for l in lineas)) + 80
    im = Image.new("RGBA", (ancho, alto * len(lineas) + 36), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if not vertical:
        d.rounded_rectangle((0, 0, im.width - 1, im.height - 1), 26, fill=(0, 6, 18, 175))
    idx = 0
    for li, ln in enumerate(lineas):
        x = (ancho - f.getlength(ln)) / 2
        y = 18 + li * alto
        for p in ln.split():
            color = M.LIMA if idx == activa else M.BLANCO
            if vertical:  # contorno estilo TikTok
                d.text((x, y), p, font=f, fill=color, stroke_width=6, stroke_fill=(0, 0, 0))
            else:
                d.text((x, y), p, font=f, fill=color)
            x += f.getlength(p + " ")
            idx += 1
    return im


# ---------------------------------------------------------------- marca fija
def capa_marca(vertical):
    f1, f2 = F(42 if vertical else 36), F(28 if vertical else 26, "Medium")
    im = Image.new("RGBA", (620, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((0, 6), M.MARCA_1, font=f1, fill=M.CIAN)
    d.text((f1.getlength(M.MARCA_1 + " ") + 6, 16), M.MARCA_2, font=f2, fill=M.GRIS)
    return im


# ---------------------------------------------------------------- render principal
class Poses:
    def __init__(self, tam):
        self.tam = tam
        self.cache = {}

    def get(self, nombre):
        if nombre not in self.cache:
            ruta = Path(nombre) if str(nombre).startswith("/") else M.POSES_DIR / f"{nombre}.png"
            if not ruta.exists():
                ruta = M.POSES_DIR / f"{M.ALIAS_GAMES.get(nombre, nombre)}.png"
            if not ruta.exists():
                ruta = M.POSES_DIR / "feliz.png"
            im = Image.open(ruta).convert("RGBA")
            if str(nombre).startswith("/"):
                im = retrato(im, self.tam)
            else:
                im = im.resize((self.tam, self.tam), Image.LANCZOS)
            self.cache[nombre] = im
        return self.cache[nombre]


def retrato(im, tam):
    """Arte de personaje (no recortado): cuadrado redondeado con borde de acento, en lugar de Lumi."""
    s = max(tam / im.width, tam / im.height)
    im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - tam) // 2, int((im.height - tam) * 0.25)
    im = im.crop((x, y, x + tam, y + tam))
    mask = Image.new("L", (tam, tam), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tam - 1, tam - 1), tam // 9, fill=255)
    im.putalpha(mask)
    marco = Image.new("RGBA", (tam, tam), (0, 0, 0, 0))
    ImageDraw.Draw(marco).rounded_rectangle((2, 2, tam - 3, tam - 3), tam // 9, outline=(*M.CIAN, 255), width=max(4, tam // 90))
    im.alpha_composite(marco)
    return im


def acento_de(escena):
    if escena.get("tipo") == "advertencia" or escena.get("pose") == "advertencia":
        return M.AMARILLO
    if escena.get("tipo") in ("pasos", "cierre") or escena.get("pose") in ("solucion", "listo", "celebracion"):
        return M.VERDE
    return M.CIAN


def renderizar(escenas, narraciones, formato, salida: Path, trabajo: Path, musica: Path | None = None, fps: int | None = None):
    """escenas: lista de dicts del guion. narraciones: lista de dicts devueltos por voz.narrar."""
    W, H = M.FORMATOS[formato]
    vertical = H > W
    fps = fps or M.FPS
    fondo = crear_fondo(W, H)
    marca_img = capa_marca(vertical)

    # --- audio: cada escena se rellena a su duración exacta y se concatena
    durs = []
    lista = trabajo / f"audio_{formato}.txt"
    with open(lista, "w") as fl:
        for i, n in enumerate(narraciones):
            d = n["duracion"] + COLA + (0.25 if i == 0 else 0)
            durs.append(d)
            wav = trabajo / f"a_{formato}_{i:02d}.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(n["audio"]),
                            "-af", f"adelay={250 if i == 0 else 0}:all=1,apad=whole_dur={d:.3f}",
                            "-ar", "44100", "-ac", "1", "-t", f"{d:.3f}", str(wav)], check=True)
            fl.write(f"file '{wav.name}'\n")
    voz = trabajo / f"voz_{formato}.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lista), "-c", "copy", str(voz)], check=True)
    total = sum(durs)

    # --- video por pipe a ffmpeg
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-i", str(voz)]
    filtro_audio = None
    if musica and Path(musica).exists():
        cmd += ["-stream_loop", "-1", "-i", str(musica)]
        filtro_audio = ("[2:a]volume=0.10,afade=t=in:d=1[m];[1:a][m]amix=inputs=2:duration=first:"
                        "dropout_transition=0:normalize=0[a]")
    if filtro_audio:
        cmd += ["-filter_complex", filtro_audio, "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "superfast", "-crf", "21", "-pix_fmt", "yuv420p", "-threads", "1",
            "-x264-params", "threads=1:lookahead-threads=1:rc-lookahead=5:ref=1:bframes=0",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(salida)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    t_global = 0.0
    pose_prev = None
    for i, (esc, nar, dur) in enumerate(zip(escenas, narraciones, durs)):
        con_img = bool(esc.get("imagen")) and Path(esc["imagen"]).exists()
        L = layout(formato, W, H, con_img)
        poses = Poses(L["lumi_t"])
        acento = acento_de(esc)
        lumi = poses.get(esc["pose"])
        glow = resplandor(int(L["lumi_t"] * 1.25), acento)
        pop = esc["pose"] != pose_prev
        pose_prev = esc["pose"]

        titulo = capa_titulo(esc["titulo"], L, acento) if esc.get("titulo") else None
        ancho_p = L["txt_w"]
        puntos = [] if con_img else [capa_punto(p, L, acento, ancho_p) for p in esc.get("puntos", [])]
        foto, foto_tam = capa_imagen(esc["imagen"], L["img_box"]) if con_img else (None, None)
        alerta = triangulo_alerta(90 if vertical else 76) if acento == M.AMARILLO else None

        if vertical and not con_img:
            # Lumi se acomoda en el espacio libre entre el texto y los subtítulos
            fondo_txt = L["txt_y"] + (titulo.height + 24 if titulo else 0) + sum(p.height + 18 for p in puntos)
            libre_ini, libre_fin = fondo_txt + 30, L["sub_y"] - 120
            tam = int(min(L["lumi_t"], (libre_fin - libre_ini) * 1.12))
            L["lumi_t"] = max(tam, 380)
            L["lumi_c"] = (W // 2, int((libre_ini + libre_fin) / 2))
            poses = Poses(L["lumi_t"])
            lumi = poses.get(esc["pose"])
            glow = resplandor(int(L["lumi_t"] * 1.25), acento)

        # momentos de aparición de cada viñeta, repartidos en la narración
        voz_d = nar["duracion"]
        t_puntos = [0.4 + (k + 0.3) / (len(puntos) + 0.3) * voz_d * 0.75 for k in range(len(puntos))]
        retraso = 0.25 if i == 0 else 0
        bloques = trozos_subtitulo(esc["narracion"], nar["palabras"], voz_d, L["sub_palabras"])
        sub_cache = {}

        nframes = int(round(dur * fps))
        for fi in range(nframes):
            t = fi / fps
            tg = t_global + t
            frame = Image.new("RGBA", (W, H))
            # fondo con desplazamiento lento
            ox = int((fondo.width - W) / 2 + math.sin(tg * 0.12) * (fondo.width - W) / 2.2)
            oy = int((fondo.height - H) / 2 + math.cos(tg * 0.09) * (fondo.height - H) / 2.2)
            frame.paste(fondo.crop((ox, oy, ox + W, oy + H)), (0, 0))

            entrada = ease_out(t / FADE)
            salida_a = ease_out((dur - t) / 0.2) if i < len(escenas) - 1 else 1
            a = min(entrada, salida_a)

            # Lumi: rebote de entrada + flotación + respiración
            cx, cy = L["lumi_c"]
            esc_l = (0.75 + 0.25 * ease_out_back(t / 0.5)) if pop else 1.0
            esc_l *= 1 + 0.015 * math.sin(tg * 2.4)
            tam = int(L["lumi_t"] * esc_l)
            dy = math.sin(tg * 2.2) * 14
            g = con_opacidad(glow, 0.55 + 0.25 * math.sin(tg * 1.7))
            frame.alpha_composite(g, (int(cx - g.width / 2), int(cy + dy - g.height / 2)))
            li = lumi if tam == lumi.width else lumi.resize((tam, tam), Image.BILINEAR)
            frame.alpha_composite(li, (int(cx - tam / 2), int(cy + dy - tam / 2)))

            # título y viñetas
            desliz = int((1 - entrada) * 40)
            y = L["txt_y"]
            if titulo:
                if alerta:
                    frame.alpha_composite(con_opacidad(alerta, a),
                                          (L["txt_x"] + L["txt_w"] - alerta.width, y - alerta.height - 20 + desliz))
                frame.alpha_composite(con_opacidad(titulo, a), (L["txt_x"], y + desliz))
                y += titulo.height + 24
            for k, (p, tp) in enumerate(zip(puntos, t_puntos)):
                ap = ease_out((t - tp) / 0.3)
                if ap > 0:
                    frame.alpha_composite(con_opacidad(p, min(ap, salida_a)),
                                          (L["txt_x"] + int((1 - ap) * 60), y))
                y += p.height + 18
            if foto is not None:
                x1, y1, _, _ = L["img_box"]
                if titulo and not vertical:
                    y1 = max(y1, L["txt_y"] + titulo.height + 30)
                fr = marco_imagen(foto, foto_tam, t, dur)
                frame.alpha_composite(con_opacidad(fr, a), (x1, y1 + desliz))

            # subtítulo con palabra activa resaltada
            tv = t - retraso
            for bi, (pals, ts) in enumerate(bloques):
                fin = bloques[bi + 1][1][0] if bi + 1 < len(bloques) else voz_d + COLA
                if ts[0] - 0.05 <= tv < fin:
                    activa = max([k for k, tt in enumerate(ts) if tt <= tv] or [0])
                    clave = (bi, activa)
                    if clave not in sub_cache:
                        sub_cache[clave] = capa_subtitulo(pals, activa, L, vertical)
                    s = sub_cache[clave]
                    frame.alpha_composite(s, (int((W - s.width) / 2), int(L["sub_y"] - s.height / 2)))
                    break

            # marca y barra de progreso
            frame.alpha_composite(marca_img, L["marca"])
            dbar = ImageDraw.Draw(frame)
            prog = tg / total
            dbar.rectangle((0, 0, W, 7), fill=(255, 255, 255, 25))
            dbar.rectangle((0, 0, int(W * prog), 7), fill=(*M.CIAN, 255))
            dbar.rectangle((max(0, int(W * prog) - 60), 0, int(W * prog), 7), fill=(*M.VERDE, 255))

            proc.stdin.write(frame.convert("RGB").tobytes())
        t_global += dur
        try:
            from ..memory import trim
            trim()
        except Exception:
            pass
    proc.stdin.close()
    proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg falló al armar el video")
    return salida, total
