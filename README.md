# Oficina de Canales

Equipo de agentes que opera canales de YouTube: elige temas, escribe, produce, hace miniaturas,
programa la publicación y **aprende de las métricas**. Un mismo sistema, varios canales
(cada uno se configura en `channels/*.yaml`).

Canales configurados:
- `lcmc`: LCMC - Servir para Servir (línea blanca, Lumi). Producción 100% generada.
- `gaming`: MLBB / Magic Chess. Se produce a partir de tus partidas grabadas.

## La oficina

| Agente | Qué hace | Archivo |
|---|---|---|
| Analista | Marca lo publicado, junta métricas 24h/7d/30d y escribe **insights** en la memoria del canal | `app/agents/analyst.py` |
| Investigador | Propone 6-10 ideas con score, a partir de las semillas, los comentarios, la memoria y (en gaming) las partidas nuevas | `app/agents/researcher.py` |
| Director | Aprueba o rechaza ideas, controla la cola, asigna horarios según la cadencia y te pide material | `app/agents/director.py` |
| Guionista | Escribe guion por escenas (LCMC) o analiza la partida y saca highlights con timestamps (gaming) | `app/agents/writer.py` |
| Productor | Imagen + voz + ffmpeg (LCMC) o cortes 9:16 + rótulos + locución (gaming) | `app/producers/` |
| Miniaturas | 2 variantes, elige 1 según lo que ha dado CTR | `app/agents/thumbnails.py` |
| Publicador | Sube como **privado programado**, nunca inmediato | `app/agents/publisher.py` |

Ciclo diario (`app/orchestrator.py`): Analista → Investigador → Director → Guionista → Productor → Miniaturas → Director (horarios) → Publicador.

### Para no "matar" videos entre sí
El Director nunca publica en ráfaga. Las reglas de `cadence` en el yaml de cada canal son límites duros:
`shorts_per_day`, `longs_per_week`, `publish_hours_local`, `min_gap_hours`, `max_queue_ready`.
Puede haber 10 videos listos, pero solo se programan en sus horarios.

### Cómo aprende
El Analista guarda insights como *"temas de refrigerador que no enfría retienen 65%; acción: hacer secuela"*.
Todos los agentes leen esa memoria en cada ciclo. Con ~20 videos publicados ya hay señal útil.

## Probar sin credenciales (modo dry)
```bash
pip install -r requirements.txt
cp .env.example .env            # MODE=dry
python scripts/run_once.py lcmc --no-publish
```
Genera videos de prueba en `media/lcmc/<job>/final.mp4` y guarda el estado en `data/db_lcmc.json`.

## Pasar a real (modo live)
1. **Google Cloud Console**: habilita *YouTube Data API v3*, *YouTube Analytics API*, *Cloud Text-to-Speech*.
   Crea credenciales OAuth tipo *Aplicación de escritorio*, descarga el JSON como `client_secret.json`.
   En la pantalla de consentimiento agrega tu correo como usuario de prueba.
2. **Firebase**: service account JSON como `firebase-sa.json`.
3. **Gemini**: API key en `.env`.
4. Autoriza cada canal (elige el canal correcto cuando Google pregunte):
   ```bash
   python scripts/auth_youtube.py lcmc
   python scripts/auth_youtube.py gaming
   ```
   Copia el `youtube_channel_id` que imprime al yaml del canal.
5. `MODE=live` en `.env` y corre un ciclo: `python scripts/run_once.py lcmc`

> Nota: la API de YouTube sube los videos de proyectos no verificados como **privados** hasta que
> Google audite la app. Programado/privado funciona igual; para que se publiquen solos al llegar la hora
> hay que pedir la auditoría de la API (formulario de YouTube API Services).
> Cuota por defecto: 10,000 unidades/día; cada subida cuesta ~1,600, así que caben ~6 subidas/día.

## Gaming: tu parte
Graba partidas (MLBB o Magic Chess) y deja los `.mp4` en `media/gaming/inbox/`
(o la carpeta que pongas en `GAMING_INBOX`). Si no hay material, el Director no inventa:
te deja en `requests_to_human` qué grabar.

## Panel 3D de la oficina
`GET /panel` abre la vista aérea: dos salas (LCMC y Gaming) con un robot por agente que camina a
entregar trabajo según lo que pasa en la cola. Pulsa **Conectar API**, pega tu `API_TOKEN` y se
actualiza cada 20 s. Sin conexión corre en modo demo. Toca un robot para ver qué hace y su bandeja.

## Desplegar en Render
`render.yaml` crea la API y un Cron Job diario. Endpoints (header `x-token: API_TOKEN`):
- `POST /run` o `POST /run/{canal}`: ejecuta un ciclo
- `GET /channels/{canal}/queue`: cola y estados
- `GET /channels/{canal}/insights`: memoria del canal
- `GET /channels/{canal}/metrics`, `/decisions`
- `POST /channels/{canal}/jobs/{id}/reject`: veto manual

Para producir videos se necesita disco: en Render usa un Persistent Disk montado en `media/`,
o corre el ciclo en tu PC y deja Render solo para la API.
