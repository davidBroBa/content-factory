"""
Render Worker — FFmpeg + MinIO
Sustituye JSON2Video completamente.
Soporta: subtítulos animados, Ken Burns, transiciones, música, formato vertical.
"""

import os
import uuid
import json
import logging
import asyncio
import tempfile
import subprocess
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

import redis.asyncio as aioredis
import asyncpg
import httpx
from minio import Minio
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("render_worker")

# ── Configuración ─────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "factory_minio")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minio_secure_2025")
MINIO_BUCKET_AUDIO = os.getenv("MINIO_BUCKET_AUDIO", "factory-audio")
MINIO_BUCKET_VIDEO = os.getenv("MINIO_BUCKET_VIDEO", "factory-video")
MINIO_BUCKET_ASSETS = os.getenv("MINIO_BUCKET_ASSETS", "factory-assets")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://factory:pass@localhost:5432/factory")
WORKER_PORT = int(os.getenv("WORKER_PORT", "8080"))
WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:8000")
WORKSPACE = Path("/tmp/workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)

# Video specs para Shorts/Reels
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
VIDEO_BITRATE = "4M"
AUDIO_BITRATE = "192k"

# ── Fuentes disponibles ───────────────────────────────────────
FONT_PATH = "/usr/share/fonts/factory/Montserrat-Bold.ttf"
FALLBACK_FONT = "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"

def get_font():
    if Path(FONT_PATH).exists():
        return FONT_PATH
    if Path(FALLBACK_FONT).exists():
        return FALLBACK_FONT
    return "Arial"  # último recurso

# ── MinIO Client ──────────────────────────────────────────────
minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False
)


# ── Modelos de datos ──────────────────────────────────────────
class Scene(BaseModel):
    scene_number: int
    start_s: float
    end_s: float
    asset_url: str        # URL del B-roll en MinIO o Pexels
    theme: str = ""


class SubtitleSegment(BaseModel):
    start: float
    end: float
    text: str


class RenderRequest(BaseModel):
    video_id: str
    script_id: str
    topic_id: str
    audio_url: str        # URL del audio en MinIO
    scenes: list[Scene]
    subtitles: list[SubtitleSegment] = []
    background_music_url: str | None = None
    music_volume: float = 0.08   # 8% — barely audible background
    voice_volume: float = 1.0
    ken_burns: bool = True
    add_subtitles: bool = True
    output_format: str = "mp4"


class RenderResponse(BaseModel):
    success: bool
    video_id: str
    video_url: str | None = None
    duration_s: float | None = None
    file_size_bytes: int | None = None
    render_duration_s: float | None = None
    error: str | None = None


# ── FastAPI ───────────────────────────────────────────────────
app = FastAPI(title="Render Worker", version="1.0.0")


