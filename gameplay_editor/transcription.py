"""Transcripcion de las dos pistas (webcam=jugador, gameplay=juego) con
faster-whisper, corriendo local sin GPU dedicada. Cachea en
<titulo>.transcripcion.json, invalidado por (mtime de los fuentes, modelo).

Ver spec_clasificacion_contenido.md, Paso 1.
"""
import json
import os

_MODEL_CACHE = {}


def _load_model(model_name, compute_type):
    key = (model_name, compute_type)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        _MODEL_CACHE[key] = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    return _MODEL_CACHE[key]


def _resegment_by_word_gaps(seg, max_gap_sec):
    """Whisper (con VAD) a veces agrupa frases separadas por silencios largos
    en UN solo segmento, con el inicio de la primera palabra y el fin de la
    ultima -- vimos casos reales de 60-127s de "duracion" para una frase de
    2-3 palabras, con el resto siendo puro silencio en el medio. Si el
    segmento no tiene word_timestamps no hay nada que partir; si los tiene,
    lo re-parte en cada hueco entre palabras mayor a max_gap_sec, para que
    cada sub-frase quede con su rango de tiempo real."""
    if not seg.words:
        return [(seg.start, seg.end, seg.text.strip())]

    pieces, current = [], [seg.words[0]]
    for w in seg.words[1:]:
        if w.start - current[-1].end > max_gap_sec:
            pieces.append(current)
            current = []
        current.append(w)
    pieces.append(current)

    return [
        (words[0].start, words[-1].end, "".join(w.word for w in words).strip())
        for words in pieces
    ]


def transcribe_track(path, fuente, cfg):
    """Transcribe un archivo de audio/video y devuelve una lista de segmentos
    {inicio, fin, texto, fuente}, a nivel frase. Pide timestamps por palabra
    y re-parte cada segmento de Whisper en sus huecos internos grandes (ver
    _resegment_by_word_gaps) para evitar que una frase corta quede con un
    rango de tiempo inflado por silencio que en realidad no le pertenece."""
    model = _load_model(cfg["model"], cfg["compute_type"])
    segments, _info = model.transcribe(
        path,
        vad_filter=cfg["vad_filter"],
        beam_size=cfg.get("beam_size", 5),
        word_timestamps=True,
    )
    max_gap_sec = cfg.get("max_word_gap_sec", 2.0)
    result = []
    for seg in segments:
        for start, end, texto in _resegment_by_word_gaps(seg, max_gap_sec):
            if texto:
                result.append({
                    "inicio": round(start, 2),
                    "fin": round(end, 2),
                    "texto": texto,
                    "fuente": fuente,
                })
    return result


def _cache_path(titulo, output_dir):
    return os.path.join(output_dir or ".", f"{titulo}.transcripcion.json")


def _sources_signature(paths, cfg):
    sig = {p: os.path.getmtime(p) for p in paths}
    sig["_model"] = cfg["model"]
    sig["_vad_filter"] = cfg["vad_filter"]
    sig["_max_word_gap_sec"] = cfg.get("max_word_gap_sec", 2.0)
    return sig


def transcribe(gameplay_path, webcam_path, cfg, titulo, output_dir=None, force=False):
    """Transcribe ambas pistas por separado. cfg es cfg["transcription"]
    (ver config.py). Devuelve {jugador: [...], juego: [...]}, cacheado."""
    cache_file = _cache_path(titulo, output_dir)
    sig = _sources_signature([gameplay_path, webcam_path], cfg)

    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("sources") == sig:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    jugador = transcribe_track(webcam_path, "jugador", cfg)
    juego = transcribe_track(gameplay_path, "juego", cfg)

    result = {
        "titulo": titulo,
        "sources": sig,
        "jugador": jugador,
        "juego": juego,
    }
    os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    return result
