#!/usr/bin/env python3
"""
ffmpeg_render.py — Script standalone de render FFmpeg
Útil para testing fuera del worker o render manual de un video.

Uso:
  python ffmpeg_render.py \
    --audio audio.wav \
    --scenes scenes.json \
    --subs subtitles.srt \
    --output output.mp4
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ── Constantes de video ───────────────────────────────────────
WIDTH, HEIGHT = 1080, 1920
FPS = 30
VIDEO_BITRATE = "4M"
AUDIO_BITRATE = "192k"
CRF = 22

# Ruta de fuente para subtítulos
FONT_CANDIDATES = [
    "/usr/share/fonts/factory/Montserrat-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",       # macOS
    "C:/Windows/Fonts/arialbd.ttf",              # Windows
]


def find_font():
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    return "Arial"


def escape_ffmpeg_path(path: str) -> str:
    """Escapa caracteres especiales en rutas para FFmpeg filtergraph."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def generate_srt(segments: list, output_path: Path):
    """Genera archivo SRT desde lista de segmentos."""
    def fmt(s):
        h, m = int(s // 3600), int((s % 3600) // 60)
        sec, ms = int(s % 60), int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            text = seg.get("text", "").strip()
            # Máximo 4 palabras por línea para legibilidad en mobile
            words = text.split()
            lines = [" ".join(words[j:j+4]) for j in range(0, len(words), 4)]
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n")
            f.write("\n".join(lines) + "\n\n")


def build_command(
    audio_path: Path,
    scene_files: list,       # list of (path, duration_s, scene_number)
    srt_path: Path | None,
    output_path: Path,
    music_path: Path | None = None,
    music_vol: float = 0.08,
    ken_burns: bool = True,
    verbose: bool = False,
) -> list:
    """
    Construye el comando FFmpeg completo para un Short vertical.

    Arquitectura del filtergraph:
      [1:v] → scale+crop → Ken Burns → fps+trim → [v0]
      [2:v] → scale+crop → Ken Burns → fps+trim → [v1]
      ...
      [v0][v1]...concat → [vconcat]
      [vconcat] → subtitles → [vfinal]
      [0:a] → volume → [afinal]
    """

    font = find_font()
    cmd = ["ffmpeg", "-y", "-loglevel", "warning" if not verbose else "info"]

    # ── Inputs ────────────────────────────────────────────────
    # Input 0: audio de voz
    cmd += ["-i", str(audio_path)]

    # Inputs 1..N: B-rolls o color sólido
    for path, duration, _ in scene_files:
        if path and path.exists():
            cmd += ["-i", str(path)]
        else:
            cmd += [
                "-f", "lavfi",
                "-i", f"color=c=0x1a1a2e:size={WIDTH}x{HEIGHT}:duration={duration}:rate={FPS}"
            ]

    # Input música (opcional)
    if music_path and music_path.exists():
        cmd += ["-i", str(music_path)]
        music_idx = len(scene_files) + 1
    else:
        music_idx = None

    # ── Filtergraph ───────────────────────────────────────────
    parts = []
    labels = []

    for i, (path, duration, scene_num) in enumerate(scene_files):
        inp = i + 1  # audio ocupa [0]
        label = f"v{i}"
        dur = max(duration, 1.0)
        frames = int(dur * FPS)

        # Scale + Crop obligatorio para formato vertical
        base = (
            f"[{inp}:v]"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT}"
        )

        # Ken Burns — 4 patrones rotativos
        if ken_burns and dur >= 3:
            pattern = scene_num % 4
            if pattern == 0:
                # Zoom in desde el centro
                kb = (
                    f",zoompan="
                    f"z='min(zoom+0.0012,1.25)':"
                    f"x='iw/2-(iw/zoom/2)':"
                    f"y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
                )
            elif pattern == 1:
                # Pan lento de izquierda a derecha
                kb = (
                    f",zoompan="
                    f"z='1.12':"
                    f"x='if(gte(on,1),x+0.4,0)':"
                    f"y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
                )
            elif pattern == 2:
                # Zoom out suave
                kb = (
                    f",zoompan="
                    f"z='if(gte(zoom,1.0),zoom-0.001,1.0)':"
                    f"x='iw/2-(iw/zoom/2)':"
                    f"y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
                )
            else:
                # Pan de derecha a izquierda
                kb = (
                    f",zoompan="
                    f"z='1.12':"
                    f"x='if(gte(on,1),x-0.4,iw*0.1)':"
                    f"y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
                )
            base += kb

        # Garantizar FPS y duración exacta antes de concat
        base += f",fps={FPS},trim=duration={dur:.3f},setpts=PTS-STARTPTS[{label}]"
        parts.append(base)
        labels.append(f"[{label}]")

    # Concatenar escenas (sin audio — audio se mezcla por separado)
    n = len(scene_files)
    concat_in = "".join(labels)
    parts.append(f"{concat_in}concat=n={n}:v=1:a=0[vconcat]")

    # Subtítulos burned-in con drawtext avanzado
    if srt_path and srt_path.exists():
        srt_escaped = escape_ffmpeg_path(str(srt_path))
        font_escaped = escape_ffmpeg_path(font)

        # Estilo de subtítulo: texto blanco con borde negro grueso y sombra
        sub_filter = (
            f"[vconcat]subtitles='{srt_escaped}':fontsdir='{Path(font).parent}':"
            f"force_style='"
            f"FontName={Path(font).stem},"
            f"FontSize=52,"
            f"Bold=1,"
            f"PrimaryColour=&H00FFFFFF,"    # Blanco puro
            f"OutlineColour=&H00000000,"    # Negro puro para borde
            f"BackColour=&H60000000,"        # Fondo semitransparente
            f"Outline=4,"                   # Borde grueso
            f"Shadow=2,"
            f"ShadowColour=&H40000000,"
            f"Alignment=2,"                 # Centro-inferior
            f"MarginV=140,"                 # Margen del borde inferior
            f"MarginL=40,"
            f"MarginR=40"
            f"'[vfinal]"
        )
        parts.append(sub_filter)
        v_out = "[vfinal]"
    else:
        parts.append("[vconcat]null[vfinal]")
        v_out = "[vfinal]"

    # Audio: voz (+ música si existe)
    if music_idx is not None:
        # Mezclar voz + música con volumen reducido de música
        parts.append(
            f"[0:a]volume=1.0[voice];"
            f"[{music_idx}:a]volume={music_vol},aloop=loop=-1:size=2e+09[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[afinal]"
        )
    else:
        parts.append("[0:a]volume=1.0[afinal]")

    a_out = "[afinal]"

    # ── Construir comando final ───────────────────────────────
    filtergraph = ";".join(parts)
    cmd += ["-filter_complex", filtergraph]
    cmd += ["-map", v_out, "-map", a_out]

    # Codec H.264 con configuración para Shorts
    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(CRF),
        "-b:v", VIDEO_BITRATE,
        "-maxrate", "6M",
        "-bufsize", "12M",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-g", str(FPS * 2),      # Keyframe cada 2 segundos
        "-sc_threshold", "0",
    ]
    cmd += [
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ar", "44100",
        "-ac", "2",
    ]

    # Limit a 60 segundos (máximo para Shorts)
    cmd += ["-t", "60"]
    cmd.append(str(output_path))

    return cmd


