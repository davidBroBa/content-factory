# 🚀 Guía de Instalación Completa — Content Factory

## Requisitos mínimos del servidor
- Ubuntu 22.04 / Debian 12 / Rocky Linux 9
- RAM: 8 GB (16 GB recomendado para render simultáneo)
- CPU: 4 cores (8 recomendado)
- Disco: 100 GB SSD (videos, assets, modelos TTS)
- Docker 24+ y Docker Compose 2.20+

---

## PASO 1 — Estructura de directorios

```bash
sudo mkdir -p /opt/factory/{minio,logs,backups}
sudo chown -R $USER:$USER /opt/factory

# Clonar / crear el proyecto
mkdir -p ~/content-factory
cd ~/content-factory
```

Estructura final esperada:
```
~/content-factory/
├── docker-compose.yml
├── .env                    ← tus secretos (no commitear)
├── .env.example
├── init-db.sh
├── sql/
│   └── schema.sql
├── workers/
│   ├── tts/
│   │   ├── Dockerfile
│   │   ├── worker.py
│   │   └── requirements.txt
│   └── render/
│       ├── Dockerfile
│       ├── worker.py
│       └── requirements.txt
└── n8n-workflows/
    ├── 00_error_handler.json
    ├── 01_main_orchestrator.json
    ├── 02_researcher.json
    ├── 03_scriptwriter.json
    ├── 04_producer.json
    ├── 05_publisher.json
    └── 06_analytics.json
```

---

## PASO 2 — Variables de entorno

```bash
cp .env.example .env
nano .env
```

**Campos obligatorios a rellenar:**
```
POSTGRES_PASSWORD=tu_password_seguro
REDIS_PASSWORD=tu_redis_password
MINIO_ROOT_PASSWORD=tu_minio_password
N8N_PASSWORD=tu_n8n_password
N8N_ENCRYPTION_KEY=exactamente_32_caracteres_aqui_!!
OPENROUTER_API_KEY=sk-or-xxxxx
YOUTUBE_API_KEY=AIzaSyXXXXXX
PEXELS_API_KEY=xxxxxxxxxx
BLOTATO_API_KEY=xxxxxxxxxx
```

**Generar N8N_ENCRYPTION_KEY segura:**
```bash
openssl rand -hex 16
# Resultado: clave de exactamente 32 caracteres hex
```

---

## PASO 3 — Preparar init-db.sh

```bash
chmod +x init-db.sh
```

---

## PASO 4 — Levantar la infraestructura base

```bash
# Primera vez: construir e iniciar todo
docker compose --env-file .env up -d postgres redis minio

# Esperar a que estén sanos (30-60 segundos)
docker compose ps

# Verificar logs
docker compose logs postgres --tail=20
docker compose logs redis --tail=10
docker compose logs minio --tail=10
```

**Verificar MinIO:**
- Abrir http://localhost:9001
- Login: `factory_minio` / tu password de MinIO
- Verificar que existen los buckets: `factory-audio`, `factory-video`, `factory-assets`, `factory-thumbnails`

---

## PASO 5 — Inicializar la base de datos

```bash
# Verificar que el schema se aplicó automáticamente
docker exec -it cf_postgres psql -U factory -d factory -c "\dt factory.*"

# Deberías ver las tablas:
# topics, scripts, scenes, assets, audios, videos,
# publications, metrics, job_queue, logs, errors, seo_outputs
```

Si el schema no se aplicó automáticamente:
```bash
docker exec -i cf_postgres psql -U factory -d factory < sql/schema.sql
```

---

## PASO 6 — Levantar n8n y Whisper

```bash
docker compose --env-file .env up -d n8n whisper

# Esperar inicialización
sleep 30
docker compose logs n8n --tail=30
```

**Acceder a n8n:**
- URL: http://localhost:5678
- Usuario: `admin` / tu N8N_PASSWORD

**Configurar credencial PostgreSQL en n8n:**
1. Settings → Credentials → Add Credential
2. Tipo: `PostgreSQL`
3. Name: `Factory PostgreSQL`
4. Host: `postgres`
5. Port: `5432`
6. Database: `factory`
7. User: `factory`
8. Password: tu POSTGRES_PASSWORD
9. Schema: `factory`
10. Guardar y hacer Test

---

## PASO 7 — Construir y levantar los workers

