"""Clasificacion de contenido por ventanas de transcripcion, via LLM local
(ollama) o API. Ver spec_clasificacion_contenido.md, Paso 2.

Flujo: build_windows() arma una ventana por tramo de habla (cortando en los
silencios de cfg["silencio_corte_sec"] en las dos pistas), cada una con su
propio texto (jugador + juego) mas el texto de las ventanas de contexto
antes/despues.
classify() le pega a un LLM por ventana y devuelve la categoria; las
ventanas sin transcripcion no llaman al LLM (quedan como "sin_transcripcion",
que Paso 3 trata como "mantener por defecto").
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from .audio_analysis import drop_short_segments, merge_intervals

PROMPT_VERSION = "v2"

SIN_TRANSCRIPCION = "sin_transcripcion"

_CATEGORY_DESCRIPTIONS = {
    "divertido_interesante": (
        "reaccion genuina CON DESARROLLO (mas de una palabra/exclamacion "
        "suelta), comentario gracioso o momento narrativamente relevante. "
        "Candidato fuerte a conservar / usar como short. Una sola "
        "interjeccion aislada rodeada de silencio NO alcanza por si sola -- "
        "ver 'relleno'."
    ),
    "relleno": (
        "habla sostenida sin contenido relevante (pensar en voz alta "
        "repetitivo, muletillas, silencio de juego narrado sin gracia), O "
        "una interjeccion/comentario aislado y trivial (probar microfono, "
        "'hola hola', una palabra suelta, murmurar sin desarrollo) en un "
        "tramo donde el resto es silencio. Candidato a recortar aunque "
        "tenga volumen."
    ),
    "neutro": (
        "no es un highlight, pero aporta continuidad narrativa (explica que "
        "esta pasando, avanza la historia). Se conserva en el video largo "
        "pero no es candidato a short."
    ),
}


def _merge_sorted(jugador, juego):
    return sorted(jugador + juego, key=lambda s: s["inicio"])


def _format_segments(segments):
    return "\n".join(f"[{s['fuente']}] {s['texto']}" for s in segments)


def _group_fin(group):
    return max(s["fin"] for s in group)


def _group_by_silence(segments, silencio_sec):
    """Agrupa segmentos (ordenados por inicio) en tramos de habla: un tramo
    se cierra cuando el siguiente segmento empieza silencio_sec o mas despues
    del fin maximo del tramo, en cualquiera de las dos pistas."""
    groups = []
    fin_max = None
    for s in segments:
        if groups and s["inicio"] - fin_max < silencio_sec:
            groups[-1].append(s)
            fin_max = max(fin_max, s["fin"])
        else:
            groups.append([s])
            fin_max = s["fin"]
    return groups


def _split_long(group, max_sec):
    """Parte un tramo que dura mas de max_sec en su pausa mas larga, hasta que
    cada parte entre. Un segmento solo queda como esta aunque sea mas largo."""
    if len(group) == 1 or _group_fin(group) - group[0]["inicio"] <= max_sec:
        return [group]
    fin_max = group[0]["fin"]
    best_gap, best_i = None, None
    for i in range(1, len(group)):
        gap = group[i]["inicio"] - fin_max
        if best_gap is None or gap > best_gap:
            best_gap, best_i = gap, i
        fin_max = max(fin_max, group[i]["fin"])
    return _split_long(group[:best_i], max_sec) + _split_long(group[best_i:], max_sec)


def _sin_texto(inicio, fin):
    return {
        "inicio": round(inicio, 2),
        "fin": round(fin, 2),
        "texto": None,
        "evidencia": None,
        "tiene_dialogo_juego": False,
        "duracion_hablada": 0.0,
        "contexto_antes": "",
        "contexto_despues": "",
        "silencio_antes": 0.0,
        "silencio_despues": 0.0,
    }


def build_windows(transcripcion, cfg, duration=None):
    """Devuelve una lista de ventanas ordenadas por inicio: {inicio, fin,
    texto, evidencia, tiene_dialogo_juego, duracion_hablada, contexto_antes,
    contexto_despues, silencio_antes, silencio_despues}.

    Cada ventana con texto es un tramo de habla: empieza en su primera frase
    y termina en la ultima, y se corta cuando hay cfg["silencio_corte_sec"]
    sin dialogo en las dos pistas a la vez. Un tramo de mas de
    cfg["max_ventana_sec"] se parte en su pausa mas larga. Cada segmento cae
    en exactamente una ventana. El contexto son las cfg["context_windows"]
    ventanas con texto mas cercanas antes/despues, y silencio_antes/despues
    los segundos sin dialogo hasta la ventana con texto vecina (o el borde
    del video): con ventanas que arrancan y terminan en el habla, es lo que
    distingue una frase suelta de una charla.

    Los huecos entre ventanas con texto (y desde 0 / hasta `duration`) son
    ventanas con texto None (quedan "sin_transcripcion", no se clasifican).
    `duration` deberia ser la duracion real del video, para que el tramo
    final sin dialogo tambien quede cubierto."""
    all_segments = _merge_sorted(transcripcion["jugador"], transcripcion["juego"])
    if duration is None:
        if not all_segments:
            return []
        duration = max(s["fin"] for s in all_segments)
    context_n = cfg["context_windows"]

    groups = [
        part
        for group in _group_by_silence(all_segments, cfg["silencio_corte_sec"])
        for part in _split_long(group, cfg["max_ventana_sec"])
    ]
    texts = [_format_segments(g) for g in groups]
    bounds = [(g[0]["inicio"], _group_fin(g)) for g in groups]

    windows = []
    cursor = 0.0
    for i, (group, (start, end)) in enumerate(zip(groups, bounds)):
        if start > cursor:
            windows.append(_sin_texto(cursor, start))
        anterior_fin = bounds[i - 1][1] if i > 0 else 0.0
        siguiente_inicio = bounds[i + 1][0] if i + 1 < len(bounds) else duration
        windows.append({
            "inicio": round(start, 2),
            "fin": round(end, 2),
            "texto": texts[i],
            "evidencia": (start, end),
            "tiene_dialogo_juego": any(s["fuente"] == "juego" for s in group),
            "duracion_hablada": round(sum(s["fin"] - s["inicio"] for s in group), 1),
            "contexto_antes": "\n---\n".join(texts[max(0, i - context_n):i]),
            "contexto_despues": "\n---\n".join(texts[i + 1:i + 1 + context_n]),
            "silencio_antes": round(max(0.0, start - anterior_fin), 1),
            "silencio_despues": round(max(0.0, siguiente_inicio - end), 1),
        })
        cursor = max(cursor, end)
    if duration > cursor:
        windows.append(_sin_texto(cursor, duration))
    return windows


def _build_prompt(window, categories):
    cat_lines = "\n".join(f"- {c}: {_CATEGORY_DESCRIPTIONS.get(c, '')}" for c in categories)
    parts = [
        "Sos un editor de video clasificando un tramo de una sesion de gameplay grabada.",
        "Las lineas [jugador] son lo que dice quien juega (voz de webcam); "
        "las lineas [juego] son dialogo/audio del propio videojuego (cinematicas, NPCs).",
        f"Antes de este tramo hubo {window['silencio_antes']:.0f}s sin dialogo y despues "
        f"{window['silencio_despues']:.0f}s. Tenelo en cuenta: una frase corta con mucho silencio "
        "antes y despues no es lo mismo que la misma frase en medio de una charla.",
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


_RETRYABLE_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)


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
    # timeout generoso: si ollama descargo el modelo de memoria por
    # inactividad (default de ollama: ~5 min sin uso), la primera llamada
    # despues de eso tiene que recargarlo de disco antes de responder.
    timeout_sec = cfg.get("timeout_sec", 180)
    retries = cfg.get("retries", 3)
    backoff_sec = cfg.get("retry_backoff_sec", 10)

    last_error = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            f"{host}/api/chat", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except _RETRYABLE_ERRORS as e:
            last_error = e
            if attempt < retries:
                print(f"    [ollama] intento {attempt}/{retries} fallo ({e}), "
                      f"reintentando en {backoff_sec}s...")
                time.sleep(backoff_sec)
    else:
        raise RuntimeError(
            f"No se pudo contactar a ollama en {host} tras {retries} intentos (¿esta corriendo "
            f"'ollama serve' y el modelo '{cfg['model']}' descargado con 'ollama pull {cfg['model']}'?): "
            f"{last_error}"
        ) from last_error

    content = body["message"]["content"]
    parsed = json.loads(content)
    categoria = parsed.get("categoria")
    if categoria not in categories:
        categoria = categories[-1]
    return {"categoria": categoria, "razon": parsed.get("razon", "")}


_JEV_URL = "https://api.typesafe.ai/v1/systemone"

_JEV_INSTRUCTIONS = (
    "Sos un editor de video clasificando un tramo de una sesion de gameplay grabada. "
    "Las lineas [jugador] son lo que dice quien juega (voz de webcam); las lineas [juego] "
    "son dialogo/audio del propio videojuego (cinematicas, NPCs). Clasifica solo "
    "`tramo_a_clasificar`; los contextos son los tramos vecinos y NO son lo que hay que "
    "clasificar. Tene en cuenta `silencio_antes_seg` y `silencio_despues_seg`: una frase "
    "corta con mucho silencio antes y despues no es lo mismo que la misma frase en medio "
    "de una charla."
)


def _jev_state(window):
    state = {
        "duracion_tramo_seg": round(window["fin"] - window["inicio"], 1),
        "habla_real_seg": window.get("duracion_hablada", 0.0),
        "silencio_antes_seg": window["silencio_antes"],
        "silencio_despues_seg": window["silencio_despues"],
    }
    if window["contexto_antes"]:
        state["contexto_antes"] = window["contexto_antes"]
    state["tramo_a_clasificar"] = window["texto"]
    if window["contexto_despues"]:
        state["contexto_despues"] = window["contexto_despues"]
    return state


def _classify_jev(window, categories, cfg):
    api_key = os.environ.get("JEV_API_KEY")
    if not api_key:
        raise RuntimeError("Falta JEV_API_KEY en el .env del backend")
    payload = json.dumps({
        "model": cfg["model"],
        "state": _jev_state(window),
        "questions": {
            "categoria": {
                "type": "choice",
                "instructions": _JEV_INSTRUCTIONS,
                "criteria": {c: _CATEGORY_DESCRIPTIONS[c] for c in categories},
            },
        },
    }).encode("utf-8")
    timeout_sec = cfg.get("timeout_sec", 180)
    retries = cfg.get("retries", 3)
    backoff_sec = cfg.get("retry_backoff_sec", 10)

    last_error = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            _JEV_URL, data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        # HTTPError hereda de URLError: va antes para no reintentar un 401/422,
        # que van a fallar igual en cada intento.
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise RuntimeError(
                    f"JEV respondio {e.code}: {e.read().decode('utf-8', 'replace')}"
                ) from e
            last_error = e
        except _RETRYABLE_ERRORS as e:
            last_error = e
        if attempt < retries:
            print(f"    [jev] intento {attempt}/{retries} fallo ({last_error}), "
                  f"reintentando en {backoff_sec}s...")
            time.sleep(backoff_sec)
    else:
        raise RuntimeError(f"No se pudo contactar a JEV tras {retries} intentos: {last_error}") from last_error

    answer = body["answers"]["categoria"]
    return {"categoria": answer["choice"], "razon": f"confianza {answer['confidence']:.2f}"}


_BACKENDS = {
    "ollama": _classify_ollama,
    "jev": _classify_jev,
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
    video: si cambias el prompt no hace falta re-transcribir).

    El progreso se guarda ventana por ventana (no recien al final): si la
    corrida se corta a mitad de camino (timeout de ollama, corte de luz,
    etc.), la proxima llamada con el mismo `titulo`/`output_dir` retoma
    desde la ultima ventana clasificada en vez de perder todo lo ya
    pagado en esta sesion."""
    windows = build_windows(transcripcion, cfg, duration=duration)
    sig = _signature(windows, cfg)
    cache_file = _cache_path(titulo, output_dir)

    classified = []
    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig:
                classified = cached["windows"]
        except (json.JSONDecodeError, OSError):
            pass

    if len(classified) < len(windows):
        if classified:
            print(f"    (retomando clasificacion: {len(classified)}/{len(windows)} "
                  f"ventanas ya cacheadas)")
        os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
        pendientes = windows[len(classified):]
        for i, window in enumerate(pendientes, start=len(classified) + 1):
            print(f"    ventana {i}/{len(windows)}...", end="\r")
            result = classify_window(window, cfg)
            classified.append({
                "inicio": window["inicio"],
                "fin": window["fin"],
                "categoria": result["categoria"],
                "razon": result.get("razon", ""),
                "texto": window["texto"],
            })
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump({"signature": sig, "windows": classified}, fh, indent=2, ensure_ascii=False)
        print()

    # evidencia y tiene_dialogo_juego se derivan de la transcripcion
    # (build_windows), no del LLM -- se recalculan siempre, sin costo, asi
    # una cache vieja de antes de este fix tambien se beneficia sin tener
    # que re-clasificar con ollama.
    for w, window in zip(classified, windows):
        w["evidencia"] = window["evidencia"]
        w["tiene_dialogo_juego"] = window["tiene_dialogo_juego"]

    return classified


