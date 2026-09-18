# Spec: Editor automático de gameplays (pre-corte + estructura + shorts)

## Contexto
Canal de gameplays (Jaco). Cada sesión de juego genera, vía OBS:
- Video de gameplay + su pista de audio
- Video de webcam + su pista de audio
- Ambos tracks grabados en simultáneo, deberían estar sincronizados (o casi)

El cuello de botella actual: editar 2-3h de material crudo a mano lleva días,
y además hay que re-buscar momentos específicos por separado para armar shorts
(otro aspect ratio, otra disposición de cámara), duplicando el trabajo.

Objetivo del script: automatizar el armado grueso de la edición y dejar un
proyecto de Kdenlive (.mlt/.kdenlive) listo para el ajuste fino manual —
nunca reemplazar el criterio editorial final.

---

## v1 — Alcance base (lo pedido originalmente)

**Inputs:**
- Video gameplay + audio (archivo de OBS)
- Video webcam + audio (archivo de OBS)

**Procesamiento:**
1. Componer el frame final: gameplay en horizontal a pantalla completa, webcam
   en un cuadro superpuesto arriba a la derecha (tamaño y posición configurables).
2. Detectar y recortar silencios (usando la pista de audio del gameplay y/o
   la de la webcam — definir cuál es la referencia, o combinar ambas).
3. Generar un archivo `.mlt` de Kdenlive con:
   - Ambos clips de video posicionados correctamente (composición picture-in-picture)
   - Los cortes de silencio ya aplicados en el playlist
   - Sin renderizar nada — el usuario abre el proyecto y ajusta a mano

**Output:** un `.mlt` (o `.kdenlive`) abrible directo, con la estructura armada.

---

## v2 — Features adicionales sugeridas

### A. Sincronización automática de audio/video
OBS puede introducir pequeños desfasajes entre el archivo de gameplay y el
de webcam si arrancaron en instantes distintos. Antes de componer, correlacionar
las dos pistas de audio (cross-correlation, ej. con `librosa` o `scipy.signal.correlate`)
para calcular el offset y alinear los clips automáticamente en el XML.
**Por qué:** evita que la webcam quede desincronizada del gameplay sin que el
usuario lo note hasta después de editar.

### B. Detección de highlights (picos de audio)
Ya prototipado: RMS sobre el audio para marcar candidatos a "buen momento"
(gritos, risas, reacciones). Se guarda como metadata (JSON) asociada al proyecto,
no como corte automático — son marcadores/guías, no decisiones.
**Por qué:** hoy se dedica tiempo a re-mirar todo el material para encontrar
estos momentos; con marcadores ya puestos en el timeline de Kdenlive (via
`<marker>` en el MLT), el usuario los ve de un vistazo.

### C. Generación de un segundo proyecto Kdenlive para Shorts
En vez de exportar `.mp4` ya renderizados, generar un **segundo archivo de
proyecto** (`shorts.mlt`, o uno por highlight: `short_01.mlt`, `short_02.mlt`...)
que referencia el **mismo material fuente** que el proyecto largo, pero con:
- Composición en vertical 9:16 (crop fijo, coordenadas configurables una vez)
- In/out ya ubicados ~15-20s antes y después de cada highlight detectado
- Webcam reposicionada acorde al nuevo formato (si aplica)

Al ser un proyecto Kdenlive (no un render), sigue siendo 100% editable y
no-destructivo: si un corte no convence, se ajusta el in/out en Kdenlive
como con cualquier clip, sin perder calidad ni tener que re-exportar desde cero.

**Qué elimina exactamente:** no el trabajo de pulir cada short (eso sigue
siendo manual), sino la etapa de **re-buscar los momentos** en el material
crudo — ambos proyectos (largo y shorts) se generan a partir de un único
pase de análisis sobre el material, en vez de mirar el video dos veces.

### D. Normalización de audio
Aplicar normalización de loudness (ej. `ffmpeg loudnorm` en dos pasadas) a
ambas pistas de audio antes de componer, en vez de hacerlo a mano en Kdenlive
como hoy.
**Por qué:** es un paso mecánico y repetitivo que ya se hace manualmente en
cada video; automatizarlo no compromete criterio editorial.

