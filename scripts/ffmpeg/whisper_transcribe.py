#!/usr/bin/env python3
"""
whisper_transcribe.py — Transcripción local con Faster-Whisper
Genera subtítulos SRT con timestamp por palabra para sincronización perfecta.

Uso:
  python whisper_transcribe.py --audio audio.wav --output subtitles.srt
  python whisper_transcribe.py --url http://localhost:9000/factory-audio/test.wav --output subs.srt
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path


def transcribe_via_api(audio_url: str, whisper_url: str = "http://localhost:8000") -> dict:
    """
    Transcribe usando el servidor Faster-Whisper (already running in Docker).
    Más eficiente: reutiliza el servicio ya desplegado.
    """
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "url": audio_url,
        "language": "es",
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"]
    }).encode()

    req = urllib.request.Request(
        f"{whisper_url}/v1/audio/transcriptions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.URLError as e:
        print(f"❌ Error conectando con Whisper API: {e}", file=sys.stderr)
        sys.exit(1)


def transcribe_local(audio_path: Path, model_size: str = "small") -> dict:
    """
    Transcribe localmente usando faster-whisper.
    Requiere: pip install faster-whisper
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("❌ faster-whisper no instalado. Ejecutar: pip install faster-whisper", file=sys.stderr)
        sys.exit(1)

    print(f"🔄 Cargando modelo Whisper '{model_size}'...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print(f"🎙️ Transcribiendo: {audio_path.name}")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language="es",
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )

    segments = []
    words_all = []

    for seg in segments_iter:
        segment_words = []
        if seg.words:
            for w in seg.words:
                word_data = {
                    "word": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "probability": round(w.probability, 3)
                }
                segment_words.append(word_data)
                words_all.append(word_data)

        segments.append({
            "id": len(segments),
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": segment_words
        })

    return {
        "text": " ".join(s["text"] for s in segments),
        "language": info.language,
        "duration": info.duration,
        "segments": segments,
        "words": words_all
    }


def words_to_subtitle_groups(words: list, words_per_group: int = 4) -> list:
    """
    Agrupa palabras en segmentos de subtítulo.
    Optimizado para legibilidad en pantalla vertical (mobile).
    """
    groups = []
    i = 0
    while i < len(words):
        group = words[i:i + words_per_group]
        if not group:
            break
        groups.append({
            "start": group[0]["start"],
            "end": group[-1]["end"],
            "text": " ".join(w["word"] for w in group).strip()
        })
        i += words_per_group
    return groups


def generate_srt(segments: list, output_path: Path):
    """Genera archivo SRT estándar."""
    def fmt(s: float) -> str:
        h, m = int(s // 3600), int((s % 3600) // 60)
        sec, ms = int(s % 60), int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            text = seg["text"].strip()
            if not text:
                continue
            f.write(f"{i}\n")
            f.write(f"{fmt(seg['start'])} --> {fmt(seg['end'])}\n")
            f.write(f"{text}\n\n")

    print(f"✅ SRT generado: {output_path} ({len(segments)} segmentos)")


def generate_json(data: dict, output_path: Path):
    """Guarda la transcripción completa como JSON (para n8n y la DB)."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON guardado: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Transcripción Whisper + generación SRT")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audio", help="Ruta al archivo de audio local")
    group.add_argument("--url", help="URL del audio (usa Whisper API en Docker)")

    parser.add_argument("--output", default="subtitles.srt", help="Archivo SRT de salida")
    parser.add_argument("--json-output", default=None, help="Guardar transcripción completa en JSON")
    parser.add_argument("--whisper-url", default="http://localhost:8000", help="URL del servidor Whisper")
    parser.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium"],
                        help="Tamaño del modelo (solo modo local)")
    parser.add_argument("--words-per-sub", type=int, default=4,
                        help="Palabras por segmento de subtítulo")
    args = parser.parse_args()

    # Transcribir
    if args.url:
        print(f"🌐 Transcribiendo via API Whisper: {args.whisper_url}")
        result = transcribe_via_api(args.url, args.whisper_url)
    else:
        result = transcribe_local(Path(args.audio), args.model)

    print(f"📝 Transcripción: {result.get('text', '')[:100]}...")
    print(f"⏱️  Duración: {result.get('duration', 0):.2f}s | Idioma: {result.get('language', 'es')}")

    # Generar segmentos de subtítulo
    words = result.get("words", [])
    if words:
        subtitle_segments = words_to_subtitle_groups(words, args.words_per_sub)
        print(f"📊 Segmentos de subtítulo por palabras: {len(subtitle_segments)}")
    else:
        # Fallback: usar segmentos directamente
        subtitle_segments = result.get("segments", [])
        print(f"📊 Usando segmentos de Whisper: {len(subtitle_segments)}")

    # Generar SRT
    generate_srt(subtitle_segments, Path(args.output))

    # Guardar JSON completo (opcional)
    if args.json_output:
        # Añadir segmentos de subtítulo procesados al JSON
        result["subtitle_segments"] = subtitle_segments
        generate_json(result, Path(args.json_output))

    print("\n🎉 Listo. Archivo SRT generado para usar con FFmpeg.")
    print(f"   ffmpeg -i video.mp4 -vf subtitles='{args.output}' output_with_subs.mp4")


if __name__ == "__main__":
    main()