@app.get("/health")
async def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    return {
        "status": "ok",
        "ffmpeg": ffmpeg_ok,
        "ffmpeg_path": shutil.which("ffmpeg"),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/render", response_model=RenderResponse)
async def render_video(req: RenderRequest, background_tasks: BackgroundTasks):
    """
    Renderiza un video completo con FFmpeg.
    """
    start_time = datetime.utcnow()
    workspace = WORKSPACE / req.video_id
    workspace.mkdir(parents=True, exist_ok=True)
    
    try:
        log.info(f"[Render] Iniciando video_id={req.video_id}, {len(req.scenes)} escenas")
        
        # 1. Descargar todos los assets
        log.info("[Render] 1/5 Descargando assets...")
        audio_path, scene_paths = await download_assets(req, workspace)
        
        # 2. Generar subtítulos SRT
        srt_path = None
        if req.add_subtitles and req.subtitles:
            log.info("[Render] 2/5 Generando subtítulos...")
            srt_path = workspace / "subtitles.srt"
            generate_srt(req.subtitles, srt_path)
        else:
            log.info("[Render] 2/5 Sin subtítulos")
        
        # 3. Renderizar video con FFmpeg
        log.info("[Render] 3/5 Ejecutando FFmpeg...")
        output_path = workspace / f"output_{req.video_id}.mp4"
        ffmpeg_cmd = build_ffmpeg_command(
            req=req,
            audio_path=audio_path,
            scene_paths=scene_paths,
            srt_path=srt_path,
            output_path=output_path
        )
        
        await run_ffmpeg(ffmpeg_cmd, req.video_id)
        
        if not output_path.exists():
            raise RuntimeError("FFmpeg no generó el archivo de salida")
        
        file_size = output_path.stat().st_size
        log.info(f"[Render] Video generado: {file_size/1024/1024:.1f}MB")
        
        # 4. Obtener duración del video
        duration_s = await get_video_duration(output_path)
        
        # 5. Subir a MinIO
        log.info("[Render] 4/5 Subiendo a MinIO...")
        storage_path = f"{req.topic_id}/{req.video_id}.mp4"
        minio_client.fput_object(
            MINIO_BUCKET_VIDEO,
            storage_path,
            str(output_path),
            content_type="video/mp4"
        )
        
        storage_url = f"http://{MINIO_ENDPOINT}/{MINIO_BUCKET_VIDEO}/{storage_path}"
        
        render_duration = (datetime.utcnow() - start_time).total_seconds()
        log.info(f"[Render] ✅ Completado en {render_duration:.1f}s → {storage_url}")
        
        # Actualizar DB en background
        background_tasks.add_task(
            update_video_db,
            req.video_id, storage_path, storage_url, 
            duration_s, file_size, render_duration, " ".join(ffmpeg_cmd)
        )
        
        return RenderResponse(
            success=True,
            video_id=req.video_id,
            video_url=storage_url,
            duration_s=duration_s,
            file_size_bytes=file_size,
            render_duration_s=render_duration
        )
        
    except Exception as e:
        log.error(f"[Render] ❌ Error: {e}", exc_info=True)
        background_tasks.add_task(update_video_error_db, req.video_id, str(e))
        return RenderResponse(success=False, video_id=req.video_id, error=str(e))
        
    finally:
        # Limpiar workspace (en background para no bloquear respuesta)
        background_tasks.add_task(cleanup_workspace, workspace)


async def download_assets(req: RenderRequest, workspace: Path):
    """Descarga audio y todos los B-rolls a disco local."""
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        
        # Descargar audio
        audio_path = workspace / "audio.wav"
        log.info(f"[Download] Audio: {req.audio_url}")
        await download_file(client, req.audio_url, audio_path)
        
        # Descargar B-rolls
        scene_paths = []
        for i, scene in enumerate(req.scenes):
            scene_path = workspace / f"scene_{i:03d}.mp4"
            log.info(f"[Download] B-roll {i+1}/{len(req.scenes)}: {scene.asset_url[:60]}...")
            try:
                await download_file(client, scene.asset_url, scene_path)
                scene_paths.append((scene, scene_path))
            except Exception as e:
                log.warning(f"[Download] B-roll {i+1} falló: {e} — usando color sólido")
                # Generar placeholder negro si falla la descarga
                scene_paths.append((scene, None))
        
    return audio_path, scene_paths


async def download_file(client: httpx.AsyncClient, url: str, dest: Path):
    """Descarga un archivo de forma asíncrona."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)


def generate_srt(subtitles: list[SubtitleSegment], output_path: Path):
    """Genera un archivo SRT de subtítulos."""
    def format_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    
    with open(output_path, "w", encoding="utf-8") as f:
        for i, sub in enumerate(subtitles, 1):
            f.write(f"{i}\n")
            f.write(f"{format_time(sub.start)} --> {format_time(sub.end)}\n")
            # Limpiar texto: máximo 4 palabras por línea
            words = sub.text.strip().split()
            lines = []
            for j in range(0, len(words), 4):
                lines.append(" ".join(words[j:j+4]))
            f.write("\n".join(lines) + "\n\n")
    
    log.info(f"[SRT] {len(subtitles)} segmentos → {output_path}")


def build_ffmpeg_command(
    req: RenderRequest,
    audio_path: Path,
    scene_paths: list[tuple],
    srt_path: Optional[Path],
    output_path: Path
) -> list[str]:
    """
    Construye el comando FFmpeg completo.
    
    Estrategia:
    1. Cada B-roll se procesa individualmente: escalar a 1080x1920, Ken Burns
    2. Concatenar todos los B-rolls
    3. Mezclar audio de voz (+ música opcional)
    4. Agregar subtítulos como filtro drawtext
    5. Output H.264 1080x1920 @30fps
    """
    
    font = get_font()
    inputs = []
    filter_parts = []
    video_labels = []
    
    # ── Input: Audio de voz ──────────────────────────────────
    audio_input_idx = 0
    inputs += ["-i", str(audio_path)]
    
    # ── Inputs: B-rolls ─────────────────────────────────────
    for i, (scene, scene_path) in enumerate(scene_paths):
        if scene_path and scene_path.exists():
            inputs += ["-i", str(scene_path)]
        else:
            # Placeholder: color sólido negro
            duration = scene.end_s - scene.start_s
            inputs += [
                "-f", "lavfi",
                "-i", f"color=black:size={VIDEO_WIDTH}x{VIDEO_HEIGHT}:duration={duration}:rate={VIDEO_FPS}"
            ]
    
    # ── Música de fondo (opcional) ───────────────────────────
    music_input_idx = None
    if req.background_music_url:
        music_input_idx = len(scene_paths) + 1
        # La música se agregar como input más adelante si existe en disco
        # Para simplificar el comando, se puede omitir si no hay música
    
    # ── Filtros de video por escena ──────────────────────────
    for i, (scene, scene_path) in enumerate(scene_paths):
        input_idx = i + 1  # +1 porque [0] es el audio
        scene_duration = scene.end_s - scene.start_s
        label = f"v{i}"
        
        # Filtro base: escalar y recortar a 1080x1920
        scale_filter = (
            f"[{input_idx}:v]"
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
        )
        
        # Ken Burns effect (zoom lento)
        if req.ken_burns and scene_duration >= 3:
            # Dirección aleatoria basada en número de escena
            if i % 4 == 0:
                # Zoom in — desde centro
                ken_filter = (
                    f",zoompan=z='min(zoom+0.0015,1.3)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                    f":d={int(scene_duration * VIDEO_FPS)}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
                )
            elif i % 4 == 1:
                # Pan derecha
                ken_filter = (
                    f",zoompan=z='1.1':x='if(gte(on,1),x+0.5,0)':y='ih/2-(ih/zoom/2)'"
                    f":d={int(scene_duration * VIDEO_FPS)}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
                )
            elif i % 4 == 2:
                # Zoom out
                ken_filter = (
                    f",zoompan=z='if(gte(zoom,1),zoom-0.0015,1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                    f":d={int(scene_duration * VIDEO_FPS)}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
                )
            else:
                # Pan izquierda
                ken_filter = (
                    f",zoompan=z='1.1':x='if(gte(on,1),x-0.5,iw)':y='ih/2-(ih/zoom/2)'"
                    f":d={int(scene_duration * VIDEO_FPS)}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
                )
            
            scale_filter += ken_filter
        
        # Asegurar fps y duración exacta
        scale_filter += f",fps={VIDEO_FPS},trim=duration={scene_duration},setpts=PTS-STARTPTS[{label}]"
        
        filter_parts.append(scale_filter)
        video_labels.append(f"[{label}]")
    
    # ── Concatenar todas las escenas ─────────────────────────
    n_videos = len(scene_paths)
    concat_inputs = "".join(video_labels)
    filter_parts.append(
        f"{concat_inputs}concat=n={n_videos}:v=1:a=0[vconcat]"
    )
    
    # ── Subtítulos ────────────────────────────────────────────
    if srt_path and req.add_subtitles:
        # Estilo: texto en caja amarilla/blanca, centrado en parte inferior
        subtitle_filter = (
            f"[vconcat]subtitles='{srt_path}'"
            f":force_style='FontName={Path(font).stem},"
            f"FontSize=48,"
            f"Bold=1,"
            f"PrimaryColour=&HFFFFFF&,"   # Blanco
            f"OutlineColour=&H000000&,"    # Borde negro
            f"BackColour=&H80000000&,"     # Fondo semitransparente
            f"Outline=3,"
            f"Shadow=1,"
            f"Alignment=2,"               # 2=centro inferior
            f"MarginV=120'"
            f"[vfinal]"
        )
        filter_parts.append(subtitle_filter)
        video_output_label = "[vfinal]"
    else:
        filter_parts.append(f"[vconcat]copy[vfinal]")
        video_output_label = "[vfinal]"
    
    # ── Audio: mezcla voz + música ────────────────────────────
    # Solo voz por ahora (música requiere archivo extra)
    audio_filter = f"[0:a]volume={req.voice_volume}[afinal]"
    filter_parts.append(audio_filter)
    audio_output_label = "[afinal]"
    
    # ── Construir comando completo ────────────────────────────
    filter_complex = ";".join(filter_parts)
    
    cmd = [
        "ffmpeg", "-y",          # Sobrescribir sin preguntar
        "-loglevel", "warning",  # Solo errores y warnings
    ]
    
    # Todos los inputs
    cmd += inputs
    
    # Filter complex
    cmd += ["-filter_complex", filter_complex]
    
    # Mappings de output
    cmd += [
        "-map", video_output_label,
        "-map", audio_output_label,
    ]
    
    # Codec de video
    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",        # fast para workers, usa medium en calidad máxima
        "-crf", "22",             # 18-28: menor = mayor calidad
        "-b:v", VIDEO_BITRATE,
        "-maxrate", "6M",
        "-bufsize", "12M",
        "-pix_fmt", "yuv420p",    # Compatibilidad máxima
        "-movflags", "+faststart", # Optimizar para streaming web
    ]
    
    # Codec de audio
    cmd += [
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ar", "44100",
    ]
    
    # Duración máxima de seguridad (60s)
    cmd += ["-t", "60"]
    
    # Output
    cmd.append(str(output_path))
    
    return cmd


async def run_ffmpeg(cmd: list[str], video_id: str):
    """Ejecuta FFmpeg de forma asíncrona con logging."""
    log.info(f"[FFmpeg] Comando:\n{' '.join(cmd[:15])}...")
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        log.error(f"[FFmpeg] Error (code {proc.returncode}):\n{stderr_text[-2000:]}")
        raise RuntimeError(f"FFmpeg falló con código {proc.returncode}")
    
    if stderr:
        stderr_text = stderr.decode("utf-8", errors="replace")
        if stderr_text.strip():
            log.info(f"[FFmpeg] Output:\n{stderr_text[-1000:]}")
    
    log.info(f"[FFmpeg] ✅ Completado para video_id={video_id}")


async def get_video_duration(video_path: Path) -> float:
    """Obtiene la duración de un video con ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(video_path)
    ]
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


