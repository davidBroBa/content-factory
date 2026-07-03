# 📊 Resumen de Decisiones de Arquitectura y Costos

## Decisiones técnicas clave

### 1. OpenRouter en lugar de OpenAI directo
**Por qué:** OpenRouter agrega múltiples proveedores con precios competitivos.
`gpt-4o-mini` vía OpenRouter es ~10-15% más barato que OpenAI directo.
Además, si OpenAI tiene downtime, puedes cambiar a Anthropic o Gemini
modificando solo el campo `model` en el payload.

**Costo estimado con gpt-4o-mini:**
- Input: $0.00015 / 1K tokens
- Output: $0.0006 / 1K tokens
- Por video (~800 tokens total entre guion + SEO + escenas): **~$0.0005/video**

---

### 2. Kokoro TTS en lugar de ElevenLabs
**Por qué:** ElevenLabs cobra ~$0.18-0.30 por 1,000 caracteres.
Un guion de 150 palabras ≈ 900 caracteres ≈ $0.16/video.
A 100 videos/mes = $16/mes solo en TTS.

Kokoro es un modelo de síntesis neuronal de alta calidad (comparable a
ElevenLabs en pruebas de MOS) publicado bajo licencia Apache 2.0.
Costo local: $0.

**Voces recomendadas en español:**
- `af_heart` — femenina, natural, cálida (buena para narración)
- `bm_george` — masculina, autoritaria (buena para true crime)
- `af_bella` — femenina, más seria

---

### 3. FFmpeg en lugar de JSON2Video
**Por qué:** JSON2Video cobra por render (~$0.10-0.50/video).
Es una caja negra: no puedes personalizar la lógica de render.
El latency de render externo es variable e impredecible.

FFmpeg local:
- Costo: $0
- Latencia: 30-90 segundos en CPU (vs 60-180s en JSON2Video)
- Control total de calidad, formato, efectos
- Ken Burns implementado con `zoompan` filter
- Subtítulos burned-in con `subtitles` filter
- Salida H.264 1080x1920 @30fps con CRF 22 (alta calidad)

---

### 4. Whisper local en lugar de OpenAI Whisper API
**Lo tenías instalado pero NO lo estabas usando correctamente.**
El workflow llamaba a `api.openai.com` para transcripción en lugar de
tu servicio local `http://whisper:8000`.

Costo OpenAI Whisper API: $0.006/minuto.
A 40s/video × 100 videos = 66 minutos = **$0.40/mes** (pequeño pero evitable).
Más importante: elimina dependencia externa y latencia de red.

---

### 5. MinIO en lugar de tmpfiles.org
**Problema crítico del workflow anterior:**
El audio TTS se subía a tmpfiles.org (TTL de 60 minutos).
Si el render de JSON2Video tardaba más de 60 minutos (posible en cola),
el audio expiraba y el video fallaba **silenciosamente**.

MinIO local:
- Almacenamiento persistente sin TTL
- API S3-compatible (fácil migración a AWS S3 si escala)
- Sin costo adicional (solo disco)
- Acceso por red interna Docker (sin latencia de internet)

---

### 6. Redis para cola de trabajos
**Por qué no usar Wait nodes de n8n:**
El patrón `Wait → Check Status → IF → Wait` en n8n bloquea un execution
slot durante todo el tiempo de render. En n8n Community Edition, los
slots de ejecución son limitados. Con 3 videos en proceso simultáneo,
n8n puede quedar saturado.

Redis + BullMQ pattern:
- n8n encola el job (`LPUSH render_jobs <payload>`) → responde de inmediato
- El worker Python consume la cola con `BLPOP` (blocking, sin busy-wait)
- n8n puede continuar procesando otros items mientras el render ocurre
- Los jobs sobreviven reinicios (Redis persiste con `save 60 1`)

---

### 7. PostgreSQL con schema separado
**Por qué `factory` schema y no mezclar con n8n:**
n8n usa la misma instancia PostgreSQL para su metadata interna.
Crear un schema `factory` separado permite:
- Backups independientes
- Permisos granulares (el app user solo accede a `factory.*`)
- Sin riesgo de corromper tablas internas de n8n
- Fácil migración futura a otra instancia

---

