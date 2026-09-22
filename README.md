# Editor automático de gameplays

Arma el pre-corte de una sesión de gameplay (gameplay + webcam) y deja
proyectos de Kdenlive listos para el ajuste fino manual:

- Transcribe ambas pistas (voz del jugador + diálogo del juego) con Whisper
  local y le pide a un LLM que clasifique el contenido de cada tramo de la
  sesión en `divertido_interesante`, `relleno` o `neutro`. Los tramos
  `relleno` se descartan aunque tengan volumen; los tramos sin transcripción
  posible (sin diálogo de ningún lado) se conservan por default, para no
  perder cinemáticas silenciosas. Ver `spec_clasificacion_contenido.md` para
  el detalle del criterio.
- Compone el video largo con lo que sobrevive a ese filtro: gameplay a
  pantalla completa + webcam en PiP.
- Genera shorts verticales (9:16) alrededor de cada tramo clasificado como
  `divertido_interesante`, centrados en el momento real de habla (no en el
  punto medio de la ventana clasificada).

No renderiza nada. La salida son archivos `.kdenlive` que abrís directo en
Kdenlive para seguir editando.

## Requisitos

- Python 3.9+
- `ffmpeg` / `ffprobe` en el PATH
- `pip install -r requirements.txt` (`numpy` + `faster-whisper`)
- [ollama](https://ollama.com) corriendo local, con un modelo de texto
  descargado
- (Opcional, para validar el XML generado sin abrir Kdenlive) `melt`

### Setup de ollama (una sola vez)

```bash
sudo pacman -S ollama        # o el instalador que corresponda a tu distro
ollama serve &                # si no lo tenés corriendo ya como servicio
ollama pull qwen2.5:7b-instruct
```

Usá un modelo de texto general (`qwen2.5:7b-instruct`, `llama3.1:8b`, etc.),
no uno de código.

## Uso

```bash
source venv/bin/activate

# una sesion (un par gameplay+webcam)
python3 editor.py \
  --titulo mi_sesion \
  --gameplay "/ruta/a/gameplay.mp4" \
  --webcam   "/ruta/a/webcam.mp4" \
  --output-dir "/ruta/de/salida"
```

Esto genera siempre **dos archivos**: `mi_sesion_long.kdenlive` (el video
completo con los cortes ya aplicados) y `mi_sesion_shorts.kdenlive` (una
secuencia por cada momento `divertido_interesante` detectado).

Si alguno de los dos ya existe en `--output-dir`, el script **no lo pisa**:
avisa cuál existe y termina sin procesar nada. Para regenerar, borrá el
archivo a mano primero.

### Varias grabaciones de la misma partida (`--folder`)

Si una partida quedó grabada en varios archivos (cortaste y reanudaste OBS,
la sesión se dividió por tamaño, etc.), `--folder` los detecta, ordena
cronológicamente por el nombre y los empalma en un solo proyecto continuo:

```bash
python3 editor.py --titulo mi_partida --folder "/ruta/a/la/carpeta"
# -> mi_partida_long.kdenlive / mi_partida_shorts.kdenlive, todas las
#    sesiones de la carpeta, en orden
```

- No se combina con `--gameplay`/`--webcam`.
- `--output-dir` es opcional con `--folder`: si no se pasa, la salida (y el
  cache) se escribe directo en esa misma carpeta.
- Convención de nombres: `<prefijo>-gameplay.<ext>` / `<prefijo>-webcam.<ext>`
  (o `<prefijo>-ps5.<ext>` en vez de `-gameplay`, si la captura es directo
  de consola). El `<prefijo>` define el orden cronológico (funciona bien con
  nombres tipo `2026-07-05 14-04-37`) y con qué `-webcam` se empareja.
  Extensiones soportadas: `mp4`, `mkv`, `mov`, `avi`, `ts`, `m2ts`. Si falta
  el archivo de un lado del par, se avisa por consola y esa grabación se
  ignora (no frena el resto).
- Cada sesión se cachea por separado (`<titulo>__<prefijo>.*.json`), así que
  si el proceso se corta a mitad de camino, correr el mismo comando de nuevo
  retoma desde la última sesión no cacheada en vez de arrancar de cero.

### Cache

Se cachean dos archivos junto al `.kdenlive` (o uno por sesión, con `--folder`):

| Cache | Qué guarda | Se invalida cuando... |
|---|---|---|
| `<titulo>.transcripcion.json` | segmentos `{inicio, fin, texto, fuente}` de ambas pistas | cambia el mtime de `--gameplay`/`--webcam`, el modelo de whisper, o `max_word_gap_sec` |
| `<titulo>.clasificacion.json` | categoría por ventana + evidencia (timestamps reales) | cambia el texto transcripto, el prompt, el modelo o las categorías, o se usa `--force-clasification` |

Por eso correr el mismo comando dos veces (con el mismo `--titulo`/carpeta)
es prácticamente gratis la segunda vez, salvo que:

- `--force-clasification`: ignora el cache de clasificación y vuelve a
  llamar a ollama por ventana (útil si cambiaste el modelo, el prompt o las
  categorías en `config.py`). La transcripción sigue invalidándose sola por
  mtime de los videos — para forzarla sin tocar los videos, borrá el
  `.transcripcion.json` a mano.

### Tiempos esperados

Con modelo `medium` (CPU, sin GPU dedicada): transcripción ronda 15-17x
tiempo real (medido: ~6 min para una sesión de 52 min). El cuello de botella
real es la clasificación — una llamada a ollama por ventana de 45s, medida
en ~30s cada una. Para una sesión de ~50 min, calculá ~30-35 min de
clasificación. Con `--folder`, estos tiempos se suman por cada sesión
encontrada.

## Config

No hay flag de `--config` — los parámetros se ajustan editando
`gameplay_editor/config.py` directamente (sección `DEFAULT_CONFIG`).

| Sección | Campo | Qué controla |
|---|---|---|
| `transcription.model` | `tiny`\|`small`\|`medium`\|`large` | modelo de Whisper. `medium` midió ~16x tiempo real en CPU (Intel Ultra 7, sin GPU) — no hace falta bajar a `small` salvo que el hardware sea más limitado. |
| `transcription.max_word_gap_sec` | segundos | si el hueco entre dos palabras de un mismo segmento de Whisper supera esto, se re-parte en sub-segmentos. Corrige un bug real de Whisper/VAD que a veces agrupa frases separadas por silencios largos (vimos casos de 60-127s) en un solo segmento con el timestamp inflado. |
| `classification.backend` | `ollama` \| `api` | motor de clasificación. Solo `ollama` está implementado hoy. |
| `classification.model` | nombre del modelo ollama | usar un modelo de texto general, no uno de código. |
| `classification.window_sec` / `overlap_sec` | segundos | tamaño de ventana a clasificar y margen de contexto para no cortar una idea al medio. |
| `classification.categories` | lista | categorías a usar en el prompt; ajustable si no discriminan bien en la práctica. |
| `long.webcam_rect_pct` | `x,y,w,h` (0–1, % del canvas) | posición/tamaño del PiP de la webcam en el video largo. Default: arriba a la derecha. |
| `shorts.webcam_rect_pct` / `shorts.gameplay_rect_pct` | `x,y,w,h` (0–1) | las dos franjas del short: webcam arriba, gameplay abajo. `h: null` en `gameplay_rect_pct` significa "lo que quede hasta el borde". |
| `shorts.webcam_fit` / `shorts.gameplay_fit` | `cover` \| `contain` | `cover` recorta para llenar el recuadro sin bandas negras (default); `contain` muestra el clip entero, con bandas si no matchea el aspect ratio. |
| `shorts.pre_roll_sec` / `post_roll_sec` | segundos | cuánto antes/después de cada highlight arranca/termina el short. |
| `shorts.merge_gap_sec` | segundos | si dos highlights caen a menos de esto, se juntan en un solo short en vez de generar dos. |
| `shorts.background_blur.enabled` | `true`/`false` | variante con el gameplay duplicado y blureado de fondo. Apagado por default — el default actual es los dos videos a pelo, sin blur. |

### Layout del video largo

Gameplay a pantalla completa, webcam en PiP arriba a la derecha (tamaño y
posición configurables vía `long.webcam_rect_pct`).

### Layout de los shorts

Por default: los dos videos a pelo, apilados — webcam arriba (40% del alto),
gameplay abajo (60% restante) — sin blur ni efectos. La variante con fondo
blureado (gameplay duplicado + blureado llenando el resto de la pantalla) se
activa con `shorts.background_blur.enabled = true` en `config.py`.

## Código legado: corte por silencio/RMS (v1)

Antes de la clasificación por contenido, el corte se decidía por silencio de
audio (`ffmpeg silencedetect`) + picos de volumen (RMS) para los highlights.
Ese código sigue en el repo (`gameplay_editor/audio_analysis.py`, función
`analyze()`) pero **`editor.py` ya no lo usa** — se dejó intacto, desvinculado
del flujo principal, por si sirve de base para un fork orientado a otro caso
de uso. No hay forma de activarlo desde la CLI actual.

## Alcance actual

Implementado: transcripción + clasificación de contenido por LLM,
composición PiP del video largo, generación de shorts, cache en dos capas,
detección de varias grabaciones de una misma partida.

Todavía no implementado: curaduría activa por escenario (Paso 4 del spec —
hoy el criterio de corte es por ventana de 45s independiente, no por
situación/escenario agrupado), contexto visual (v3.2), sincronización
automática de audio/video (asume que gameplay y webcam arrancan en el mismo
instante) y normalización de loudness — esto último se sigue ajustando a
mano en Kdenlive. Ver `spec_clasificacion_contenido.md` para el detalle y el
estado de cada punto.