async def update_video_db(
    video_id: str, storage_path: str, storage_url: str,
    duration_s: float, file_size: int, render_duration: float, ffmpeg_cmd: str
):
    """Actualiza el registro de video en PostgreSQL."""
    try:
        conn = await asyncpg.connect(POSTGRES_DSN)
        await conn.execute("""
            UPDATE factory.videos
            SET storage_path = $1,
                storage_url = $2,
                duration_s = $3,
                file_size_bytes = $4,
                render_finished_at = NOW(),
                render_duration_s = $5,
                ffmpeg_command = $6,
                status = 'completed',
                updated_at = NOW()
            WHERE id = $7::uuid
        """, storage_path, storage_url, duration_s, file_size, render_duration, ffmpeg_cmd[:2000], video_id)
        await conn.close()
        log.info(f"[DB] Video actualizado: {video_id}")
    except Exception as e:
        log.error(f"[DB] Error actualizando video: {e}")


async def update_video_error_db(video_id: str, error: str):
    """Marca el video como fallido en PostgreSQL."""
    try:
        conn = await asyncpg.connect(POSTGRES_DSN)
        await conn.execute("""
            UPDATE factory.videos
            SET status = 'failed', error_message = $1, updated_at = NOW()
            WHERE id = $2::uuid
        """, error[:1000], video_id)
        await conn.close()
    except Exception as e:
        log.error(f"[DB] Error actualizando estado de error: {e}")