### 8. Workflows separados (subworkflows)
**Problema del monolito anterior:**
Un solo workflow de 20+ nodos significa que si falla el paso 18,
tienes que reiniciar desde el paso 1 (YouTube Search).

Separación actual:
- `00_error_handler` → centraliza todos los errores
- `01_main_orchestrator` → control de flujo y concurrencia
- `02_researcher` → independiente, puede re-ejecutarse
- `03_scriptwriter` → independiente, puede re-ejecutarse
- `04_producer` → TTS + Assets + Render (recuperable por etapa)
- `05_publisher` → independiente del render
- `06_analytics` → completamente independiente

Si falla el render, no necesitas repetir el research ni el guion.

---

## Tabla de costos comparativa

### Arquitectura ANTERIOR (estimada)

| Componente | Costo/video | 100 videos/mes |
|---|---|---|
| JSON2Video | $0.30 | $30 |
| ElevenLabs TTS | $0.16 | $16 |
| OpenAI Whisper API | $0.004 | $0.40 |
| OpenAI GPT-4.1 (guion) | $0.08 | $8 |
| tmpfiles.org | $0 | $0 |
| **Total APIs** | **$0.544** | **$54.40/mes** |
| + VPS/servidor | — | $20-40 |
| **TOTAL** | **$0.544** | **~$74-94/mes** |

### Arquitectura NUEVA

| Componente | Costo/video | 100 videos/mes |
|---|---|---|
| OpenRouter gpt-4o-mini | $0.0005 | $0.05 |
| Kokoro TTS (local) | $0 | $0 |
| Whisper local | $0 | $0 |
| FFmpeg render (local) | $0 | $0 |
| MinIO storage | $0 | $0 |
| Pexels API | $0 | $0 |
| Blotato publicación | — | $29 |
| **Total APIs** | **$0.0005** | **$0.05/mes** |
| + VPS 8GB | — | $20-40 |
| **TOTAL** | **$0.0005** | **~$49-69/mes** |

**Ahorro neto: ~$25-50/mes a 100 videos → escala a ~$5,000/mes de ahorro a 10,000 videos.**

---

## Bottlenecks identificados y soluciones

### Bottleneck 1: Render en CPU
- **Problema:** FFmpeg con Ken Burns en CPU tarda 60-120 segundos por video
- **Solución corto plazo:** `MAX_RENDER_WORKERS=2` (2 renders simultáneos)
- **Solución largo plazo:** GPU render con `h264_nvenc` (NVIDIA) o `h264_videotoolbox` (Mac)
  ```
  # Para GPU NVIDIA, cambiar en worker.py:
  "-c:v", "h264_nvenc", "-preset", "fast", "-cq", "22"
  ```

### Bottleneck 2: Pexels API rate limits
- **Problema:** Pexels free tier = 200 requests/hora
- **Solución:** Cache de assets descargados en MinIO. Si el B-roll ya fue
  descargado para una búsqueda previa, reutilizarlo.
- **Implementación:** Tabla `assets` ya tiene `source` + `source_id` para deduplicación.

### Bottleneck 3: Kokoro carga del modelo
- **Problema:** Kokoro carga el modelo en RAM al iniciar (~1-2 GB)
- **Solución:** El worker mantiene el modelo en memoria entre requests.
  Primer request es lento (~30s), los siguientes son rápidos (<5s).

---

## Siguientes pasos recomendados

1. **Implementar cache de B-roll** — Antes de llamar a Pexels, buscar en la tabla `assets`
   si ya hay un video descargado para esa búsqueda. Ahorra API calls y tiempo de descarga.

2. **Thumbnail automático** — Agregar un paso en el render que extraiga el frame
   más impactante del video como thumbnail:
   ```bash
   ffmpeg -i output.mp4 -vf "select=gt(scene\,0.4)" -frames:v 1 thumbnail.jpg
   ```

3. **A/B testing de guiones** — Generar 2 variantes de guion y publicar la que
   obtenga más engagement en las primeras 2 horas.

4. **Música de fondo** — Agregar un bucket de música libre de derechos en MinIO
   y seleccionar aleatoriamente según el mood del guion. Freesound.org tiene
   tracks con licencia CC0.

5. **Dashboard de métricas** — Crear una vista Grafana conectada a PostgreSQL
   para visualizar el rendimiento del pipeline en tiempo real.