```bash
# Construir imágenes (puede tardar 5-15 minutos por descarga de modelos)
docker compose --env-file .env build tts_worker render_worker

# Levantar workers
docker compose --env-file .env up -d tts_worker render_worker

# Verificar que están corriendo
docker compose logs tts_worker --tail=20
docker compose logs render_worker --tail=20

# Health checks
curl http://localhost:8081/health   # TTS Worker
curl http://localhost:8080/health   # Render Worker
```

**Respuesta esperada del health check:**
```json
{"status": "ok", "kokoro_available": true, "timestamp": "..."}
{"status": "ok", "ffmpeg": true, "ffmpeg_path": "/usr/bin/ffmpeg"}
```

---

## PASO 8 — Importar workflows en n8n

En la interfaz de n8n:

1. **Menú lateral → Workflows → Import from file**
2. Importar en este orden:
   - `00_error_handler.json`
   - `01_main_orchestrator.json`
   - `02_researcher.json`
   - `03_scriptwriter.json`
   - `04_producer.json`
   - `05_publisher.json`
   - `06_analytics.json`

3. **Para cada workflow importado:**
   - Verificar que las credenciales de PostgreSQL estén asignadas
   - Verificar que las variables de entorno (`$env.OPENROUTER_API_KEY`, etc.) estén configuradas
   - Activar el workflow con el toggle

---

## PASO 9 — Configurar variables de entorno en n8n

En n8n: **Settings → Environment Variables** (o directamente en el `docker-compose.yml`)

Las variables ya están pasadas vía `environment:` en el compose. Verificar que n8n las reconoce:

```bash
# Desde un Code node en n8n, ejecutar:
return [{ json: { 
  openrouter: !!$env.OPENROUTER_API_KEY,
  youtube: !!$env.YOUTUBE_API_KEY,
  pexels: !!$env.PEXELS_API_KEY,
  tts_url: $env.TTS_WORKER_URL,
  render_url: $env.WORKER_API_URL
}}];
```

---

## PASO 10 — Vincular subworkflows

Después de importar, cada workflow tiene un ID único. Necesitas actualizar el Orchestrator con los IDs reales:

1. Ir a **02 - Researcher Agent** → copiar su ID desde la URL
2. Ir a **03 - Scriptwriter Agent** → copiar su ID
3. Ir a **04 - Producer** → copiar su ID
4. Ir a **05 - Publisher** → copiar su ID

Luego en **01 - Main Orchestrator**, editar los nodos `ExecuteWorkflow` y pegar los IDs correspondientes.

**Alternativa más robusta:** Agregar estas variables de entorno en el compose:
```
WORKFLOW_ID_RESEARCHER=ID_del_workflow_researcher
WORKFLOW_ID_SCRIPTWRITER=ID_del_workflow_scriptwriter
WORKFLOW_ID_PRODUCER=ID_del_workflow_producer
WORKFLOW_ID_PUBLISHER=ID_del_workflow_publisher
```

---

## PASO 11 — Configurar el Error Handler

1. Abrir **01 - Main Orchestrator**
2. Settings (rueda dentada) → **Error Workflow**
3. Seleccionar `00 - Error Handler`
4. Repetir para los workflows: 02, 03, 04, 05

---

## PASO 12 — Test de integración completo

### Test 1: TTS Worker
```bash
curl -X POST http://localhost:8081/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Este es un caso de prueba para la fábrica de contenido.",
    "voice": "af_heart",
    "script_id": "00000000-0000-0000-0000-000000000001",
    "lang": "e"
  }'

# Respuesta esperada:
# {"success": true, "audio_id": "...", "storage_url": "http://...", "duration_s": 3.5}
```

### Test 2: Whisper (STT)
```bash
# Primero necesitas una URL de audio accesible
curl -X POST http://localhost:8000/v1/audio/transcriptions \
  -F "url=http://minio:9000/factory-audio/test.wav" \
  -F "language=es" \
  -F "response_format=verbose_json"
```

