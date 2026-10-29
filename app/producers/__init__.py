from ..config import ChannelConfig
from ..models import VideoJob
from . import gameplay, generated


def run(cfg: ChannelConfig, job: VideoJob) -> VideoJob:
    if cfg.production.kind == "gameplay":
        return gameplay.run(cfg, job)
    return generated.run(cfg, job)
