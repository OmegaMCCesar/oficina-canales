"""Ejecuta un ciclo desde la terminal.

  python scripts/run_once.py            # todos los canales
  python scripts/run_once.py lcmc       # uno
  python scripts/run_once.py lcmc --no-publish
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(name)-18s %(message)s")

from app import orchestrator  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
publish = "--no-publish" not in sys.argv
res = [orchestrator.run_channel(args[0], publish)] if args else orchestrator.run_all(publish)
print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