def render(
    audio_path: Path,
    scenes_json: Path,
    srt_path: Path | None,
    output_path: Path,
    music_path: Path | None = None,
    verbose: bool = False,
):
    """Ejecuta el render completo."""
    with open(scenes_json) as f:
        scenes_data = json.load(f)

    # Preparar lista de escenas
    scene_files = []
    for scene in scenes_data:
        asset_path = Path(scene.get("local_path", "")) if scene.get("local_path") else None
        duration = scene["end_s"] - scene["start_s"]
        scene_num = scene.get("scene_number", 1)
        scene_files.append((asset_path, duration, scene_num))

    if not scene_files:
        print("❌ No hay escenas válidas", file=sys.stderr)
        sys.exit(1)

    cmd = build_command(
        audio_path=audio_path,
        scene_files=scene_files,
        srt_path=srt_path,
        output_path=output_path,
        music_path=music_path,
        ken_burns=True,
        verbose=verbose,
    )

    print("🎬 Ejecutando FFmpeg...")
    if verbose:
        print("Comando:", " ".join(cmd[:20]), "...")

    result = subprocess.run(cmd, capture_output=not verbose)

    if result.returncode != 0:
        print(f"❌ FFmpeg falló (código {result.returncode})", file=sys.stderr)
        if not verbose:
            print(result.stderr.decode(errors="replace")[-2000:], file=sys.stderr)
        sys.exit(1)

    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"✅ Video generado: {output_path} ({size_mb:.1f} MB)")

    # Verificar duración con ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(output_path)],
        capture_output=True, text=True
    )
    if probe.stdout.strip():
        print(f"⏱️  Duración: {float(probe.stdout.strip()):.2f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FFmpeg Render para YouTube Shorts")
    parser.add_argument("--audio", required=True, help="Archivo WAV de voz")
    parser.add_argument("--scenes", required=True, help="JSON con lista de escenas y local_path")
    parser.add_argument("--subs", default=None, help="Archivo SRT de subtítulos (opcional)")
    parser.add_argument("--music", default=None, help="Archivo MP3/WAV de música de fondo (opcional)")
    parser.add_argument("--output", default="output.mp4", help="Archivo de salida")
    parser.add_argument("--verbose", action="store_true", help="Output detallado de FFmpeg")
    args = parser.parse_args()

    render(
        audio_path=Path(args.audio),
        scenes_json=Path(args.scenes),
        srt_path=Path(args.subs) if args.subs else None,
        output_path=Path(args.output),
        music_path=Path(args.music) if args.music else None,
        verbose=args.verbose,
    )
