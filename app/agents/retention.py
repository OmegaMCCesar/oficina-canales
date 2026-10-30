"""EDITOR DE RETENCIÓN Y ENGAGEMENT.

Toma el guion del Guionista y lo convierte en algo que la gente vea hasta el final:
- Gancho que pone tensión o el resultado en el PRIMER segundo (texto grande en pantalla).
- Ritmo: un cambio visual cada 2-3 s (escenas cortas de una frase).
- Shorts: sin intro, final que conecta con el inicio (loop) para repeticiones.
- Largos: banner de marca después del gancho, CTA a mitad y CTA final.
- Pregunta concreta para provocar comentarios (engagement).
- Por escena: consulta de stock en inglés y si conviene video o imagen.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from ..config import ChannelConfig
from ..db import save_job
from ..llm import ask_json
from ..models import VideoJob
from .base import channel_brief, insights_block


def studio_catalog() -> str:
    from ..tools.anim import ASSETS
    folder = ASSETS / "studio"
    if not folder.exists():
        return ""
    return ", ".join(sorted(p.stem for p in folder.iterdir() if p.suffix.lower() in (".mp4", ".webm", ".mov")))

log = logging.getLogger("retention")


class RScene(BaseModel):
    text: str               # una frase narrada (máx ~14 palabras)
    on_screen: str = ""     # 1-3 palabras clave a resaltar (opcional)
    visual: str = "anim"    # anim | studio | video | image
    anim: str = ""          # plantilla del Estudio Animador (si visual=anim)
    anim_params_json: str = "{}"  # parámetros de la plantilla en JSON
    studio_clip: str = ""   # nombre de clip del Studio Quinto Sol (si visual=studio)
    lumi: str = ""          # expresión de Lumi en esta escena ("" = sin Lumi)
    stock_query: str = ""   # búsqueda en INGLÉS para stock (2-4 palabras concretas)
    visual_prompt: str = "" # descripción para IA de imagen


class RetentionOut(BaseModel):
    hook_text: str          # máx 6 palabras, en pantalla 0-2.5 s
    scenes: list[RScene]
    cta_mid: str = ""       # solo largos
    cta_end: str            # frase final hablada
    comment_question: str   # pregunta abierta y específica
    loop_line: str = ""     # shorts: última frase que empata con la primera
    notes: str              # qué cambiaste y por qué


SYSTEM = """Eres el EDITOR DE RETENCIÓN de un canal de YouTube. Tu métrica es el % visto y las
repeticiones. Reglas con datos:
- La retención se decide en los primeros 3 segundos: la PRIMERA escena es el gancho, y debe mostrar
  el problema o el resultado ya (nada de "hola", "en este video", "bienvenidos").
- hook_text: máximo 6 palabras, en mayúsculas mentales, que provoquen "¿y luego?".
- Un cambio visual cada 2-3 s: escenas de UNA frase corta (≤14 palabras). Shorts: 10-18 escenas.
  Largos: 35-70 escenas.
- Shorts: duran lo mínimo que la idea necesita (20-50 s); sin intro; la última frase (loop_line)
  debe empatar con la primera para que el video se repita sin notarse.
- Largos: CTA de suscripción a mitad (cta_mid, corto y con beneficio concreto) y CTA final.
- cta_end: corto, con beneficio ("Sígueme para el siguiente: cómo probar el capacitor").
- comment_question: pregunta específica que la audiencia pueda contestar en una línea
  ("¿Qué marca es tu refri y qué código te marca?").
- stock_query SIEMPRE en inglés, concreto y filmable ("refrigerator compressor closeup",
  "technician multimeter"). visual=video cuando haya acción; image para conceptos.
- Conserva la información técnica correcta y las advertencias de seguridad del guion.

