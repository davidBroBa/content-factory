"""
TTS Worker — Kokoro TTS + MinIO
Sustituye ElevenLabs con un modelo local de alta calidad.
Costo: $0
"""

import os
import uuid
import json
import logging
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime

import redis.asyncio as aioredis
import asyncpg
from minio import Minio
from minio.error import S3Error
import soundfile as sf
import numpy as np
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("tts_worker")

# ── Configuración ─────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "factory_minio")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minio_secure_2025")
MINIO_BUCKET = os.getenv("MINIO_BUCKET_AUDIO", "factory-audio")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://factory:pass@localhost:5432/factory")
WORKER_PORT = int(os.getenv("WORKER_PORT", "8081"))
WORKSPACE = Path("/tmp/workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)

# ── Kokoro TTS ────────────────────────────────────────────────
# Voces disponibles en español (Kokoro):
# es_es: Antonio (masculino), Camila (femenino), Diego (masculino)
# Referencia: https://huggingface.co/hexgrad/Kokoro-82M
KOKORO_VOICES = {
    "es_male_1": "af_heart",      # Male, natural, energético
    "es_female_1": "af_bella",    # Female, clara
    "es_male_narrator": "bm_george",  # Male, narrativo
}
DEFAULT_VOICE = "af_heart"

try:
    from kokoro import KPipeline
    _pipeline_cache = {}
    
    def get_pipeline(lang_code="e"):
        if lang_code not in _pipeline_cache:
            _pipeline_cache[lang_code] = KPipeline(lang_code=lang_code)
        return _pipeline_cache[lang_code]
    
    KOKORO_AVAILABLE = True
    log.info("✅ Kokoro TTS cargado correctamente")
except ImportError:
    KOKORO_AVAILABLE = False
    log.warning("⚠️ Kokoro no disponible — usando fallback espeak-ng")


# ── MinIO Client ──────────────────────────────────────────────
minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)


# ── FastAPI App ───────────────────────────────────────────────
app = FastAPI(title="TTS Worker", version="1.0.0")


class TTSRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE
    script_id: str
    audio_db_id: str | None = None
    lang: str = "e"  # 'e' = español en Kokoro


class TTSResponse(BaseModel):
    success: bool
    audio_id: str | None = None
    storage_url: str | None = None
    duration_s: float | None = None
    storage_path: str | None = None
    error: str | None = None


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "kokoro_available": KOKORO_AVAILABLE,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/synthesize", response_model=TTSResponse)
async def synthesize(req: TTSRequest, background_tasks: BackgroundTasks):
    """
    Sintetiza texto a voz y sube el audio a MinIO.
    Retorna la URL del audio en MinIO.
    """
    try:
        audio_id = str(uuid.uuid4())
        output_path = WORKSPACE / f"{audio_id}.wav"
        
        log.info(f"[TTS] Sintetizando audio_id={audio_id}, script_id={req.script_id}")
        log.info(f"[TTS] Texto ({len(req.text)} chars): {req.text[:100]}...")
        
        # ── Generar audio con Kokoro ──────────────────────────
        if KOKORO_AVAILABLE:
            duration_s = await synthesize_kokoro(req.text, req.voice, req.lang, output_path)
        else:
            duration_s = await synthesize_espeak(req.text, output_path)
        
        if not output_path.exists():
            raise RuntimeError("El archivo de audio no fue generado")
        
        file_size = output_path.stat().st_size
        log.info(f"[TTS] Audio generado: {duration_s:.2f}s, {file_size/1024:.1f}KB")
        
        # ── Subir a MinIO ─────────────────────────────────────
        storage_path = f"audio/{req.script_id}/{audio_id}.wav"
        minio_client.fput_object(
            MINIO_BUCKET,
            storage_path,
            str(output_path),
            content_type="audio/wav"
        )
        
        storage_url = f"http://{MINIO_ENDPOINT}/{MINIO_BUCKET}/{storage_path}"
        log.info(f"[TTS] Subido a MinIO: {storage_url}")
        
        # ── Actualizar DB ─────────────────────────────────────
        background_tasks.add_task(
            update_audio_db,
            req.audio_db_id or audio_id,
            storage_path,
            storage_url,
            duration_s,
            file_size
        )
        
        # Limpiar workspace
        output_path.unlink(missing_ok=True)
        
        return TTSResponse(
            success=True,
            audio_id=audio_id,
            storage_url=storage_url,
            storage_path=storage_path,
            duration_s=duration_s
        )
        
    except Exception as e:
        log.error(f"[TTS] Error en síntesis: {e}", exc_info=True)
        return TTSResponse(success=False, error=str(e))


