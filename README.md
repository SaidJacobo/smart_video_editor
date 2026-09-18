# Editor automático de gameplays

Arma el pre-corte de una sesión de gameplay (gameplay + webcam grabados por
OBS) y deja proyectos de Kdenlive listos para el ajuste fino manual:

- Compone el video largo: gameplay a pantalla completa + webcam en PiP.
- Corta los silencios (usando la pista de audio que elijas como referencia).
- Detecta highlights por picos de volumen y los deja como *guides*
  (marcadores) en el timeline — no corta automáticamente por ellos.
- Genera uno o varios shorts verticales (9:16) alrededor de cada highlight.

No renderiza nada. La salida son archivos `.kdenlive` que abrís directo en
Kdenlive para seguir editando.

## Requisitos

- Python 3.9+
- `ffmpeg` / `ffprobe` en el PATH
- `numpy` (`pip install -r requirements.txt`)
- (Opcional, para validar el XML generado sin abrir Kdenlive) `melt`

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

## Alcance actual

Implementado: corte de silencios, composición PiP del video largo,
detección de highlights + guides, generación de shorts, config por sesión,
cache de análisis, reporte final.

Todavía no implementado (fase 2 del spec): sincronización automática de
audio/video (asume que gameplay y webcam arrancan en el mismo instante) y
normalización de loudness — hoy eso se sigue ajustando a mano en Kdenlive.