### Test 3: Render Worker
```bash
curl -X POST http://localhost:8080/render \
  -H "Content-Type: application/json" \
  -d '{
    "video_id": "00000000-0000-0000-0000-000000000002",
    "script_id": "00000000-0000-0000-0000-000000000001",
    "topic_id": "00000000-0000-0000-0000-000000000000",
    "audio_url": "TU_URL_AUDIO_DE_TEST",
    "scenes": [
      {
        "scene_number": 1,
        "start_s": 0,
        "end_s": 7,
        "asset_url": "https://www.pexels.com/video/854982/download/",
        "theme": "test scene"
      }
    ],
    "subtitles": [
      {"start": 0.0, "end": 3.5, "text": "Este es un test"},
      {"start": 3.5, "end": 7.0, "text": "de la fábrica de contenido"}
    ],
    "ken_burns": true,
    "add_subtitles": true
  }'
```

### Test 4: Pipeline completo desde n8n
1. Ir a **01 - Main Orchestrator**
2. Hacer clic en **Execute Workflow**
3. Monitorear en la vista de ejecuciones
4. Verificar en PostgreSQL:
```sql
SELECT * FROM factory.v_pipeline_status LIMIT 5;
```

---

## PASO 13 — Levantar Watchtower (opcional)

```bash
docker compose --env-file .env up -d watchtower
```

---

## PASO 14 — Verificación final del sistema

```bash
# Estado de todos los contenedores
docker compose ps

# Debe mostrar todos en estado "healthy" o "running":
# cf_postgres      running (healthy)
# cf_redis         running (healthy)
# cf_minio         running (healthy)
# cf_n8n           running
# cf_whisper       running (healthy)
# cf_tts_worker    running
# cf_render_worker running
# cf_watchtower    running
```

```bash
# Verificar red interna
docker exec cf_n8n curl -s http://cf_tts_worker:8081/health | python3 -m json.tool
docker exec cf_n8n curl -s http://cf_render_worker:8080/health | python3 -m json.tool
docker exec cf_n8n curl -s http://cf_whisper:8000/health | python3 -m json.tool
```

---

## Comandos de mantenimiento

```bash
# Ver logs en tiempo real
docker compose logs -f tts_worker render_worker

# Reiniciar un worker
docker compose restart render_worker

# Ver errores en la base de datos
docker exec -it cf_postgres psql -U factory -d factory \
  -c "SELECT service, error_type, message, created_at FROM factory.errors ORDER BY created_at DESC LIMIT 10;"

# Ver pipeline status
docker exec -it cf_postgres psql -U factory -d factory \
  -c "SELECT topic, topic_status, script_status, audio_status, video_status, published_count FROM factory.v_pipeline_status LIMIT 10;"

# Ver costo acumulado
docker exec -it cf_postgres psql -U factory -d factory \
  -c "SELECT topic, total_cost_usd FROM factory.v_cost_per_video ORDER BY total_cost_usd DESC LIMIT 10;"

# Backup de la base de datos
docker exec cf_postgres pg_dump -U factory factory | gzip > /opt/factory/backups/factory_$(date +%Y%m%d).sql.gz

# Limpiar workspace temporal (si se llena el disco)
docker exec cf_render_worker find /tmp/workspace -mtime +1 -delete
docker exec cf_tts_worker find /tmp/workspace -mtime +1 -delete
```

---

## Escalado horizontal

Para procesar más videos simultáneamente, escalar los workers:

```bash
# Múltiples instancias del render worker
docker compose --env-file .env up -d --scale render_worker=3

# Ajustar MAX_CONCURRENT en el Orchestrator (actualmente 3)
# para que coincida con el número de workers
```

---

## Estimación de costos mensual (100 videos/mes)

| Componente | Costo |
|---|---|
| OpenRouter gpt-4o-mini (guion + SEO + escenas) | ~$0.02/video × 100 = **$2/mes** |
| Pexels API | **$0** (free tier) |
| Kokoro TTS | **$0** (local) |
| Whisper STT | **$0** (local) |
| FFmpeg render | **$0** (local) |
| Blotato publicación | **~$29/mes** (plan básico) |
| VPS 8GB RAM | **~$20-40/mes** |
| **TOTAL** | **~$51-71/mes** |

vs. arquitectura anterior estimada:
| JSON2Video | ~$30-50/video × 100 = $3,000-5,000/mes |
| ElevenLabs | ~$480/mes |
| OpenAI Whisper API | ~$15/mes |
| **TOTAL ANTERIOR** | **~$3,500-5,500/mes** |

**Ahorro: ~$3,450-5,430/mes**