async def synthesize_kokoro(text: str, voice: str, lang: str, output_path: Path) -> float:
    """
    Genera audio con Kokoro TTS.
    Kokoro es un modelo TTS de alta calidad similar a ElevenLabs.
    """
    import soundfile as sf
    import numpy as np
    
    loop = asyncio.get_event_loop()
    
    def _synth():
        pipeline = get_pipeline(lang)
        generator = pipeline(text, voice=voice, speed=0.95)
        
        audio_chunks = []
        sample_rate = 24000
        
        for _, _, audio in generator:
            if audio is not None:
                audio_chunks.append(audio)
        
        if not audio_chunks:
            raise RuntimeError("Kokoro no generó audio")
        
        audio_data = np.concatenate(audio_chunks)
        sf.write(str(output_path), audio_data, sample_rate, format="WAV")
        
        duration = len(audio_data) / sample_rate
        return duration
    
    duration = await loop.run_in_executor(None, _synth)
    return duration


async def synthesize_espeak(text: str, output_path: Path) -> float:
    """
    Fallback: genera audio con espeak-ng (calidad menor pero gratuito).
    Solo se usa si Kokoro no está disponible.
    """
    import subprocess
    
    cmd = [
        "espeak-ng",
        "-v", "es",
        "-s", "145",  # velocidad
        "-a", "80",   # amplitud
        "-w", str(output_path),
        text
    ]
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        raise RuntimeError(f"espeak-ng falló: {stderr.decode()}")
    
    # Calcular duración
    import soundfile as sf
    with sf.SoundFile(str(output_path)) as f:
        duration = len(f) / f.samplerate
    
    return duration


async def update_audio_db(audio_id: str, storage_path: str, storage_url: str, 
                          duration_s: float, file_size: int):
    """Actualiza el registro de audio en PostgreSQL."""
    try:
        conn = await asyncpg.connect(POSTGRES_DSN)
        await conn.execute("""
            UPDATE factory.audios
            SET storage_path = $1,
                storage_url = $2,
                duration_s = $3,
                file_size_bytes = $4,
                status = 'completed',
                cost_usd = 0,
                updated_at = NOW()
            WHERE id = $5::uuid
        """, storage_path, storage_url, duration_s, file_size, audio_id)
        await conn.close()
        log.info(f"[TTS] DB actualizada para audio_id={audio_id}")
    except Exception as e:
        log.error(f"[TTS] Error actualizando DB: {e}")


# ── Worker de cola Redis ──────────────────────────────────────
async def process_queue():
    """
    Procesa jobs de TTS desde la cola Redis.
    Permite que n8n encole trabajos sin esperar la respuesta.
    """
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("[Queue] TTS Worker escuchando cola 'tts_jobs'...")
    
    while True:
        try:
            # BLPOP con timeout de 5s (no busy-wait)
            result = await redis.blpop("tts_jobs", timeout=5)
            if not result:
                continue
            
            _, job_json = result
            job = json.loads(job_json)
            
            log.info(f"[Queue] Procesando job: {job.get('audio_db_id', 'unknown')}")
            
            req = TTSRequest(**job)
            
            # Procesar de forma sincrónica (en la cola, un job a la vez)
            await synthesize(req, background_tasks=BackgroundTasks())
            
        except Exception as e:
            log.error(f"[Queue] Error procesando job: {e}", exc_info=True)
            await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    asyncio.create_task(process_queue())
    log.info("✅ TTS Worker iniciado")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
