# Editor automático de gameplays

Arma el pre-corte de una sesión de gameplay (gameplay + webcam grabados por
OBS) y deja proyectos de Kdenlive listos para el ajuste fino manual:

- Compone el video largo: gameplay a pantalla completa + webcam en PiP.
- Corta los silencios (usando la pista de audio que elijas como referencia),
  o — opcionalmente — por [clasificación de contenido](#clasificación-de-contenido-v31)
  vía transcripción + LLM en vez de volumen.
- Detecta highlights y los deja como *guides* (marcadores) en el timeline
  del largo — no corta automáticamente por ellos ahí. Por default son picos
  de volumen (RMS); con la clasificación de contenido activada, son las
  ventanas que el LLM marcó como `divertido_interesante`.
- Genera uno o varios shorts verticales (9:16) alrededor de cada highlight
  (con la misma fuente de highlights que el punto anterior).

No renderiza nada. La salida son archivos `.kdenlive` que abrís directo en
Kdenlive para seguir editando.

## Requisitos

- Python 3.9+
- `ffmpeg` / `ffprobe` en el PATH
- `numpy` (`pip install -r requirements.txt`)
- (Opcional, para validar el XML generado sin abrir Kdenlive) `melt`
- (Opcional, para [clasificación de contenido](#clasificación-de-contenido-v31))
  `faster-whisper` (`pip install -r requirements.txt`) + [ollama](https://ollama.com)
  corriendo local con un modelo de texto descargado

## Uso

```bash
# Genera los dos proyectos (largo + shorts)
python3 editor.py both --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
# -> re9_ep12_long.kdenlive
# -> re9_ep12_short_01.kdenlive, re9_ep12_short_02.kdenlive, ...

# Solo el proyecto largo
python3 editor.py long --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4

# Solo los shorts (no depende de haber corrido "long" antes)
python3 editor.py short --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
```

Otras opciones disponibles en los tres subcomandos:

```bash
python3 editor.py both \
  --titulo re9_ep12 \
  --gameplay "/ruta/a/gameplay.mp4" \
  --webcam "/ruta/a/webcam.mp4" \
  --output-dir "/ruta/de/salida" \
  --config mi_config.json \
  --force-analysis
```

- `--output-dir`: carpeta donde se escriben los `.kdenlive` y el cache de
  análisis (por default, la carpeta actual).
- `--config`: config JSON propio (ver [Config](#config) abajo). Si no se
  pasa, se usan los defaults de `config.example.json`.
- `--force-analysis`: ignora el cache de análisis y lo vuelve a calcular
  (útil si cambiaste los umbrales de silencio/highlights en el config).

Al terminar, cada corrida imprime un reporte:

```
--- Reporte ---
Duracion original: 5230.4s  ->  post-corte de silencios: 4680.1s (10.5% recortado)
Highlights detectados: 7
Proyecto largo: re9_ep12_long.kdenlive
Shorts generados: 7
  - re9_ep12_short_01.kdenlive
  - re9_ep12_short_02.kdenlive
  ...
```

### Cache de análisis

El primer subcomando que corras sobre un `--titulo` genera
`<titulo>.analisis.json` (silencios detectados + highlights) en
`--output-dir`. Mientras ese archivo sea más nuevo que `--gameplay` y
`--webcam`, los siguientes comandos con el mismo `--titulo` lo reusan en vez
de volver a correr `ffmpeg`/analizar audio. Por eso podés correr `long` y
después `short` (o al revés) sin repetir el análisis:

```bash
python3 editor.py long  --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
python3 editor.py short --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
# el segundo comando reusa re9_ep12.analisis.json, no vuelve a analizar audio
```

### Varias grabaciones de la misma partida (`--carpeta`)

Si una partida quedó grabada en varios archivos (cortaste y reanudaste OBS,
la sesión se dividió por tamaño, etc.), `--carpeta` los detecta, ordena
cronológicamente por el nombre y los empalma en **un solo proyecto continuo**
— en vez de pasar `--gameplay`/`--webcam` de a un par.

```bash
python3 editor.py both --titulo re9_ep12 --carpeta /ruta/a/la/partida/
```

Los archivos tienen que estar nombrados `<prefijo>-gameplay.<ext>` /
`<prefijo>-webcam.<ext>` (o `<prefijo>-ps5.<ext>` en vez de `-gameplay`, si
la captura es directo de consola) — el `<prefijo>` es lo que define el
orden cronológico (funciona bien con nombres tipo `2026-07-05 14-04-37`, que
ordenan igual como texto que como fecha) y con qué par se junta cada
`-webcam`. Extensiones soportadas: `mp4`, `mkv`, `mov`, `avi`, `ts`, `m2ts`.

Con `--carpeta`, cambia dónde se escribe la salida respecto al modo de un
solo par:

- El proyecto largo y el cache de análisis (`<titulo>__<prefijo>.analisis.json`,
  uno por grabación encontrada) se escriben directo en `--carpeta` — `--output-dir`
  se ignora.
- Los shorts van a una subcarpeta `shorts/` dentro de esa misma carpeta.

Si falta el `-webcam` o el `-gameplay`/`-ps5` de algún archivo, se avisa por
consola y ese archivo se ignora (no frena el resto).

## Config

Copiá `config.example.json`, ajustá lo que necesites y pasalo con
`--config`. Los campos que dejes afuera del JSON quedan con su valor por
default (merge parcial, no hace falta repetir todo el archivo).

```json
{
  "silence": { "reference": "webcam", "min_silence_sec": 0.7 },
  "shorts": { "background_blur": { "enabled": true } }
}
```

Parámetros más relevantes:

| Sección | Campo | Qué controla |
|---|---|---|
| `silence.reference` | `webcam` \| `gameplay` \| `mix` | qué pista de audio se usa para detectar silencios. `mix` corta solo donde *ambas* están en silencio. |
| `silence.min_silence_sec` | segundos | duración mínima de un hueco para considerarlo silencio y cortarlo. |
| `silence.padding_sec` | segundos | margen de audio que se deja pegado a cada corte, para no comerse palabras. |
| `highlights.z_threshold` | desvíos estándar | qué tan por encima del volumen promedio tiene que estar un pico para marcarlo como highlight. Más bajo = más highlights (y más sensible a falsos positivos). |
| `highlights.min_gap_sec` | segundos | separación mínima entre dos highlights. |
| `long.webcam_rect_pct` | `x,y,w,h` (0–1, % del canvas) | posición/tamaño del PiP de la webcam en el video largo. Default: arriba a la derecha. |
| `shorts.webcam_rect_pct` / `shorts.gameplay_rect_pct` | `x,y,w,h` (0–1) | las dos franjas del short: webcam arriba, gameplay abajo. `h: null` en `gameplay_rect_pct` significa "lo que quede hasta el borde". |
| `shorts.webcam_fit` / `shorts.gameplay_fit` | `cover` \| `contain` | `cover` recorta para llenar el recuadro sin bandas negras (default); `contain` muestra el clip entero, con bandas si no matchea el aspect ratio. |
| `shorts.pre_roll_sec` / `post_roll_sec` | segundos | cuánto antes/después de cada highlight arranca/termina el short. |
| `shorts.merge_gap_sec` | segundos | si dos highlights caen a menos de esto, se juntan en un solo short en vez de generar dos. |
| `shorts.background_blur.enabled` | `true`/`false` | variante con el gameplay duplicado y blureado de fondo (ver ejemplo abajo). Apagado por default — el default actual es los dos videos a pelo, sin blur. |

### Layout del video largo

Gameplay a pantalla completa, webcam en PiP arriba a la derecha (tamaño y
posición configurables vía `long.webcam_rect_pct`).

### Layout de los shorts

Por default: los dos videos a pelo, apilados — webcam arriba (40% del alto),
gameplay abajo (60% restante) — sin blur ni efectos.

Si preferís la variante con fondo blureado (gameplay duplicado + blureado
llenando el resto de la pantalla, con webcam y gameplay más chicos
superpuestos), activala así:

```json
{
  "shorts": {
    "background_blur": { "enabled": true }
  }
}
```

## Clasificación de contenido (v3.1)

Reemplaza (opcionalmente) el criterio de corte por volumen/silencio por uno
basado en **contenido**: transcribe ambas pistas (voz del jugador + diálogo
del juego) con Whisper local y le pide a un LLM que clasifique cada ventana
de la sesión en `divertido_interesante`, `relleno` o `neutro`. Los tramos
`relleno` se cortan aunque tengan volumen; los tramos sin transcripción
posible (sin diálogo de ningún lado) se mantienen por default en vez de
cortarse, para no perder cinemáticas silenciosas. Ver
`spec_clasificacion_contenido.md` para el detalle del criterio.

Está apagado por default (`transcription.enabled: false` en la config) — sin
tocar nada, el editor se comporta exactamente igual que antes (silencio +
RMS).

### Setup

```bash
# 1. instalar dependencias python (ya incluye faster-whisper)
pip install -r requirements.txt

# 2. instalar y levantar ollama (una sola vez)
sudo pacman -S ollama        # o el instalador que corresponda a tu distro
ollama serve &                # si no lo tenés corriendo ya como servicio
ollama pull qwen2.5:7b-instruct
```

### Uso: `reclasificar.py`

Es un CLI aparte de `editor.py`. Corre transcripción + clasificación y
genera el `.kdenlive` con esos cortes, para una sesión o para una carpeta
con varias.

```bash
source venv/bin/activate

# una sesion
python3 reclasificar.py \
  --titulo mi_sesion \
  --gameplay "/ruta/a/gameplay.mp4" \
  --webcam   "/ruta/a/webcam.mp4" \
  --output-dir "/ruta/de/salida"
# -> mi_sesion_long.kdenlive, con keep_segments derivados de la clasificacion

# varias grabaciones de la misma partida, empalmadas en un solo proyecto
# (mismo criterio de deteccion de pares que editor.py --carpeta: archivos
# "<prefijo>-gameplay.<ext>" / "<prefijo>-ps5.<ext>" + "<prefijo>-webcam.<ext>",
# ordenados cronologicamente por el prefijo)
python3 reclasificar.py --titulo mi_partida --carpeta "/ruta/a/la/carpeta"
# -> mi_partida_long.kdenlive (todas las sesiones de la carpeta, en orden)
```

Con `--carpeta`, cada sesión se cachea por separado (`<titulo>__<prefijo>.*.json`)
así que si el proceso se corta a mitad de camino, correr el mismo comando de
nuevo retoma desde la última sesión no cacheada en vez de arrancar de cero.

Flags adicionales:

- `--shorts`: además del video largo, genera `<nombre-salida>_shorts.kdenlive`
  (highlights de la clasificación, centrados en el momento real de habla —
  ver `evidencia` en `spec_clasificacion_contenido.md`).
- `--sin-largo`: no (re)genera el proyecto largo. Útil junto con `--shorts`
  si ya editaste el largo a mano en Kdenlive y no querés pisarlo.
- `--categorias`: ver más abajo, corte alternativo sin recalcular nada.

Con una sesión de ~50 min y modelo `medium`, calculá ~6 min de transcripción
+ ~30-35 min de clasificación (el cuello de botella es la llamada a ollama
por ventana, no Whisper) — con `--carpeta` estos tiempos se suman por cada
sesión encontrada. Se cachea en tres archivos junto al `.kdenlive`:

| Cache | Qué guarda | Se invalida cuando... |
|---|---|---|
| `<titulo>.analisis.json` | silencios + highlights por RMS (igual que siempre) | cambia el mtime de `--gameplay`/`--webcam` |
| `<titulo>.transcripcion.json` | segmentos `{inicio, fin, texto, fuente}` de ambas pistas | cambia el mtime de los fuentes, el modelo de whisper, o `max_word_gap_sec` |
| `<titulo>.clasificacion.json` | categoría por ventana | cambia el texto transcripto, el prompt, el modelo o las categorías |

Por eso corridas repetidas con el mismo `--titulo`/`--output-dir` son
prácticamente gratis salvo que fuerces algo explícitamente:

- `--force-analysis`: reprocesa silencio/RMS.
- `--force-transcripcion`: vuelve a correr Whisper (ej. si cambiaste el
  modelo, o si corregiste algo del post-procesado de segmentos).
- `--force-clasificacion`: vuelve a llamar a ollama por ventana (ej. si
  cambiaste el prompt, las categorías, o el modelo de clasificación).

**Generar variantes del corte sin recalcular nada:** una vez que
`<titulo>.clasificacion.json` está generado, podés pedir un corte más
agresivo con `--categorias` (sin pasar ningún `--force-*`, reusa todo lo
cacheado y es casi instantáneo):

```bash
# corte "solo lo mejor": únicamente ventanas divertido_interesante
python3 reclasificar.py \
  --titulo mi_sesion \
  --gameplay "/ruta/a/gameplay.mp4" \
  --webcam   "/ruta/a/webcam.mp4" \
  --output-dir "/ruta/de/salida" \
  --categorias divertido_interesante \
  --nombre-salida mi_sesion_solo_relevante
# -> mi_sesion_solo_relevante_long.kdenlive
```

Sin `--categorias`, el corte por default conserva todo lo que no sea
`relleno` (`divertido_interesante` + `neutro` + tramos sin transcripción).
Lo mismo aplica con `--carpeta` en vez de `--gameplay`/`--webcam` — el corte
"solo lo relevante" de varias sesiones también reusa la cache de cada una.

### Config relevante

| Sección | Campo | Qué controla |
|---|---|---|
| `transcription.enabled` | `true`/`false` | prende v3.1. Con `false`, `reclasificar.py` no tiene sentido usarlo (usá `editor.py` directo). |
| `transcription.model` | `tiny`\|`small`\|`medium`\|`large` | modelo de Whisper. `medium` midió ~16x tiempo real en CPU (Intel Ultra 7, sin GPU) — no hace falta bajar a `small` salvo que el hardware sea más limitado. |
| `transcription.max_word_gap_sec` | segundos | si el hueco entre dos palabras de un mismo segmento de Whisper supera esto, se re-parte en sub-segmentos. Corrige un bug real de Whisper/VAD que a veces agrupa frases separadas por silencios largos (vimos casos de 60-127s) en un solo segmento con el timestamp inflado. |
| `classification.backend` | `ollama` \| `api` | motor de clasificación. Solo `ollama` está implementado hoy. |
| `classification.model` | nombre del modelo ollama | usar un modelo de texto general (`qwen2.5:7b-instruct`), no uno de código. |
| `classification.window_sec` / `overlap_sec` | segundos | tamaño de ventana a clasificar y margen de contexto para no cortar una idea al medio. |
| `classification.categories` | lista | categorías a usar en el prompt; ajustable si no discriminan bien en la práctica. |

## Alcance actual

Implementado: corte de silencios, composición PiP del video largo,
detección de highlights + guides, generación de shorts, config por sesión,
cache de análisis, reporte final.

Todavía no implementado (fase 2 del spec): sincronización automática de
audio/video (asume que gameplay y webcam arrancan en el mismo instante) y
normalización de loudness — hoy eso se sigue ajustando a mano en Kdenlive.
