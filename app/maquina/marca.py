"""Identidad visual de la Máquina Lumi (plantilla que César hizo para LCMC), con temas por canal.

usar_tema("lcmc") o usar_tema("games") cambia colores y marca antes de renderizar."""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ASSETS = RAIZ / "assets" / "maquina"
POSES_DIR = ASSETS / "lumi"
FUENTES_DIR = ASSETS / "fuentes"

FONDO = (2, 10, 25)
AZUL = (10, 91, 255)
CIAN = (0, 212, 255)
VERDE = (124, 243, 61)
LIMA = (184, 255, 105)
BLANCO = (255, 255, 255)
GRIS = (167, 178, 196)
AMARILLO = (255, 214, 10)
MARCA_1, MARCA_2 = "LCMC", "Servir para Servir"

TEMAS = {
    "lcmc": dict(FONDO=(2, 10, 25), AZUL=(10, 91, 255), CIAN=(0, 212, 255), VERDE=(124, 243, 61),
                 LIMA=(184, 255, 105), GRIS=(167, 178, 196), MARCA_1="LCMC", MARCA_2="Servir para Servir"),
    # LCMC Games: morado noche + cian + dorado (el logo del canal)
    "games": dict(FONDO=(10, 6, 28), AZUL=(124, 58, 237), CIAN=(34, 211, 238), VERDE=(245, 165, 36),
                  LIMA=(255, 224, 138), GRIS=(201, 195, 255), MARCA_1="LCMC", MARCA_2="GAMES"),
}

FORMATOS = {"largo": (1920, 1080), "corto": (1080, 1920)}
FPS = 30

POSES = ["principal", "feliz", "analizando", "pensando", "sorpresa", "idea", "confirmacion", "advertencia",
         "listo", "aprendizaje", "diagnostico", "solucion", "celebracion"]


def usar_tema(nombre: str) -> None:
    for k, v in TEMAS.get(nombre, TEMAS["lcmc"]).items():
        globals()[k] = v


def fuente(peso="Bold"):
    for c in (FUENTES_DIR / f"Poppins-{peso}.ttf", FUENTES_DIR / "Poppins-Bold.ttf",
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), RAIZ / "assets" / "DejaVuSans-Bold.ttf"):
        if c.exists():
            return str(c)
    raise FileNotFoundError("falta Poppins-Bold.ttf en app/assets/maquina/fuentes")
