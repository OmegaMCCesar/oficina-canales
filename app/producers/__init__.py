from ..config import ChannelConfig
from ..models import VideoJob
from . import gameplay, generated


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    if cfg.production.kind == "gameplay" and job.idea.source_media:
        if job.idea.format == "long":
            from . import fullmatch
            return fullmatch.run(cfg, job)   # partida completa editada (video largo)
        return gameplay.run(cfg, job)   # partida grabada; los videos de héroes (wiki) se generan
    return generated.run(cfg, job)
