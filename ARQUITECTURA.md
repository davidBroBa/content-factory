# 🏭 Content Factory — Auditoría Completa y Rediseño de Arquitectura

## ⚠️ PROBLEMAS CRÍTICOS DETECTADOS EN EL WORKFLOW ACTUAL

### 🔴 CRÍTICO: API Keys expuestas en texto plano dentro del JSON
- `AIzaSyASNoIWHBTiqn_tVswcf9hK4YvuMb5Yl7Q` (YouTube Data API) — hardcodeada en nodos HTTP
- `gcRoofIYQPgnr3GwekgohXkC3nUo8ejXwEPYKGhd15fuwoMhzNfgXGRc` (Pexels) — hardcodeada
- `PvZpBP59tL8WxD07DIW8EXYgtEHXpGQZbeIx1xGc` (JSON2Video) — hardcodeada

**Acción inmediata:** Rotar todas estas claves. Están comprometidas si el JSON fue compartido.

---

### 🔴 CRÍTICO: Whisper en Docker pero llamando a OpenAI Whisper API
Tienes `faster-whisper` corriendo en Docker (gratis, local) pero el nodo `"Whisper API (Timestamps)"` llama a `https://api.openai.com/v1/audio/transcriptions` (de pago). Estás pagando por algo que ya tienes gratis instalado.

---

### 🔴 CRÍTICO: tmpfiles.org para almacenamiento de audio
El audio TTS se sube a `tmpfiles.org` (servicio público gratuito de terceros con TTL de 60 minutos) para luego pasárselo a JSON2Video. Si el render tarda más de 60 min, el video falla silenciosamente. Es un punto de fallo no manejado.

---

### 🟠 GRAVE: JSON2Video — costo evitable + dependencia externa
- Costo: ~$0.10–0.50 por video
- Es una caja negra: no controlas calidad, formato ni lógica de render
- Si falla su API, todo el pipeline se detiene
- La lógica de construcción del payload ya está en n8n; solo falta el render local
- **Solución: FFmpeg local** → costo $0, control total, sin límites de velocidad

---

### 🟠 GRAVE: ElevenLabs TTS — costo evitable
- Costo: ~$0.18–0.30 por 1000 caracteres
- Para 1 short de 40s (~150 palabras = ~900 chars): ~$0.16 por video
- A 100 videos/día = $16/día = $480/mes solo en TTS
- **Alternativa local:** Kokoro TTS (calidad casi igual a ElevenLabs), Coqui, o Piper — costo $0

---

### 🟠 GRAVE: Modelo GPT-3.5-turbo para el Trend Agent
El `Trend Agent` usa `gpt-3.5-turbo` (más barato pero con peor razonamiento). El Script Agent usa `gpt-4.1` (más caro). Hay inconsistencia. Para análisis de tendencias se puede usar `gpt-4o-mini` que es más barato que `gpt-4.1` y mejor que `gpt-3.5-turbo`.

---

### 🟡 MODERADO: Workflow monolítico — todo en un solo flujo
El workflow actual tiene 20+ nodos en una cadena lineal. Si cualquier nodo falla, todo el proceso muere desde el principio. No hay:
- Checkpoints de recuperación
- Reintentos granulares por etapa
- Capacidad de reanudar desde un punto intermedio
- Separación entre producción de contenido y publicación

---

### 🟡 MODERADO: `$getWorkflowStaticData('global')` para pasar datos entre nodos
Esto es un antipatrón en n8n. `staticData` es estado global persistente que puede corromper datos entre ejecuciones concurrentes. Se deben usar variables de contexto o pasar datos explícitamente entre nodos.

---

### 🟡 MODERADO: Nodos Code redundantes y acoplados
`"Estructura informacion"` → `"Otorga Puntuacion"` → `"Elimina videos bajos"` son tres nodos Code que podrían ser uno solo. El código es simple y no justifica tres pasos separados.

---

### 🟡 MODERADO: Sin base de datos real para estado de videos
No hay persistencia del estado. Si n8n se reinicia mientras procesa, no hay forma de saber qué videos están en progreso, completados o fallidos.

---