### E. Transcripción + detección de eventos por texto (más ambicioso, fase 3)
Usar Whisper (local o API) para transcribir el audio y buscar palabras/frases
clave (risas marcadas, exclamaciones, nombres de jefes/bosses, etc.) que
refuercen o complementen la detección por volumen (RMS solo capta intensidad,
no contenido).
**Por qué:** mejora la precisión de los highlights más allá de "hubo un pico
de volumen" — capta también momentos narrativamente relevantes aunque sean
dichos en voz baja.

### F. Config por perfil de sesión
Un archivo de config (YAML/JSON) con los parámetros que no cambian entre
sesiones: posición/tamaño del cuadro de webcam, coordenadas de crop para
shorts, umbral de silencio, FPS del proyecto. Evita pasar los mismos flags
por CLI cada vez.
**Por qué:** el layout de grabación es estable sesión a sesión — configurar
una vez y reusar.

### G. Reporte resumen post-procesamiento
Al terminar, imprimir/loguear: duración original vs. duración post-corte de
silencios, cantidad de highlights detectados, cantidad de candidatos a short
generados. Ayuda a calibrar los umbrales sin tener que abrir el proyecto.

---

## Diseño de CLI

Tres subcomandos, todos reciben el/los archivo(s) fuente (gameplay + webcam)
y un `--titulo` común, a partir del cual se generan los nombres de salida
con sufijo automático:

```bash
# Genera ambos proyectos
python3 editor.py both --titulo "re9_ep12" --gameplay gameplay.mp4 --webcam webcam.mp4
# -> re9_ep12_long.mlt
# -> re9_ep12_short.mlt (o re9_ep12_short_01.mlt, _02.mlt... si hay varios highlights)

# Solo el proyecto largo
python3 editor.py long --titulo "re9_ep12" --gameplay gameplay.mp4 --webcam webcam.mp4
# -> re9_ep12_long.mlt

# Solo los proyectos de shorts
python3 editor.py short --titulo "re9_ep12" --gameplay gameplay.mp4 --webcam webcam.mp4
# -> re9_ep12_short_01.mlt, re9_ep12_short_02.mlt, ...
```

**Notas de diseño:**
- El análisis (detección de silencios + highlights) se corre siempre como
  paso previo interno, sin importar el subcomando — `long` y `short` no
  deberían repetir el análisis si se corren en la misma sesión (cachear el
  resultado en un `.analisis.json` con el mismo `--titulo`, y reusarlo si
  existe y es más reciente que el archivo fuente).
- El sufijo (`_long`, `_short`) se agrega siempre en código, no lo tipea el
  usuario — el `--titulo` es el nombre base sin sufijo.
- `short` sin haber corrido `both` o `long` antes debe poder generar su propio
  análisis desde cero (no depende del comando `long` para funcionar).

---

## Decisiones técnicas a definir mientras se itera

- **Formato de proyecto:** ¿`.mlt` puro o `.kdenlive` (que incluye metadata
  extra de la app, como carpetas de proyecto y bins)? `.kdenlive` es más
  "nativo" pero tiene más estructura para generar correctamente.
- **Composición PiP en el XML:** Kdenlive maneja esto vía el filtro
  `affine`/`qtblend` sobre una segunda pista de video (transform + posición).
  Hay que armar esto a mano en el XML, ya que no hay una librería Python
  de alto nivel para "componer proyectos Kdenlive" — es edición directa de MLT.
- **Referencia de audio para silencios:** ¿el gameplay, la webcam (voz), o
  una mezcla? Probablemente la voz (webcam) sea mejor referencia para
  "momentos sin comentario", pero a veces el usuario reacciona sin hablar
  (silencio en cámara + acción en pantalla) — ver cómo balancear esto.
- **Herramientas base:** `ffmpeg`/`ffprobe` (silencio, crop, normalización),
  `librosa` (análisis de audio), `melt` (motor de Kdenlive, para preview/render
  headless sin abrir la GUI).

---

## Prioridad sugerida de implementación
1. v1 (composición + corte de silencios) — ya resuelve el bloqueo principal
2. C (shorts automáticos) — resuelve la duplicación de trabajo, que es la
   segunda fuente de fricción más grande
3. B (marcadores de highlights) — barato de agregar, ya que el análisis RMS
   es el mismo que usa C
4. D (normalización) — quick win, poco riesgo
5. A (sincronización automática) — importante pero puede probarse primero
   a mano (offset fijo) si el desfasaje de OBS es consistente
6. E (transcripción) y F (config) — mejoras de fase 2 una vez que v1+C están
   validados en uso real
