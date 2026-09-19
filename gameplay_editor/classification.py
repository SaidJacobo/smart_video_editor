"""Clasificacion de contenido por ventanas de transcripcion, via LLM local
(ollama) o API. Ver spec_clasificacion_contenido.md, Paso 2.

Flujo: build_windows() arma ventanas no solapadas de cfg["window_sec"], cada
una con su propio texto (jugador + juego, extendido con cfg["overlap_sec"]
de margen para no cortar una idea al medio) mas el texto de las ventanas de
contexto antes/despues. classify() le pega a un LLM por ventana y devuelve
la categoria; las ventanas sin transcripcion no llaman al LLM (quedan como
"sin_transcripcion", que Paso 3 trata como "mantener por defecto").
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

from .audio_analysis import drop_short_segments, merge_intervals

PROMPT_VERSION = "v1"

SIN_TRANSCRIPCION = "sin_transcripcion"

_CATEGORY_DESCRIPTIONS = {
    "divertido_interesante": (
        "reaccion genuina, comentario gracioso o momento narrativamente "
        "relevante. Candidato fuerte a conservar / usar como short."
    ),
    "relleno": (
        "habla sostenida sin contenido relevante: pensar en voz alta "
        "repetitivo, muletillas, silencio de juego narrado sin gracia. "
        "Candidato a recortar aunque tenga volumen."
    ),
    "neutro": (
        "no es un highlight, pero aporta continuidad narrativa (explica que "
        "esta pasando, avanza la historia). Se conserva en el video largo "
        "pero no es candidato a short."
    ),
}


def _merge_sorted(jugador, juego):
    return sorted(jugador + juego, key=lambda s: s["inicio"])


def _window_bounds(duration, window_sec):
    bounds = []
    t = 0.0
    while t < duration:
        bounds.append((t, min(t + window_sec, duration)))
        t += window_sec
    return bounds


def _segments_in_range(segments, start, end):
    return [s for s in segments if s["inicio"] < end and s["fin"] > start]


def _format_segments(segments):
    return "\n".join(f"[{s['fuente']}] {s['texto']}" for s in segments)


def build_windows(transcripcion, cfg, duration=None):
    """Devuelve una lista de ventanas: {inicio, fin, texto, contexto_antes,
    contexto_despues}. `texto` es None si la ventana no tiene transcripcion
    (ni jugador ni juego dijeron nada ahi).

    `duration` deberia ser la duracion real del video (de audio_analysis),
    no solo hasta el ultimo segmento transcripto: un tramo final sin dialogo
    tambien tiene que quedar cubierto por una ventana "sin_transcripcion"
    para que Paso 3 lo mantenga por defecto en vez de ignorarlo."""
    all_segments = _merge_sorted(transcripcion["jugador"], transcripcion["juego"])
    if duration is None:
        if not all_segments:
            return []
        duration = max(s["fin"] for s in all_segments)
    window_sec = cfg["window_sec"]
    overlap_sec = cfg["overlap_sec"]
    context_n = cfg["context_windows"]

    bounds = _window_bounds(duration, window_sec)
    per_window_text = []
    per_window_evidencia = []
    for start, end in bounds:
        extended = _segments_in_range(all_segments, start - overlap_sec, end + overlap_sec)
        if extended:
            per_window_text.append(_format_segments(extended))
            # rango de tiempo real de las frases que aportaron texto a esta
            # ventana -- puede caer fuera de [start, end) por el margen de
            # overlap_sec (para no cortar una idea a la mitad al armar el
            # prompt). Sirve para ubicar un highlight en el momento real en
            # que se hablo, en vez del punto medio arbitrario de la ventana.
            per_window_evidencia.append((
                min(s["inicio"] for s in extended),
                max(s["fin"] for s in extended),
            ))
        else:
            per_window_text.append(None)
            per_window_evidencia.append(None)

    windows = []
    for i, (start, end) in enumerate(bounds):
        ctx_before = [t for t in per_window_text[max(0, i - context_n):i] if t]
        ctx_after = [t for t in per_window_text[i + 1:i + 1 + context_n] if t]
        windows.append({
            "inicio": round(start, 2),
            "fin": round(end, 2),
            "texto": per_window_text[i],
            "evidencia": per_window_evidencia[i],
            "contexto_antes": "\n---\n".join(ctx_before),
            "contexto_despues": "\n---\n".join(ctx_after),
        })
    return windows


def _build_prompt(window, categories):
    cat_lines = "\n".join(f"- {c}: {_CATEGORY_DESCRIPTIONS.get(c, '')}" for c in categories)
    parts = [
        "Sos un editor de video clasificando un tramo de una sesion de gameplay grabada.",
        "Las lineas [jugador] son lo que dice quien juega (voz de webcam); "
        "las lineas [juego] son dialogo/audio del propio videojuego (cinematicas, NPCs).",
        "",
        "Categorias posibles:",
        cat_lines,
        "",
    ]
    if window["contexto_antes"]:
        parts += ["Contexto (tramo anterior, NO es lo que hay que clasificar):", window["contexto_antes"], ""]
    parts += ["Tramo a clasificar:", window["texto"], ""]
    if window["contexto_despues"]:
        parts += ["Contexto (tramo siguiente, NO es lo que hay que clasificar):", window["contexto_despues"], ""]
    parts += [
        "Devolve SOLO un JSON con esta forma exacta, sin texto adicional:",
        '{"categoria": "<una de: ' + ", ".join(categories) + '>", "razon": "<una frase breve>"}',
    ]
    return "\n".join(parts)


def _classify_ollama(window, categories, cfg):
    prompt = _build_prompt(window, categories)
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0},
    }).encode("utf-8")
    host = cfg.get("ollama_host", "http://localhost:11434")
    req = urllib.request.Request(
        f"{host}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout_sec", 60)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"No se pudo contactar a ollama en {host} (¿esta corriendo 'ollama serve' "
            f"y el modelo '{cfg['model']}' descargado con 'ollama pull {cfg['model']}'?): {e}"
        ) from e

    content = body["message"]["content"]
    parsed = json.loads(content)
    categoria = parsed.get("categoria")
    if categoria not in categories:
        categoria = categories[-1]
    return {"categoria": categoria, "razon": parsed.get("razon", "")}


_BACKENDS = {
    "ollama": _classify_ollama,
}


def classify_window(window, cfg):
    if window["texto"] is None:
        return {"categoria": SIN_TRANSCRIPCION, "razon": "sin transcripcion en este tramo"}
    backend = _BACKENDS.get(cfg["backend"])
    if backend is None:
        raise NotImplementedError(
            f"Backend de clasificacion '{cfg['backend']}' no implementado todavia."
        )
    return backend(window, cfg["categories"], cfg)


def _cache_path(titulo, output_dir):
    return os.path.join(output_dir or ".", f"{titulo}.clasificacion.json")


def _signature(windows, cfg):
    texts = "\x00".join(w["texto"] or "" for w in windows)
    digest = hashlib.sha256(texts.encode("utf-8")).hexdigest()
    return {
        "prompt_version": PROMPT_VERSION,
        "texto_hash": digest,
        "backend": cfg["backend"],
        "model": cfg["model"],
        "categories": cfg["categories"],
    }


def classify(transcripcion, cfg, titulo, output_dir=None, force=False, duration=None):
    """cfg es cfg["classification"] (ver config.py). Devuelve una lista de
    ventanas clasificadas: {inicio, fin, categoria, razon, texto, evidencia},
    cacheada en <titulo>.clasificacion.json e invalidada por el contenido
    transcripto + version del prompt + modelo/categorias (no por mtime de
    video: si cambias el prompt no hace falta re-transcribir)."""
    windows = build_windows(transcripcion, cfg, duration=duration)
    sig = _signature(windows, cfg)

    classified = None
    cache_file = _cache_path(titulo, output_dir)
    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig:
                classified = cached["windows"]
        except (json.JSONDecodeError, OSError):
            pass

    if classified is None:
        classified = []
        for window in windows:
            result = classify_window(window, cfg)
            classified.append({
                "inicio": window["inicio"],
                "fin": window["fin"],
                "categoria": result["categoria"],
                "razon": result.get("razon", ""),
                "texto": window["texto"],
            })
        os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump({"signature": sig, "windows": classified}, fh, indent=2, ensure_ascii=False)

    # evidencia se deriva de la transcripcion (build_windows), no del LLM --
    # se recalcula siempre, sin costo, asi una cache vieja de antes de este
    # fix tambien se beneficia sin tener que re-clasificar con ollama.
    for w, window in zip(classified, windows):
        w["evidencia"] = window["evidencia"]

    return classified


def merge_keep_segments(classified_windows, duration, min_keep_sec=0.3):
    """Paso 3: deriva keep_segments (mismo formato que audio_analysis, lista
    de (inicio, fin)) a partir de la clasificacion de contenido, invirtiendo
    la heuristica de v1:
    - SIN_TRANSCRIPCION (no se pudo evaluar contenido ahi) -> se mantiene
      por defecto, para no perder cinematicas silenciosas.
    - "relleno" -> se corta, aunque haya volumen/voz.
    - cualquier otra categoria (p.ej. divertido_interesante, neutro) -> se
      mantiene.
    El silencio de audio (audio_analysis.detect_silence) no participa aca:
    en v3.1 la clasificacion de contenido es la señal primaria y el silencio
    queda relegado a los tramos SIN_TRANSCRIPCION, que ya se mantienen por
    esta misma regla."""
    keep = merge_intervals([
        (w["inicio"], w["fin"]) for w in classified_windows if w["categoria"] != "relleno"
    ])
    keep = drop_short_segments(keep, min_keep_sec)
    if not keep:
        keep = [(0.0, duration)]
    return keep


def keep_segments_for_categories(classified_windows, duration, categories, min_keep_sec=0.3):
    """Como merge_keep_segments pero mas restrictivo: solo conserva ventanas
    cuya categoria este en `categories` (p.ej. solo divertido_interesante),
    en vez de conservar todo lo que no sea relleno. Util para un corte
    "solo lo mas relevante" sin re-procesar transcripcion/clasificacion."""
    keep = merge_intervals([
        (w["inicio"], w["fin"]) for w in classified_windows if w["categoria"] in categories
    ])
    keep = drop_short_segments(keep, min_keep_sec)
    if not keep:
        keep = [(0.0, duration)]
    return keep


def classification_highlights(classified_windows, categoria="divertido_interesante"):
    """Highlights candidatos para shorts a partir de la clasificacion, mismo
    shape {time, score} que audio_analysis.detect_highlights para que
    project_short.highlight_windows() los consuma sin cambios.

    El "time" se centra en la evidencia real (rango de tiempo de las frases
    que se transcribieron para esa ventana), no en el punto medio de la
    ventana de 45s -- si el contenido que disparo la clasificacion esta
    pegado a un borde (o entro por el margen de overlap_sec), el punto medio
    de la ventana puede caer en una zona sin voz y el short queda centrado
    en el vacio. Si por algun motivo no hay evidencia (no deberia pasar,
    ya que sin texto ni siquiera se llama al LLM), cae al punto medio."""
    highlights = []
    for w in classified_windows:
        if w["categoria"] != categoria:
            continue
        evidencia = w.get("evidencia")
        if evidencia:
            time = round((evidencia[0] + evidencia[1]) / 2, 2)
        else:
            time = round((w["inicio"] + w["fin"]) / 2, 2)
        highlights.append({"time": time, "score": 1.0})
    return highlights


def apply_to_analysis(analysis, gameplay_path, webcam_path, cfg, titulo, output_dir=None, force=False):
    """Punto de integracion de v3.1 con el pipeline existente (editor.py).
    Corre transcripcion + clasificacion y devuelve una COPIA de `analysis`
    (el dict que produce audio_analysis.analyze) con keep_segments/highlights
    reemplazados por los derivados de contenido (Paso 3). Los originales por
    RMS/silencio quedan conservados bajo *_silencio/*_rms para referencia y
    debugging -- no se pierden, solo dejan de ser los que usa el resto del
    pipeline (project_long.py, project_short.py) para armar el proyecto."""
    from . import transcription as _transcription

    transcripcion = _transcription.transcribe(
        gameplay_path, webcam_path, cfg["transcription"], titulo, output_dir=output_dir, force=force,
    )
    classified = classify(
        transcripcion, cfg["classification"], titulo, output_dir=output_dir, force=force,
        duration=analysis["duration"],
    )
    keep_segments = merge_keep_segments(classified, analysis["duration"])
    highlights = classification_highlights(classified)

    result = dict(analysis)
    result["keep_segments_silencio"] = analysis["keep_segments"]
    result["highlights_rms"] = analysis["highlights"]
    result["keep_segments"] = keep_segments
    result["highlights"] = highlights
    result["clasificacion"] = classified
    return result
