-- ============================================================
-- CONTENT FACTORY — PostgreSQL Schema Completo
-- Versión 1.0
-- ============================================================

-- Extensiones
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- Para búsqueda fuzzy de tópicos duplicados

-- ============================================================
-- SCHEMA PRINCIPAL
-- ============================================================
CREATE SCHEMA IF NOT EXISTS factory;
SET search_path TO factory, public;

-- ============================================================
-- ENUM TYPES
-- ============================================================
CREATE TYPE job_status AS ENUM (
  'pending', 'processing', 'completed', 'failed', 'skipped'
);

CREATE TYPE platform AS ENUM (
  'youtube', 'tiktok', 'instagram', 'facebook'
);

CREATE TYPE asset_type AS ENUM (
  'video_broll', 'image', 'music', 'sfx', 'overlay'
);

CREATE TYPE voice_provider AS ENUM (
  'kokoro', 'elevenlabs', 'openai', 'local'
);

-- ============================================================
-- TABLA: topics
-- Temas/ideas de videos a producir
-- ============================================================
CREATE TABLE topics (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  topic         TEXT NOT NULL,
  niche         TEXT NOT NULL DEFAULT 'true_crime',
  summary       TEXT,
  trend_score   NUMERIC(10,2) DEFAULT 0,
  source        TEXT,          -- 'youtube_trending', 'manual', 'rss', etc.
  source_id     TEXT,          -- ID del video/artículo de origen
  status        job_status NOT NULL DEFAULT 'pending',
  priority      INTEGER DEFAULT 5,
  produced      BOOLEAN DEFAULT FALSE,
  skip_reason   TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW(),
  
  -- Evitar duplicados por topic fuzzy
  CONSTRAINT unique_topic_text UNIQUE (topic)
);

CREATE INDEX idx_topics_status ON topics(status);
CREATE INDEX idx_topics_priority ON topics(priority DESC, trend_score DESC);
CREATE INDEX idx_topics_produced ON topics(produced);
CREATE INDEX idx_topics_topic_trgm ON topics USING gin(topic gin_trgm_ops);