VISUALES (en este orden de preferencia):
1) visual=anim — Estudio Animador propio (gratis, se ve profesional). Úsalo en la MAYORÍA de escenas técnicas.
   Plantillas y anim_params_json:
   - ciclo_refrigeracion {"highlight":"compresor|condensador|capilar|evaporador","falla":"texto corto"}
   - lavadora {"modo":"lavado|centrifugado|desagüe|atorada"}
   - minisplit {"goteo":true|false,"flujo":"frio|caliente"}
   - multimetro {"valor":"12.4","unidad":"Ω|V|A|µF","estado":"ok|falla"}
   - termometro {"desde":4,"hasta":16,"unidad":"°C"}
   - lista {"titulo":"3 causas","items":["...","...","..."]}  (máx 5 items cortos)
   - titulo {"texto":"frase corta","sub":"subtítulo"}
2) visual=studio — clip hecho a mano en el Studio Quinto Sol (solo si existe en el catálogo).
3) visual=image — imagen IA (visual_prompt) para lo que no cubren las plantillas.
LUMI (personaje del canal): elige su expresión por escena según la emoción:
feliz, analizando, pensando, sorpresa, idea, confirmacion, advertencia, listo, aprendizaje, diagnostico,
solucion, celebracion. Usa "advertencia" en avisos de seguridad, "diagnostico" al medir, "solucion" al
resolver y "celebracion" en el cierre. Deja lumi vacío en 1 de cada 4 escenas para no saturar."""


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    sc = job.script
    if not sc or cfg.production.kind != "generated":
        return job  # gameplay: los highlights ya son el ritmo; solo se agregan CTA abajo
    fmt = job.idea.format
    user = f"""{channel_brief(cfg)}
{insights_block(cfg.id, 12)}
FORMATO: {fmt}
TÍTULO: {sc.title}
GUION ORIGINAL (por escenas):
{chr(10).join(f'{i+1}. {s["text"]}' for i, s in enumerate(sc.scenes))}
CTA ORIGINAL: {sc.cta}
CATÁLOGO DEL STUDIO QUINTO SOL: {studio_catalog() or '(vacío)'}"""
    tpl = ["titulo", "ciclo_refrigeracion", "multimetro", "lista", "termometro", "lavadora", "minisplit"]
    fb_scenes = [RScene(text=s["text"], stock_query="appliance repair technician", visual_prompt=s.get("visual_prompt", ""),
                        visual="anim", anim=tpl[i % len(tpl)],
                        anim_params_json='{"texto": "%s"}' % s["text"][:40].replace('"', "") if i % len(tpl) == 0 else "{}",
                        lumi=["feliz", "diagnostico", "", "idea", "solucion"][i % 5]) for i, s in enumerate(sc.scenes)]
    fallback = RetentionOut(hook_text=" ".join(sc.title.split()[:5]), scenes=fb_scenes,
                            cta_mid="Suscríbete para más fallas resueltas" if fmt == "long" else "",
                            cta_end=sc.cta, comment_question="¿Qué falla tiene tu equipo? Te leo en comentarios",
                            loop_line="", notes="dry-run")
    try:
        out = ask_json(SYSTEM, user, RetentionOut, fallback=fallback)
    except Exception as e:
        log.warning("[%s] retención no disponible (%s); sigo con el guion original", cfg.id, e)
        return job
    scenes = [s.model_dump() for s in out.scenes if s.text.strip()]
    if out.cta_end and (not scenes or out.cta_end.strip() not in scenes[-1]["text"]):
        scenes.append({"text": out.cta_end, "on_screen": "", "visual": "anim", "anim": "titulo",
                       "anim_params_json": '{"texto": "SÍGUEME", "sub": "%s"}' % cfg.production.brand_name,
                       "lumi": "celebracion", "stock_query": "", "visual_prompt": ""})
    if fmt == "short" and out.loop_line:
        first = dict(scenes[0])
        first["text"] = out.loop_line
        scenes.append(first)
    sc.scenes = scenes
    sc.body = " ".join(s["text"] for s in scenes)
    sc.hook_text = out.hook_text
    sc.cta_mid = out.cta_mid
    sc.cta_end = out.cta_end
    sc.comment_question = out.comment_question
    sc.loop_line = out.loop_line
    sc.retention_notes = out.notes
    save_job(job)
    log.info("[%s] retención: %d escenas, gancho '%s'", cfg.id, len(scenes), out.hook_text)
    return job
