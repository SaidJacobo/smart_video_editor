"""Config por perfil de sesion (spec item F). JSON, con defaults razonables."""
import copy
import json

DEFAULT_CONFIG = {
    "project": {
        # fps/resolution: si es null se auto-detectan a partir del gameplay
        "fps": None,
        "width": None,
        "height": None,
    },
    "silence": {
        # de que pista se toma la referencia para cortar silencios: webcam | gameplay | mix
        "reference": "webcam",
        "noise_db": -30.0,
        "min_silence_sec": 5,
        "min_keep_sec": 0.3,
        "padding_sec": 0.15,
        # protege del corte los tramos de silencio (de mic) donde hay un pico
        # de audio del gameplay (disparo, explosion, etc) respecto al nivel de
        # sonido reciente -- asi no depende de que estes hablando para
        # conservar un momento importante en silencio.
        "protect_peaks": {
            "enabled": False,
            "window_sec": 0.5,
            "z_threshold": 1.5,
            "pad_before_sec": 1.0,
            "pad_after_sec": 1.5,
        },
    },
    "highlights": {
        "window_sec": 1.0,
        "z_threshold": 2.0,
        "min_gap_sec": 25.0,
        "max_highlights": 20,
    },
    "transcription": {
        "model": "medium",
        "compute_type": "int8",
        "vad_filter": True,
        "beam_size": 5,
        # si el hueco entre dos palabras de un mismo segmento de Whisper supera
        # esto, se re-parte en sub-segmentos (ver transcription._resegment_by_word_gaps)
        "max_word_gap_sec": 2.0,
    },
    "classification": {
        "backend": "ollama",  # "ollama" | "api"
        "model": "qwen2.5:7b-instruct",
        "window_sec": 45.0,
        "overlap_sec": 10.0,
        "context_windows": 2,
        "categories": ["divertido_interesante", "relleno", "neutro"],
    },
    "long": {
        # posicion/tamano del recuadro de webcam, en % del canvas (0-1)
        "webcam_rect_pct": {"x": 0.71, "y": 0.02, "w": 0.27, "h": None},
        "webcam_opacity": 1.0,
    },
    "shorts": {
        # layout por default: los dos videos a pelo, sin blur, apilados
        # (webcam arriba, gameplay abajo) llenando el 100% del canvas entre los dos.
        "width": 1080,
        "height": 1920,
        "pre_roll_sec": 18.0,
        "post_roll_sec": 18.0,
        "merge_gap_sec": 5.0,
        "webcam_rect_pct": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.4},
        "gameplay_rect_pct": {"x": 0.0, "y": 0.4, "w": 1.0, "h": None},
        "webcam_fit": "cover",
        "gameplay_fit": "cover",
        # relleno de fondo con el gameplay duplicado + blureado (opcional, off por default)
        "background_blur": {
            "enabled": False,
            "kernel": 0.045,
            "passes": 2,
        },
    },
    "output_dir": None,
}


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        cfg = _deep_merge(cfg, user_cfg)
    return cfg