-- ============================================================
-- TABLA: scripts
-- Guiones generados por IA
-- ============================================================
CREATE TABLE scripts (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  topic_id      UUID NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  version       INTEGER DEFAULT 1,
  
  -- Contenido
  full_script   TEXT NOT NULL,
  hook          TEXT,
  word_count    INTEGER,
  estimated_duration_s NUMERIC(6,2),
  
  -- SEO
  title         TEXT,
  description   TEXT,
  hashtags      TEXT[],
  keywords      TEXT[],
  
  -- LLM metadata
  model_used    TEXT DEFAULT 'gpt-4o-mini',
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost_usd      NUMERIC(8,6),
  
  status        job_status NOT NULL DEFAULT 'completed',
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_scripts_topic_id ON scripts(topic_id);
CREATE INDEX idx_scripts_status ON scripts(status);

-- ============================================================
-- TABLA: scenes
-- Escenas del video (timeline de B-roll)
-- ============================================================
CREATE TABLE scenes (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  script_id     UUID NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  scene_number  INTEGER NOT NULL,
  
  -- Timing
  start_s       NUMERIC(8,3) NOT NULL,
  end_s         NUMERIC(8,3) NOT NULL,
  duration_s    NUMERIC(8,3) GENERATED ALWAYS AS (end_s - start_s) STORED,
  
  -- Contenido
  theme         TEXT,
  search_query  TEXT NOT NULL,   -- query para buscar B-roll
  keywords      TEXT[],
  
  -- Asset seleccionado
  asset_id      UUID,            -- FK a assets, se llena cuando se selecciona
  
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  
  CONSTRAINT scenes_scene_order UNIQUE (script_id, scene_number),
  CONSTRAINT scenes_valid_timing CHECK (end_s > start_s)
);

CREATE INDEX idx_scenes_script_id ON scenes(script_id);

-- ============================================================
-- TABLA: assets
-- Assets multimedia descargados (B-roll, imágenes, música)
-- ============================================================
CREATE TABLE assets (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  
  -- Identificación
  asset_type    asset_type NOT NULL,
  source        TEXT NOT NULL,   -- 'pexels', 'pixabay', 'freepik', 'local'
  source_id     TEXT,            -- ID en la fuente original
  source_url    TEXT,            -- URL original
  
  -- Almacenamiento local (MinIO)
  storage_path  TEXT,            -- path en MinIO: assets/broll/uuid.mp4
  storage_url   TEXT,            -- URL pública de MinIO
  file_size_bytes BIGINT,
  
  -- Metadata técnica
  width         INTEGER,
  height        INTEGER,
  duration_s    NUMERIC(8,3),
  fps           NUMERIC(6,2),
  codec         TEXT,
  format        TEXT,
  
  -- Metadata descriptiva
  description   TEXT,
  tags          TEXT[],
  
  -- Licencia
  license       TEXT DEFAULT 'free',
  attribution   TEXT,
  
  status        job_status NOT NULL DEFAULT 'completed',
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_assets_type ON assets(asset_type);
CREATE INDEX idx_assets_source ON assets(source, source_id);
CREATE INDEX idx_assets_storage_path ON assets(storage_path);

-- FK de scenes a assets (after asset table creation)
ALTER TABLE scenes ADD CONSTRAINT fk_scenes_asset 
  FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE SET NULL;

-- ============================================================
-- TABLA: audios
-- Archivos de audio TTS generados
-- ============================================================
CREATE TABLE audios (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  script_id     UUID NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  
  -- Proveedor TTS
  provider      voice_provider NOT NULL DEFAULT 'kokoro',
  voice_id      TEXT NOT NULL DEFAULT 'af_heart', -- Kokoro voice
  
  -- Almacenamiento
  storage_path  TEXT,            -- path en MinIO: audio/uuid.wav
  storage_url   TEXT,
  file_size_bytes BIGINT,
  
  -- Metadata de audio
  duration_s    NUMERIC(8,3),
  sample_rate   INTEGER DEFAULT 24000,
  channels      INTEGER DEFAULT 1,
  
  -- Subtítulos (output de Whisper)
  transcript    TEXT,
  segments_json JSONB,           -- [{start, end, text, words:[{start,end,word}]}]
  
  -- Costo
  cost_usd      NUMERIC(8,6) DEFAULT 0,
  
  status        job_status NOT NULL DEFAULT 'pending',
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audios_script_id ON audios(script_id);
CREATE INDEX idx_audios_status ON audios(status);

-- ============================================================
-- TABLA: videos
-- Videos renderizados
-- ============================================================
CREATE TABLE videos (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  topic_id      UUID NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  script_id     UUID NOT NULL REFERENCES scripts(id),
  audio_id      UUID REFERENCES audios(id),
  
  -- Almacenamiento
  storage_path  TEXT,
  storage_url   TEXT,
  thumbnail_path TEXT,
  thumbnail_url  TEXT,
  file_size_bytes BIGINT,
  
  -- Metadata técnica
  width         INTEGER DEFAULT 1080,
  height        INTEGER DEFAULT 1920,
  duration_s    NUMERIC(8,3),
  fps           INTEGER DEFAULT 30,
  codec         TEXT DEFAULT 'h264',
  bitrate_kbps  INTEGER,
  
  -- Render info
  render_started_at  TIMESTAMPTZ,
  render_finished_at TIMESTAMPTZ,
  render_duration_s  NUMERIC(10,2),
  ffmpeg_command      TEXT,        -- comando FFmpeg usado (debug)
  
  -- Estado
  status        job_status NOT NULL DEFAULT 'pending',
  error_message TEXT,
  
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_videos_topic_id ON videos(topic_id);
CREATE INDEX idx_videos_status ON videos(status);
CREATE INDEX idx_videos_created_at ON videos(created_at DESC);

-- ============================================================
-- TABLA: publications
-- Publicaciones en plataformas sociales
-- ============================================================
CREATE TABLE publications (
  id              UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  video_id        UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  
  platform        platform NOT NULL,
  platform_post_id TEXT,          -- ID devuelto por la plataforma
  platform_url    TEXT,           -- URL del post publicado
  
  -- Contenido publicado
  title_used      TEXT,
  description_used TEXT,
  hashtags_used   TEXT[],
  
  -- Scheduling
  scheduled_for   TIMESTAMPTZ,
  published_at    TIMESTAMPTZ,
  
  status          job_status NOT NULL DEFAULT 'pending',
  error_message   TEXT,
  retry_count     INTEGER DEFAULT 0,
  
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  
  CONSTRAINT unique_video_platform UNIQUE (video_id, platform)
);

CREATE INDEX idx_publications_video_id ON publications(video_id);
CREATE INDEX idx_publications_status ON publications(status);
CREATE INDEX idx_publications_platform ON publications(platform);
CREATE INDEX idx_publications_scheduled ON publications(scheduled_for) WHERE status = 'pending';

-- ============================================================
-- TABLA: metrics
-- Métricas de rendimiento de publicaciones
-- ============================================================
CREATE TABLE metrics (
  id              UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  publication_id  UUID NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  
  -- Snapshot timing
  snapshot_at     TIMESTAMPTZ DEFAULT NOW(),
  hours_since_pub INTEGER,
  
  -- Métricas base
  views           BIGINT DEFAULT 0,
  likes           BIGINT DEFAULT 0,
  comments        BIGINT DEFAULT 0,
  shares          BIGINT DEFAULT 0,
  saves           BIGINT DEFAULT 0,
  
  -- Métricas derivadas
  engagement_rate NUMERIC(8,4),   -- (likes+comments+shares)/views * 100
  watch_time_avg_s NUMERIC(8,2),  -- avg watch time in seconds
  retention_rate  NUMERIC(6,4),   -- % de usuarios que ven completo
  
  -- Métricas de crecimiento
  new_followers   INTEGER DEFAULT 0,
  click_through   INTEGER DEFAULT 0,
  
  -- Revenue (si aplica)
  estimated_revenue_usd NUMERIC(10,4) DEFAULT 0,
  
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_metrics_publication_id ON metrics(publication_id);
CREATE INDEX idx_metrics_snapshot_at ON metrics(snapshot_at DESC);

-- ============================================================
-- TABLA: job_queue
-- Cola de trabajos para los workers Python
-- ============================================================
CREATE TABLE job_queue (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  job_type      TEXT NOT NULL,   -- 'tts', 'transcribe', 'render', 'download_asset'
  payload       JSONB NOT NULL,
  
  status        job_status NOT NULL DEFAULT 'pending',
  priority      INTEGER DEFAULT 5,
  
  -- Tracking
  worker_id     TEXT,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  attempts      INTEGER DEFAULT 0,
  max_attempts  INTEGER DEFAULT 3,
  
  -- Error
  error_message TEXT,
  error_trace   TEXT,
  
  -- Scheduling
  run_after     TIMESTAMPTZ DEFAULT NOW(),
  
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_job_queue_status_priority ON job_queue(status, priority DESC, run_after) 
  WHERE status IN ('pending', 'failed');
CREATE INDEX idx_job_queue_type ON job_queue(job_type);
CREATE INDEX idx_job_queue_worker ON job_queue(worker_id) WHERE status = 'processing';

-- ============================================================
-- TABLA: logs
-- Log estructurado del sistema
-- ============================================================
-- ============================================================
-- TABLA: logs (CORREGIDA)
-- ============================================================
CREATE TABLE logs (
  id           BIGSERIAL, -- Quitamos el PRIMARY KEY de aquí
  level        TEXT NOT NULL DEFAULT 'info',
  service      TEXT NOT NULL,
  event        TEXT NOT NULL,
  message      TEXT,
  
  -- Referencias opcionales
  topic_id     UUID,
  script_id    UUID,
  video_id     UUID,
  job_id       UUID,
  
  -- Datos adicionales
  metadata     JSONB,
  
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  
  -- Definimos la llave primaria compuesta
  PRIMARY KEY (id, created_at) 
) PARTITION BY RANGE (created_at);

-- Particiones por mes (crear manualmente o con pg_partman)
CREATE TABLE logs_2025_01 PARTITION OF logs
  FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
CREATE TABLE logs_2025_06 PARTITION OF logs
  FOR VALUES FROM ('2025-06-01') TO ('2025-07-01');
CREATE TABLE logs_2025_07 PARTITION OF logs
  FOR VALUES FROM ('2025-07-01') TO ('2025-08-01');
CREATE TABLE logs_2025_08 PARTITION OF logs
  FOR VALUES FROM ('2025-08-01') TO ('2025-09-01');
CREATE TABLE logs_2025_09 PARTITION OF logs
  FOR VALUES FROM ('2025-09-01') TO ('2025-10-01');
CREATE TABLE logs_2025_10 PARTITION OF logs
  FOR VALUES FROM ('2025-10-01') TO ('2025-11-01');
CREATE TABLE logs_2025_11 PARTITION OF logs
  FOR VALUES FROM ('2025-11-01') TO ('2025-12-01');
CREATE TABLE logs_2025_12 PARTITION OF logs
  FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');
CREATE TABLE logs_2026_01 PARTITION OF logs
  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE logs_2026_default PARTITION OF logs DEFAULT;

CREATE INDEX idx_logs_created_at ON logs(created_at DESC);
CREATE INDEX idx_logs_level ON logs(level) WHERE level IN ('error', 'critical');
CREATE INDEX idx_logs_service ON logs(service);
CREATE INDEX idx_logs_video_id ON logs(video_id) WHERE video_id IS NOT NULL;

-- ============================================================
-- TABLA: errors
-- Errores detallados para debugging
-- ============================================================
CREATE TABLE errors (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  service       TEXT NOT NULL,
  error_type    TEXT NOT NULL,
  message       TEXT NOT NULL,
  stack_trace   TEXT,
  
  -- Contexto
  topic_id      UUID,
  video_id      UUID,
  job_id        UUID,
  
  -- Datos del error
  input_data    JSONB,
  http_status   INTEGER,
  http_response TEXT,
  
  -- Resolución
  resolved      BOOLEAN DEFAULT FALSE,
  resolved_at   TIMESTAMPTZ,
  resolution_note TEXT,
  
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_errors_resolved ON errors(resolved, created_at DESC);
CREATE INDEX idx_errors_service ON errors(service);
CREATE INDEX idx_errors_video_id ON errors(video_id) WHERE video_id IS NOT NULL;

-- ============================================================
-- TABLA: seo_outputs
-- Outputs SEO generados por el agente
-- ============================================================
CREATE TABLE seo_outputs (
  id            UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
  script_id     UUID NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  
  -- Por plataforma
  platform      platform NOT NULL,
  
  title         TEXT NOT NULL,
  description   TEXT,
  hashtags      TEXT[],
  tags          TEXT[],
  
  -- Thumbnail text
  thumbnail_text TEXT,
  
  model_used    TEXT DEFAULT 'gpt-4o-mini',
  
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  
  CONSTRAINT unique_script_platform UNIQUE (script_id, platform)
);

-- ============================================================
-- VISTAS ÚTILES
-- ============================================================

-- Vista: Pipeline Status — estado completo de cada video
CREATE VIEW v_pipeline_status AS
SELECT
  t.id AS topic_id,
  t.topic,
  t.trend_score,
  t.status AS topic_status,
  s.id AS script_id,
  s.status AS script_status,
  s.word_count,
  a.id AS audio_id,
  a.status AS audio_status,
  a.duration_s AS audio_duration_s,
  a.provider AS tts_provider,
  v.id AS video_id,
  v.status AS video_status,
  v.storage_url AS video_url,
  v.render_duration_s,
  COUNT(p.id) AS publications_count,
  COUNT(p.id) FILTER (WHERE p.status = 'completed') AS published_count,
  t.created_at
FROM topics t
LEFT JOIN scripts s ON s.topic_id = t.id
LEFT JOIN audios a ON a.script_id = s.id
LEFT JOIN videos v ON v.script_id = s.id
LEFT JOIN publications p ON p.video_id = v.id
GROUP BY t.id, t.topic, t.trend_score, t.status,
         s.id, s.status, s.word_count,
         a.id, a.status, a.duration_s, a.provider,
         v.id, v.status, v.storage_url, v.render_duration_s,
         t.created_at
ORDER BY t.created_at DESC;

-- Vista: Métricas diarias por plataforma
CREATE VIEW v_daily_metrics AS
SELECT
  DATE(m.snapshot_at) AS date,
  p.platform,
  COUNT(DISTINCT p.video_id) AS videos_published,
  SUM(m.views) AS total_views,
  SUM(m.likes) AS total_likes,
  SUM(m.comments) AS total_comments,
  AVG(m.engagement_rate) AS avg_engagement_rate,
  SUM(m.estimated_revenue_usd) AS estimated_revenue_usd
FROM metrics m
JOIN publications p ON p.id = m.publication_id
GROUP BY DATE(m.snapshot_at), p.platform
ORDER BY date DESC, total_views DESC;

-- Vista: Costo por video
CREATE VIEW v_cost_per_video AS
SELECT
  t.id AS topic_id,
  t.topic,
  COALESCE(s.cost_usd, 0) AS script_cost_usd,
  COALESCE(a.cost_usd, 0) AS tts_cost_usd,
  COALESCE(s.cost_usd, 0) + COALESCE(a.cost_usd, 0) AS total_cost_usd
FROM topics t
LEFT JOIN scripts s ON s.topic_id = t.id
LEFT JOIN audios a ON a.script_id = s.id
ORDER BY t.created_at DESC;

-- ============================================================
-- FUNCIONES Y TRIGGERS
-- ============================================================

-- Función: actualizar updated_at automáticamente
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Aplicar trigger a todas las tablas con updated_at
CREATE TRIGGER trigger_topics_updated_at
  BEFORE UPDATE ON topics
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_scripts_updated_at
  BEFORE UPDATE ON scripts
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_audios_updated_at
  BEFORE UPDATE ON audios
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_videos_updated_at
  BEFORE UPDATE ON videos
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_publications_updated_at
  BEFORE UPDATE ON publications
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_job_queue_updated_at
  BEFORE UPDATE ON job_queue
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Función: marcar tópico como producido cuando el video esté completo
CREATE OR REPLACE FUNCTION mark_topic_produced()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
    UPDATE topics SET produced = TRUE, updated_at = NOW()
    WHERE id = (
      SELECT s.topic_id FROM scripts s WHERE s.id = NEW.script_id
    );
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_video_mark_produced
  AFTER UPDATE ON videos
  FOR EACH ROW EXECUTE FUNCTION mark_topic_produced();

-- ============================================================
-- DATOS INICIALES
-- ============================================================

-- Usuario de aplicación con permisos mínimos
-- (ejecutar como superuser una sola vez)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_app') THEN
    CREATE ROLE factory_app WITH LOGIN PASSWORD 'factory_secure_pass_2025';
  END IF;
END$$;

GRANT USAGE ON SCHEMA factory TO factory_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA factory TO factory_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA factory TO factory_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA factory GRANT SELECT, INSERT, UPDATE ON TABLES TO factory_app;

-- ============================================================
-- ÍNDICES ADICIONALES PARA PERFORMANCE
-- ============================================================

-- Para el worker que busca jobs pendientes
CREATE INDEX idx_job_queue_claim ON job_queue(job_type, priority DESC, created_at)
  WHERE status = 'pending' AND run_after <= NOW();

-- Para dashboard de pipeline
CREATE INDEX idx_videos_pipeline ON videos(status, created_at DESC)
  INCLUDE (topic_id, script_id, storage_url);
