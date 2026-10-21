"""Analisis de un video (y opcionalmente la camara de quien graba) para la
API: mismo flujo que editor.py::_build_session (sin --con-neutro), pero con
los audios que sube el frontend, las categorias que eligio el usuario y
devolviendo los tramos a conservar en vez de armar los .kdenlive. Ver
CONTRATO-CORTES.md y SPEC-CONTENIDO-GENERAL.md."""
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter

from gameplay_editor import classification, ffmpeg_utils, transcription
from gameplay_editor.config import load_config

# clave de la cache propia de transcription.transcribe; da igual cual sea
# porque su output_dir es un directorio temporal nuevo en cada request
_CACHE_KEY = "analisis"

# transcripciones y clasificaciones ya hechas, por hash de los audios: el
# navegador extrae siempre los mismos bytes del mismo video, asi que volver a
# analizar no repite Whisper ni las llamadas al clasificador
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")


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


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transcribir(hashes, main, secondary, cfg, tmp):
    """transcription.transcribe con cache por contenido de los audios
    (hashes) y por la config de Whisper (cfg es cfg["transcription"])."""
    # "creador/contenido": las caches de antes tenian las pistas como jugador/juego
    clave = hashlib.sha256(
        json.dumps([*hashes, cfg, "creador/contenido"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    path = os.path.join(_CACHE_DIR, f"{clave}.transcripcion.json")
    if os.path.exists(path):
        _log("transcripcion ya hecha antes para estos audios, se reutiliza")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    _log("transcribiendo (Whisper, puede tardar varios minutos)...")
    t = transcription.transcribe(main, secondary, cfg, _CACHE_KEY, output_dir=tmp)
    t = {"creador": t["creador"], "contenido": t["contenido"]}
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(t, fh, ensure_ascii=False)
    return t


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
    """Pasa segmentos del segundo video a tiempo del principal (w - offset).
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


def _ventanas_candidatas(classified, duration, padding, kept):
    """Ventanas que pueden aparecer en el video largo, sin fusionar: las de
    categorias en Conservar, con la confianza del clasificador (el editor las
    filtra con el slider). Con el margen de padding a cada lado."""
    return [
        {
            "start": max(0.0, w["inicio"] - padding),
            "end": min(duration, w["fin"] + padding),
            "category": w["categoria"],
            # ollama no da confianza: se toma como segura
            "confidence": 1.0 if w["confianza"] is None else w["confianza"],
        }
        for w in classified
        if w["categoria"] in kept
    ]


def analyze(main_file, secondary_file, secondary_offset, settings):
    """main_file/secondary_file son file-likes binarios con el audio de cada
    video (secondary_file None si hay uno solo). settings es el
    AnalysisSettings ya validado. Devuelve {duration, windows, shorts} con
    [{start, end, category, confidence}] en segundos del video principal (ver
    CONTRATO-CORTES.md). Lanza AnalysisError si algun audio no se puede leer
    o si el clasificador no responde."""
    cfg = load_config()
    started = time.monotonic()
    with _carpeta_de_trabajo() as tmp:
        main = _guardar(main_file, os.path.join(tmp, "main"))
        duration = _duracion_audio(main, "No se pudo leer el audio del video.")
        secondary = None
        if secondary_file is not None:
            secondary = _guardar(secondary_file, os.path.join(tmp, "secondary"))
            secondary_duration = _duracion_audio(secondary, "No se pudo leer el audio de la cámara.")
            _log(f"audios recibidos: video {duration / 60:.1f} min, camara {secondary_duration / 60:.1f} min, "
                 f"offset {secondary_offset:g}s")
        else:
            _log(f"audio recibido: video {duration / 60:.1f} min, sin camara")

        hashes = [_sha256(path) for path in (main, secondary) if path]
        t = _transcribir(hashes, main, secondary, cfg["transcription"], tmp)
        if secondary:
            t["creador"] = correr_offset(t["creador"], secondary_offset, duration)
            _log(f"transcripcion lista: creador {len(t['creador'])} frases, contenido {len(t['contenido'])} "
                 f"frases ({time.monotonic() - started:.0f}s desde el inicio)")
        else:
            _log(f"transcripcion lista: {len(t['contenido'])} frases "
                 f"({time.monotonic() - started:.0f}s desde el inicio)")

        _log("clasificando ventanas (las ya clasificadas para estos audios, este offset y estas "
             "categorias se reutilizan)...")
        try:
            # la cache de classify se invalida sola si cambia el texto de las
            # ventanas (otro offset), el prompt, el modelo o los settings, y si se
            # corta a la mitad retoma desde la ultima ventana
            classified = classification.classify(
                t, cfg["classification"], settings,
                hashlib.sha256("".join(hashes).encode("utf-8")).hexdigest(),
                output_dir=_CACHE_DIR, duration=duration,
            )
        except RuntimeError as e:
            print(f"    [clasificador] {e}")
            raise AnalysisError(502, "No se pudo conectar con el clasificador.") from e

        _log(f"{len(classified)} ventanas -> {dict(Counter(w['categoria'] for w in classified))}")

        padding = cfg["classification"]["solo_relevante_padding_sec"]
        kept = [c["label"] for c in settings["categories"] if c["keep"]]
        candidatas = _ventanas_candidatas(classified, duration, padding, kept)
        shorts = classification.short_windows(classified, duration, padding, kept, exigir_creador=bool(secondary))
    _log(f"listo en {time.monotonic() - started:.0f}s: {len(candidatas)} ventanas candidatas, "
         f"{len(shorts)} shorts")
    return {
        "duration": duration,
        "windows": candidatas,
        "shorts": shorts,
    }
