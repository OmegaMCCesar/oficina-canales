"""ESTUDIO DE RENDER de la Oficina de Canales, en la laptop de César (junto a la Máquina Lumi).

Qué hace mientras la laptop está prendida:
  1. Avisa a la oficina cada minuto que está en línea.
  2. Pide videos por armar (plantilla Máquina Lumi: LCMC y héroes de LCMC Games), los narra con la voz del
     canal, los arma con la potencia de la laptop y los sube a la oficina (la oficina los revisa y los sube a YouTube).
  3. Cada tantas horas le pide a la oficina un ciclo (alterna LCMC y LCMC Games) para que todo avance solo.
Si la laptop se apaga a media tarea, la oficina recupera ese video y lo arma ella. No se pierde nada.

Configuración en .env (en esta misma carpeta):
  OFICINA_URL=https://oficina-canales.onrender.com
  OFICINA_TOKEN=<el mismo token del panel>
  OFICINA_CICLO_HORAS=3        (0 = no pedir ciclos, solo armar)

Uso:  python oficina_worker.py           (o doble clic en iniciar_estudio.bat)
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

import requests

AQUI = Path(__file__).resolve().parent
BUNDLE = AQUI / "oficina_bundle"
TRABAJO = AQUI / "salida" / "_oficina"
VERSION = "1.0"
os.environ.setdefault("MAQUINA_POTENTE", "1")

try:
    from dotenv import load_dotenv
    load_dotenv(AQUI / ".env")
except ImportError:
    pass

URL = os.getenv("OFICINA_URL", "https://oficina-canales.onrender.com").rstrip("/")
TOKEN = os.getenv("OFICINA_TOKEN", "")
CICLO_H = float(os.getenv("OFICINA_CICLO_HORAS", "3") or 0)
NOMBRE = os.getenv("OFICINA_NOMBRE", socket.gethostname())[:40]
H = {"x-token": TOKEN}
VOCES = {"male": "es-MX-JorgeNeural", "female": "es-MX-DaliaNeural"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def api(method, path, **kw):
    kw.setdefault("timeout", 60)
    r = requests.request(method, URL + path, headers=H, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- plantilla (misma que la oficina)
def actualizar_bundle(version: str) -> None:
    marca = BUNDLE / ".version"
    if marca.exists() and marca.read_text().strip() == version:
        return
    log("⬇  actualizando la plantilla desde la oficina…")
    data = api("GET", "/worker/bundle.zip", timeout=300).content
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    zipfile.ZipFile(io.BytesIO(data)).extractall(BUNDLE)
    marca.write_text(version)
    for m in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
        del sys.modules[m]


def plantilla():
    if str(BUNDLE) not in sys.path:
        sys.path.insert(0, str(BUNDLE))
    from app.maquina import marca as M, render as R
    return M, R


# ---------------------------------------------------------------- voz
async def _tts(texto, destino, voz, vel):
    import edge_tts
    com = edge_tts.Communicate(texto, voz, rate=vel, boundary="WordBoundary")
    palabras = []
    with open(destino, "wb") as f:
        async for c in com.stream():
            if c["type"] == "audio":
                f.write(c["data"])
            elif c["type"] == "WordBoundary":
                palabras.append((c["text"], c["offset"] / 1e7))
    return palabras


def duracion(ruta: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(ruta)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def narrar(texto, destino: Path, voz, vel) -> dict:
    err = None
    for intento in range(5):
        try:
            pal = asyncio.run(_tts(texto, destino, voz, vel))
            d = duracion(destino)
            if d > 0:
                return {"audio": destino, "duracion": d, "palabras": pal, "real": True}
        except Exception as e:
            err = e
        time.sleep(2 + intento * 3)
    raise RuntimeError(f"la voz no respondió ({err})")


# ---------------------------------------------------------------- imágenes
def bajar(url: str, carpeta: Path, nombre: str) -> Path | None:
    if not url:
        return None
    destino = carpeta / nombre
    try:
        if url.startswith("drive:"):
            data = api("GET", "/worker/media", params={"u": url}, timeout=120).content
        else:
            r = requests.get(url, headers={"User-Agent": "OficinaDeCanales/1.0 (@LCMCGAMES)"}, timeout=60)
            r.raise_for_status()
            data = r.content
        destino.write_bytes(data)
        from PIL import Image
        Image.open(destino).convert("RGB").save(destino.with_suffix(".png"))
        return destino.with_suffix(".png")
    except Exception as e:
        log("   ⚠ no pude bajar una imagen:", str(e)[:80])
        return None


def panel_icono(src: Path, out: Path, M, w=980, h=460) -> Path:
    from PIL import Image, ImageDraw, ImageFilter
    bg = Image.new("RGB", (w, h), tuple(min(255, c + 18) for c in M.FONDO))
    ic = Image.open(src).convert("RGBA")
    side = min(int(h * 0.78), max(ic.size) * 3)
    ic = ic.resize((int(ic.width * side / max(ic.size)), int(ic.height * side / max(ic.size))), Image.LANCZOS)
    cx, cy, r = w // 2, h // 2, int(max(ic.size) * 0.62)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*M.CIAN, 110))
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    bg.paste(glow, (0, 0), glow)
    ImageDraw.Draw(bg).ellipse((cx - r * .86, cy - r * .86, cx + r * .86, cy + r * .86), outline=M.VERDE, width=6)
    bg.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    bg.save(out)
    return out


# ---------------------------------------------------------------- armar un video
def armar(tarea: dict) -> None:
    ch, jid, sp = tarea["channel"], tarea["job_id"], tarea["spec"]
    M, R = plantilla()
    M.usar_tema(sp["tema"])
    R.F.cache_clear()
    carpeta = TRABAJO / jid
    carpeta.mkdir(parents=True, exist_ok=True)
    log(f"🎬 {sp['titulo']}  ({sp['formato']}, {len(sp['escenas'])} escenas)")
    t0 = time.time()
    voz = VOCES.get(sp.get("voz", "male"), VOCES["male"])
    escenas, nars = [], []
    for i, e in enumerate(sp["escenas"]):
        e = dict(e)
        if e.get("icono_url"):
            p = bajar(e["icono_url"], carpeta, f"icono_{i:02d}")
            if p:
                e["imagen"] = str(panel_icono(p, carpeta / f"panel_{i:02d}.png", M))
        if not e.get("imagen") and e.get("imagen_url"):
            p = bajar(e["imagen_url"], carpeta, f"arte_{i:02d}")
            if p:
                e["imagen"] = str(p)
        escenas.append(e)
        nars.append(narrar(e["narracion"], carpeta / f"voz_{i:02d}.mp3", voz, sp.get("velocidad", "+8%")))
    musica = AQUI / "assets" / "musica.mp3"
    out, dur = R.renderizar(escenas, nars, sp["formato"], carpeta / "final.mp4", carpeta,
                            musica if musica.exists() else None, fps=sp.get("fps", 30))
    if sp.get("miniatura"):
        try:
            from app.maquina.miniatura import generar_miniatura
            th = generar_miniatura({"titulo": sp["titulo"], "miniatura": sp["miniatura"]}, carpeta / "miniatura.jpg")
            with open(th, "rb") as fh:
                api("POST", f"/worker/result/{ch}/{jid}", params={"kind": "thumb"}, data=fh, timeout=300)
        except Exception as e:
            log("   ⚠ miniatura:", str(e)[:100])
    log(f"   ✓ armado en {int(time.time() - t0)} s ({dur:.0f} s de video); subiendo a la oficina…")
    with open(out, "rb") as fh:
        api("POST", f"/worker/result/{ch}/{jid}", params={"kind": "video", "seconds": int(time.time() - t0)},
            data=fh, timeout=1800)
    log("   ✓ entregado")
    shutil.rmtree(carpeta, ignore_errors=True)


# ---------------------------------------------------------------- bucle principal
def main():
    if not TOKEN:
        sys.exit("Falta OFICINA_TOKEN en .env (es el mismo token que usas en el panel)")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("Falta ffmpeg en esta PC (winget install ffmpeg)")
    log(f"💻 Estudio de render «{NOMBRE}» conectado a {URL}")
    ultimo_ciclo, canal = 0.0, 0
    while True:
        try:
            hb = api("POST", "/worker/heartbeat", json={"name": NOMBRE, "version": VERSION}).json()
            actualizar_bundle(hb.get("bundle", ""))
            tarea = api("POST", "/worker/next", timeout=120).json()
            if tarea.get("job_id"):
                try:
                    armar(tarea)
                except Exception as e:
                    traceback.print_exc()
                    api("POST", f"/worker/failed/{tarea['channel']}/{tarea['job_id']}", json={"error": str(e)[:300]})
                if not hb.get("running"):
                    try:
                        api("POST", f"/run/{tarea['channel']}")   # que la oficina siga: crítico, miniatura, YouTube
                    except Exception:
                        pass
                continue
            if CICLO_H and not hb.get("running") and time.time() - ultimo_ciclo > CICLO_H * 3600:
                ch = ("lcmc", "gaming")[canal % 2]
                canal += 1
                try:
                    api("POST", f"/run/{ch}")
                    log(f"▶ pedí un ciclo de {ch}")
                    ultimo_ciclo = time.time()
                except requests.HTTPError as e:
                    log("   ciclo no iniciado:", e.response.status_code)
        except requests.RequestException as e:
            log("⚠ sin conexión con la oficina:", str(e)[:100])
        except Exception:
            traceback.print_exc()
        time.sleep(60)


if __name__ == "__main__":
    main()