### 🟡 MODERADO: Polling activo con Wait nodes para JSON2Video
El patrón `Wait → Check Status → IF → Wait` es un busy-wait que bloquea el execution thread de n8n. Con n8n Community Edition (que probablemente tienes), esto consume un slot de ejecución durante todo el tiempo de render.

---

### 🟡 MODERADO: Filter node mal diseñado
El nodo `"Filter"` filtra por `status` vacío, pero no está claro qué `status` se está evaluando ni por qué. Parece un artefacto de debugging que quedó en producción.

---

### 🔵 MENOR: Sin manejo de errores en la mayoría de nodos
Solo `"Elimina videos bajos"` tiene `onError: continueErrorOutput`. El resto de nodos fallarán silenciosamente o detendrán el workflow sin logging.

---

### 🔵 MENOR: Sin deduplicación de tópicos
Si el cron ejecuta dos veces seguidas, puede generar videos sobre el mismo tema sin verificación.

---

## 🏗️ ARQUITECTURA REDISEÑADA

### Principios de diseño
1. **Separación de responsabilidades**: cada servicio hace UNA cosa
2. **Tolerancia a fallos**: cada etapa es recuperable independientemente
3. **Costo mínimo**: prioridad a herramientas locales/open source
4. **Escalabilidad horizontal**: workers stateless que procesan colas
5. **Observabilidad**: logs estructurados, métricas, alertas

---

### Stack tecnológico final

| Componente | Herramienta | Justificación |
|---|---|---|
| Orquestador | n8n | Ya instalado, buena integración |
| Base de datos | PostgreSQL | Ya instalado |
| Cola de tareas | Redis + BullMQ | Lightweight, rápido, ideal para jobs |
| TTS local | Kokoro TTS (Python) | Calidad casi ElevenLabs, gratis |
| STT/Subtítulos | Faster-Whisper (ya tienes) | Ya instalado, redirigir hacia él |
| Render video | FFmpeg (Python worker) | Gratis, control total, sin límites |
| Almacenamiento | MinIO (S3 local) | Sustituye tmpfiles.org |
| LLM | OpenRouter (gpt-4o-mini) | 50% más barato que OpenAI directo |
| Assets | Pexels API | Free tier, suficiente |
| Publicación | APIs oficiales + Blotato | Automatización multi-plataforma |
| Monitoreo | n8n + PostgreSQL logs | Sin herramientas extra |

---

### Diagrama de flujo de la arquitectura

```
CRON (n8n)
    │
    ▼
[SW1: Researcher] ──► PostgreSQL (topics table)
    │
    ▼
[SW2: Scriptwriter] ──► PostgreSQL (scripts table)
    │
    ▼
[SW3: TTS Worker] ──► Kokoro Python Worker ──► MinIO (audio)
    │
    ▼
[SW4: Transcriber] ──► Faster-Whisper local ──► PostgreSQL (subtitles)
    │
    ▼
[SW5: Assets Agent] ──► Pexels API ──► PostgreSQL (assets table)
    │
    ▼
[SW6: Render Worker] ──► FFmpeg Python Worker ──► MinIO (video)
    │
    ▼
[SW7: SEO Agent] ──► PostgreSQL (seo table)
    │
    ▼
[SW8: Publisher] ──► YouTube / TikTok / IG / FB APIs
    │
    ▼
[SW9: Analytics] ──► PostgreSQL (metrics table)
```

---

### Decisión: ¿Qué queda en n8n y qué va a Python?

**Queda en n8n (orquestación):**
- Triggers (cron, webhooks)
- Llamadas a LLM (OpenRouter)
- Queries a PostgreSQL
- Encolado de jobs via Redis
- Publicación en redes sociales
- Notificaciones

**Se mueve a Python workers (procesamiento):**
- TTS (Kokoro) — requiere ML
- FFmpeg render — requiere construcción compleja de comandos
- Transcripción Whisper — ya lo tienes pero mal conectado
- Descarga de assets

**Razón:** n8n no es un motor de procesamiento. Es un orquestador. Los workers Python son stateless, escalables y rápidos para procesamiento intensivo.
