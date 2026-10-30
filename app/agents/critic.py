"""CRÍTICO DE VIDEO: VE el video ya renderizado (hoja con 9 cuadros) antes de subirlo.

- Califica con una rúbrica: claridad, variedad, legibilidad, coherencia con lo narrado, calidad de imagen.
- Deja LECCIONES en la memoria del canal (tipo "visual") que el Animador y el Guionista leen en el
  siguiente video: así la oficina aprende de sus propios errores sin esperar métricas.
- Si el video sale muy mal (≤ 4/10) y aún no se rehizo, pide rehacer la animación con sus notas.
Usa modelos gratuitos con visión (Groq Llama 4 Scout, GitHub gpt-4.1-mini, Gemini, Cloudflare...).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel

from ..config import ChannelConfig, settings
from ..db import add_activity, insights, save_insight, save_job, store
from ..models import Insight, VideoJob
from ..tools import ffmpeg
from ..tools.tts import duration
from .base import channel_brief

log = logging.getLogger("critic")


class Problem(BaseModel):
    frame: int            # número de cuadro (1-9)
    issue: str


class Lesson(BaseModel):
    statement: str        # regla general para futuros videos ("Las tarjetas de solo texto aburren")
    action: str           # qué hacer distinto ("Usa el arte oficial de fondo con el dato encima")


class VideoReview(BaseModel):
    score: int            # 1-10
    problems: list[Problem] = []
    fixes: list[str] = []         # cambios concretos para ESTE video
    lessons: list[Lesson] = []    # 0-3 reglas para los siguientes
    summary: str


SYSTEM = """Eres el CRÍTICO DE VIDEO de un canal de YouTube. Ves 9 cuadros tomados a lo largo del video (en orden,
con su segundo) y el guion. Califica como lo haría un editor exigente que conoce qué retiene en YouTube:
1) ¿Cada cuadro se entiende en 1 segundo? ¿Hay un sujeto claro o es una pantalla vacía/aburrida?
2) Variedad: ¿cambia lo que se ve o se repite lo mismo (misma tarjeta, mismo fondo)?
3) Legibilidad: subtítulos y textos legibles en celular; ¿algo tapa lo importante?
4) Coherencia: ¿lo que se ve ilustra lo que se narra en ese momento?
5) Calidad de imagen: pixelado, borroso, estirado, recortado (personajes cortados), colores lavados.
Sé concreto y útil: cada problema con su número de cuadro. Las lecciones deben ser reglas generales que mejoren
los siguientes videos (no repitas lecciones obvias ya conocidas). score: 1-10 (7+ = publicable)."""


def contact_sheet(video: Path, out: Path, n: int = 9) -> tuple[Path, list[float]]:
    total = duration(video) or 10
    times = [total * (k + 0.5) / n for k in range(n)]
    frames = []
    for k, t in enumerate(times):
        f = out.with_name(f"crit_{k}.jpg")
        try:
            ffmpeg.frame_at(video, t, f)
            frames.append(Image.open(f).convert("RGB"))
        except Exception:
            frames.append(Image.new("RGB", (640, 360), (0, 0, 0)))
        finally:
            f.unlink(missing_ok=True)
    portrait = frames[0].height > frames[0].width
    cw, ch = (240, 427) if portrait else (400, 225)
    sheet = Image.new("RGB", (cw * 3 + 8, ch * 3 + 8), (30, 30, 30))
    d = ImageDraw.Draw(sheet)
    f = ffmpeg.font(18)
    for k, im in enumerate(frames):
        x, y = (k % 3) * (cw + 4), (k // 3) * (ch + 4)
        sheet.paste(im.resize((cw, ch)), (x, y))
        d.rectangle([x, y, x + 74, y + 26], fill=(255, 214, 40))
        d.text((x + 5, y + 3), f"{k+1} · {int(times[k])}s", font=f, fill=(0, 0, 0))
    sheet.save(out, quality=82)
    return out, times


def _narration_at(job: VideoJob, times: list[float], total: float) -> str:
    sc = job.script
    scenes = sc.scenes or [{"text": h.get("voiceover", "")} for h in sc.highlights]
    words = [len(s["text"].split()) for s in scenes] or [1]
    acc, bounds = 0, []
    for w in words:
        bounds.append(acc / sum(words) * total)
        acc += w
    lines = []
    for k, t in enumerate(times):
        idx = max(i for i, b in enumerate(bounds) if b <= t) if bounds else 0
        lines.append(f"Cuadro {k+1} ({int(t)}s): «{scenes[idx]['text'][:140]}»")
    return "\n".join(lines)


def _remember(cfg: ChannelConfig, job: VideoJob, lessons: list[Lesson]) -> int:
    known = [i.statement.lower() for i in insights(cfg.id, limit=100) if i.kind == "visual"]
    added = 0
    for l in lessons[:3]:
        st = l.statement.strip()
        if not st or any(SequenceMatcher(None, st.lower(), k).ratio() > 0.72 for k in known):
            continue
        save_insight(Insight(channel_id=cfg.id, kind="visual", statement=st[:220], action=l.action[:220],
                             evidence=f"crítico:{job.id}", confidence=0.6,
                             expires_at=datetime.now(timezone.utc) + timedelta(days=60)))
        known.append(st.lower())
        added += 1
    return added


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    if not job.video_path or not Path(job.video_path).exists() or settings().MODE == "dry":
        return job
    from ..providers import vision
    folder = Path(job.video_path).parent
    sheet, times = contact_sheet(Path(job.video_path), folder / "critica.jpg")
    total = duration(Path(job.video_path)) or times[-1]
    user = f"""{channel_brief(cfg)}
