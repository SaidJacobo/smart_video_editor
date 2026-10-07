"""Analisis de un par gameplay + webcam para la API: mismo flujo que
editor.py::_build_session (sin --con-neutro), pero con los dos audios que
sube el frontend y devolviendo los tramos a conservar en vez de armar los
.kdenlive. Ver SPEC-fastapi.md."""
import os
import shutil
import subprocess
import tempfile

from gameplay_editor import classification, ffmpeg_utils, project_short, transcription
from gameplay_editor.config import load_config

# clave de cache de transcription/classification; da igual cual sea porque
# el output_dir es un directorio temporal nuevo en cada request
_CACHE_KEY = "analisis"


class AnalysisError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _guardar(upload, path):
    with open(path, "wb") as fh:
        shutil.copyfileobj(upload, fh)
    return path


def _duracion_audio(path, error):
    """Duracion segun ffprobe. Cualquier cosa que ffprobe no pueda leer, o
    que no tenga pista de audio, es un archivo invalido."""
    try:
        data = ffmpeg_utils.probe(path)
        if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
            raise ValueError("sin pista de audio")
        return float(data["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError) as e:
        raise AnalysisError(400, error) from e


def correr_offset(segmentos, offset, duration):
    """Pasa segmentos de la webcam a tiempo del gameplay (w - offset).
    Descarta los que quedan fuera de [0, duration] y recorta los del borde."""
    corridos = []
    for s in segmentos:
        inicio = s["inicio"] - offset
        fin = s["fin"] - offset
        if fin <= 0 or inicio >= duration:
            continue
        corridos.append({
            **s,
            "inicio": round(max(0.0, inicio), 2),
            "fin": round(min(duration, fin), 2),
        })
    return corridos


def analyze(gameplay_file, webcam_file, webcam_offset):
    """gameplay_file/webcam_file son file-likes binarios con el audio de cada
    pista. Devuelve {duration, segments: [{start, end}], shorts: [{start, end}]}
    en segundos del gameplay. Lanza AnalysisError si algun audio no se puede leer o si
    el clasificador no responde."""
    cfg = load_config()
    with tempfile.TemporaryDirectory(prefix="video_editor_") as tmp:
        gameplay = _guardar(gameplay_file, os.path.join(tmp, "gameplay"))
        webcam = _guardar(webcam_file, os.path.join(tmp, "webcam"))
        duration = _duracion_audio(gameplay, "No se pudo leer el audio del gameplay.")
        _duracion_audio(webcam, "No se pudo leer el audio de la webcam.")

        t = transcription.transcribe(gameplay, webcam, cfg["transcription"], _CACHE_KEY, output_dir=tmp)
        t["jugador"] = correr_offset(t["jugador"], webcam_offset, duration)

        try:
            classified = classification.classify(
                t, cfg["classification"], _CACHE_KEY, output_dir=tmp, duration=duration,
            )
        except RuntimeError as e:
            print(f"    [clasificador] {e}")
            raise AnalysisError(502, "No se pudo conectar con el clasificador.") from e

        keep = classification.keep_segments_for_categories(
            classified, duration, ["divertido_interesante"],
            padding_sec=cfg["classification"]["solo_relevante_padding_sec"],
        )
        s = cfg["shorts"]
        windows = project_short.highlight_windows(
            classification.classification_highlights(classified), duration,
            s["pre_roll_sec"], s["post_roll_sec"], s["merge_gap_sec"],
        )
    return {
        "duration": duration,
        "segments": [{"start": a, "end": b} for a, b in keep],
        "shorts": [{"start": a, "end": b} for a, b, _ in windows],
    }
