from ..config import ChannelConfig
from ..models import VideoJob
from . import gameplay, generated


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    if cfg.production.kind == "gameplay" and job.idea.source_media:
        return gameplay.run(cfg, job)   # partida grabada; los videos de héroes (wiki) se generan
    return generated.run(cfg, job)
