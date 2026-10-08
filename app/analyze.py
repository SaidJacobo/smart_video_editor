"""Analisis de un par gameplay + webcam para la API: mismo flujo que
editor.py::_build_session (sin --con-neutro), pero con los dos audios que
sube el frontend y devolviendo los tramos a conservar en vez de armar los
.kdenlive. Ver SPEC-fastapi.md."""
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter

from gameplay_editor import classification, ffmpeg_utils, project_short, transcription
from gameplay_editor.config import load_config

# clave de cache de transcription/classification; da igual cual sea porque
# el output_dir es un directorio temporal nuevo en cada request
_CACHE_KEY = "analisis"


def _log(message):
    print(f"[analyze {time.strftime('%H:%M:%S')}] {message}", flush=True)


@contextlib.contextmanager
def _carpeta_de_trabajo():
    """Carpeta temporal que se borra al terminar; con DEBUG_OUTPUT_DIR, una
    subcarpeta nueva <fecha-hora> que se conserva con los .json de la corrida."""
    debug_dir = os.environ.get("DEBUG_OUTPUT_DIR")
    if not debug_dir:
        with tempfile.TemporaryDirectory(prefix="video_editor_") as tmp:
            yield tmp
        return
    base = os.path.join(debug_dir, time.strftime("%Y-%m-%d_%H-%M-%S"))
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = f"{base}_{n}"
    os.makedirs(path)
    _log(f"guardando la corrida en {os.path.abspath(path)}")
    yield path


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


def _ventanas_candidatas(classified, duration, padding):
    """Ventanas que pueden aparecer en el video largo, sin fusionar: las que
    tienen dialogo del juego (confidence None, siempre visibles) y las
    divertido_interesante (con la confianza del clasificador; el editor las
    filtra con el slider). Con el margen de padding a cada lado."""
    candidatas = []
    for w in classified:
        if w["tiene_dialogo_juego"]:
            confidence = None
        elif w["categoria"] == "divertido_interesante":
            # ollama no da confianza: se toma como segura
            confidence = 1.0 if w["confianza"] is None else w["confianza"]
        else:
            continue
        candidatas.append({
            "start": max(0.0, w["inicio"] - padding),
            "end": min(duration, w["fin"] + padding),
            "confidence": confidence,
        })
    return candidatas


def analyze(gameplay_file, webcam_file, webcam_offset):
    """gameplay_file/webcam_file son file-likes binarios con el audio de cada
    pista. Devuelve {duration, windows: [{start, end, confidence}], shorts:
    [{start, end}]} en segundos del gameplay (ver CONTRATO-CORTES.md). Lanza AnalysisError si algun audio no se puede leer o si
    el clasificador no responde."""
    cfg = load_config()
    started = time.monotonic()
    with _carpeta_de_trabajo() as tmp:
        gameplay = _guardar(gameplay_file, os.path.join(tmp, "gameplay"))
        webcam = _guardar(webcam_file, os.path.join(tmp, "webcam"))
        duration = _duracion_audio(gameplay, "No se pudo leer el audio del gameplay.")
        webcam_duration = _duracion_audio(webcam, "No se pudo leer el audio de la webcam.")
        _log(f"audios recibidos: gameplay {duration / 60:.1f} min, webcam {webcam_duration / 60:.1f} min, "
             f"offset {webcam_offset:g}s")

        _log("transcribiendo (Whisper, puede tardar varios minutos)...")
        t = transcription.transcribe(gameplay, webcam, cfg["transcription"], _CACHE_KEY, output_dir=tmp)
        t["jugador"] = correr_offset(t["jugador"], webcam_offset, duration)
        _log(f"transcripcion lista: jugador {len(t['jugador'])} frases, juego {len(t['juego'])} frases "
             f"({time.monotonic() - started:.0f}s desde el inicio)")

        _log("clasificando ventanas...")
        try:
            classified = classification.classify(
                t, cfg["classification"], _CACHE_KEY, output_dir=tmp, duration=duration,
            )
        except RuntimeError as e:
            print(f"    [clasificador] {e}")
            raise AnalysisError(502, "No se pudo conectar con el clasificador.") from e

        _log(f"{len(classified)} ventanas -> {dict(Counter(w['categoria'] for w in classified))}")

        candidatas = _ventanas_candidatas(classified, duration, cfg["classification"]["solo_relevante_padding_sec"])
        s = cfg["shorts"]
        windows = project_short.highlight_windows(
            classification.classification_highlights(classified), duration,
            s["pre_roll_sec"], s["post_roll_sec"], s["merge_gap_sec"],
        )
    con_dialogo = sum(1 for w in candidatas if w["confidence"] is None)
    _log(f"listo en {time.monotonic() - started:.0f}s: {len(candidatas)} ventanas candidatas "
         f"({con_dialogo} con dialogo del juego), {len(windows)} shorts")
    return {
        "duration": duration,
        "windows": candidatas,
        "shorts": [{"start": a, "end": b} for a, b, _ in windows],
    }