ESTILO ESPERADO: {"caricatura 2D fondo blanco con Lumi protagonista" if cfg.production.style == "doodle" else "arte oficial del héroe y tarjetas de habilidad" if job.idea.hero else "video explicativo"}
VIDEO: «{job.script.title}» ({job.idea.format}, {int(total)} s)
LO QUE SE NARRA EN CADA CUADRO:
{_narration_at(job, times, total)}"""
    try:
        r = vision.ask(SYSTEM, user, sheet, VideoReview)
    except Exception as e:
        log.warning("[%s] crítico sin modelo de visión (%s)", cfg.id, e)
        add_activity(cfg.id, "critico", "No pude ver el video (sin modelo de visión disponible)", kind="ask")
        return job
    added = _remember(cfg, job, r.lessons)
    review = {"score": r.score, "summary": r.summary, "problems": [p.model_dump() for p in r.problems][:9],
              "fixes": r.fixes[:6], "lessons_added": added, "at": datetime.now(timezone.utc).isoformat()}
    job.qa = {**job.qa, "video_review": review}
    store().put(cfg.id, "reviews", job.id, {"job_id": job.id, "title": job.script.title, **review})
    msg = f"Vi el video: {r.score}/10 · {r.summary[:70]}"
    add_activity(cfg.id, "critico", msg, to="director", kind="ok" if r.score >= 7 else "ask")
    if added:
        add_activity(cfg.id, "critico", f"Aprendí {added} lección(es) para los siguientes videos", to="animador")
    # muy malo y aún no se rehizo: una sola vuelta más con las notas del Crítico
    if r.score <= 4 and not job.qa.get("redo_by_critic") and cfg.production.style == "doodle" and job.script.scenes:
        job.qa["redo_by_critic"] = True
        job.status, job.anim_redo = "scripted", True
        job.anim_notes = ("Crítico de video: " + "; ".join(r.fixes[:4]))[:500]
        job.video_path = None
        add_activity(cfg.id, "director", "Se rehace la animación con las notas del Crítico", to="animador", kind="ask")
    save_job(job)
    return job
