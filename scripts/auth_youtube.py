"""Autoriza un canal de YouTube y guarda su token.

Uso:  python scripts/auth_youtube.py lcmc
Se abre el navegador: entra con la cuenta de Google y ELIGE EL CANAL CORRECTO
(ej. @LCMC-ServirParaServir, no el otro canal de la misma cuenta).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from app.config import settings  # noqa: E402
from app.tools.youtube import SCOPES, token_path  # noqa: E402

if len(sys.argv) < 2:
    sys.exit("Uso: python scripts/auth_youtube.py <channel_id>")
cid = sys.argv[1]

flow = InstalledAppFlow.from_client_secrets_file(settings().YOUTUBE_CLIENT_SECRETS, SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
token_path(cid).parent.mkdir(exist_ok=True)
token_path(cid).write_text(creds.to_json())

yt = build("youtube", "v3", credentials=creds)
ch = yt.channels().list(part="snippet", mine=True).execute()["items"][0]
print(f"OK: token guardado para '{cid}'")
print(f"Canal autorizado: {ch['snippet']['title']}  id={ch['id']}  handle={ch['snippet'].get('customUrl')}")
print(f"Pon youtube_channel_id: {ch['id']} en channels/{cid}.yaml")