def keep_segments_for_categories(classified_windows, duration, categories, min_keep_sec=0.3,
                                  padding_sec=8.0, proteger_silencio=False):
    """Paso 3: deriva keep_segments (mismo formato que audio_analysis, lista
    de (inicio, fin)) a partir de la clasificacion de contenido. Conserva
    solo el rango real de evidencia (± padding_sec) de las ventanas cuya
    categoria este en `categories` -- una ventana con 2s de reaccion real
    puede tener 43s de silencio alrededor, y no tiene sentido arrastrar eso.
    Ademas, sin importar `categories`:
    - SIN_TRANSCRIPCION (silencio total, ni jugador ni juego dicen nada) ->
      se corta por default, igual que relleno (en la practica protege mas
      silencio aburrido de exploracion que cinematicas). Si `proteger_silencio`
      es True, se mantiene siempre, ventana completa, para no perder
      cinematicas silenciosas -- a criterio de quien llama, ver
      spec_clasificacion_contenido.md.
    - Ventana con dialogo real del juego (tiene_dialogo_juego) -> se
      mantiene siempre, recortada a su evidencia igual que las demas. El
      dialogo del juego no es contenido del jugador que haya que filtrar
      por "interesante vs relleno": es material que el usuario quiere
      revisar el mismo en la edicion.
    El silencio de audio (audio_analysis.detect_silence) no participa aca:
    en v3.1 la clasificacion de contenido es la señal primaria."""
    intervals = []
    for w in classified_windows:
        if w["categoria"] == SIN_TRANSCRIPCION:
            if proteger_silencio:
                intervals.append((w["inicio"], w["fin"]))
            continue
        if w["categoria"] not in categories and not w.get("tiene_dialogo_juego"):
            continue
        evidencia = w.get("evidencia")
        if evidencia:
            s = max(0.0, evidencia[0] - padding_sec)
            e = min(duration, evidencia[1] + padding_sec)
        else:
            s, e = w["inicio"], w["fin"]
        intervals.append((s, e))
    keep = merge_intervals(intervals)
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