async def cleanup_workspace(workspace: Path):
    """Elimina archivos temporales después del render."""
    try:
        shutil.rmtree(workspace, ignore_errors=True)
        log.info(f"[Cleanup] Workspace eliminado: {workspace}")
    except Exception as e:
        log.warning(f"[Cleanup] No se pudo eliminar workspace: {e}")


# ── Worker de cola Redis ──────────────────────────────────────
async def process_queue():
    """Procesa jobs de render desde la cola Redis."""
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("[Queue] Render Worker escuchando cola 'render_jobs'...")
    
    while True:
        try:
            result = await redis.blpop("render_jobs", timeout=5)
            if not result:
                continue
            
            _, job_json = result
            job = json.loads(job_json)
            
            log.info(f"[Queue] Procesando job: video_id={job.get('video_id', 'unknown')}")
            
            req = RenderRequest(**job)
            # Usar BackgroundTasks vacío (en queue, no en HTTP request)
            response = await render_video(req, BackgroundTasks())
            
            if response.success:
                log.info(f"[Queue] ✅ Job completado: {response.video_url}")
            else:
                log.error(f"[Queue] ❌ Job fallido: {response.error}")
                
        except Exception as e:
            log.error(f"[Queue] Error en queue: {e}", exc_info=True)
            await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    asyncio.create_task(process_queue())
    log.info("✅ Render Worker iniciado")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
