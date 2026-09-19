# Spec: Módulo de clasificación de contenido por transcripción (v3 del editor de Jaco)

## Contexto y por qué este spec es separado

Ya existe un editor funcional (ver `spec_editor_gameplays.md`) con tres
comandos (`both`, `long`, `short`) que genera proyectos Kdenlive a partir de
gameplay + webcam, usando un análisis de audio (silencios por `ffmpeg
silencedetect` + highlights por RMS) cacheado en un `.analisis.json`.

**Problema detectado en uso real de esa v1:** el criterio de corte por
volumen tiene dos fallas sistemáticas:
1. **Falsos positivos de corte:** cinemáticas del juego donde el usuario
   está en silencio (no está hablando) se recortan como si fueran muertos,
   cuando en realidad deberían quedar en el video.
2. **Falsos negativos de corte:** tramos donde el usuario habla de forma
   sostenida pero el contenido es relleno/aburrido no se detectan como
   descartables, porque tienen volumen constante — el RMS no distingue
   "hablar con contenido interesante" de "hablar sin decir nada relevante".

**Objetivo de este módulo:** reemplazar (o complementar) la señal de RMS
con una señal de **contenido**, vía transcripción + clasificación por LLM.
Este spec cubre solo esa pieza — no toca composición PiP, generación de XML,
ni la CLI existente más que en el punto de integración.

---

## Alcance v3.1 — Transcripción + clasificación por texto

### Paso 1: Transcripción (dos fuentes, no una)
- Transcribir **ambas pistas por separado**: la voz del usuario (audio de
  webcam) y el audio del juego (diálogos de cinemáticas, NPCs, etc.). OBS ya
  las graba en tracks separados, así que no hace falta separar nada, solo
  correr Whisper dos veces (o en paralelo) sobre cada archivo.
- Usar `whisper.cpp` o la librería Python de Whisper corriendo local
  (sin GPU dedicada disponible — Intel Ultra 7 con QuickSync, sin CUDA).
  Empezar con el modelo `medium` como balance velocidad/precisión; medir
  tiempo real de procesamiento sobre una sesión de 2-3h antes de decidir
  si `small` alcanza o si vale la pena `large`. Con dos pistas a transcribir,
  el tiempo total se duplica — tenerlo en cuenta al elegir el modelo.
- Output: dos listas de segmentos con `{inicio, fin, texto, fuente}` (fuente
  = "jugador" o "juego"), a nivel frase.
- **Qué resuelve esto y qué no:** en cinemáticas con diálogo hablado (no
  todos los juegos lo tienen), ahora hay contenido real para clasificar ese
  tramo aunque el jugador esté en silencio — deja de ser una zona ciega.
  En tramos de acción/exploración pura con solo música o efectos de sonido
  (sin diálogo de ningún lado), sigue sin haber señal de contenido — ahí
  el fallback del Paso 3 sigue siendo necesario, solo que aplica a menos
  casos que antes.

### Paso 2: Clasificación por LLM
- Agrupar la transcripción en ventanas con contexto (ej. ventanas de
  30-60s, con solapamiento para no cortar una idea a la mitad).
- Cada ventana debe combinar lo dicho por el jugador **y** lo que dice el
  juego en ese mismo rango temporal (si hay diálogo de cinemática ahí), para
  que el LLM tenga el cuadro completo al clasificar — no solo la voz del
  usuario.
- Para cada ventana, pedirle a un LLM que clasifique en categorías, por ejemplo:
  - `divertido/interesante` — reacción genuina, comentario gracioso, momento
    narrativamente relevante → candidato fuerte a conservar / a short.
  - `relleno` — habla sostenida sin contenido relevante (pensar en voz alta
    repetitivo, muletillas, silencio de juego narrado sin gracia) → candidato
    a recortar aunque tenga volumen.
  - `neutro/contexto necesario` — no es un highlight, pero aporta continuidad
    narrativa (explicación de qué está pasando, avance de la historia) →
    conservar en el video largo, pero no usar como candidato a short.
- El prompt al LLM debería incluir el texto de la ventana + posiblemente
  unas pocas ventanas de contexto antes/después, para que la clasificación
  no sea miope a nivel frase suelta.
- Output: mismo `.analisis.json` (o una extensión de él) con un array de
  segmentos clasificados, reemplazando/complementando los `highlights_candidatos`
  actuales (que quedaban basados solo en RMS).

### Paso 3: Integración con el corte de silencios existente
Regla a definir explícitamente en la implementación (punto de decisión, no
resuelto de antemano):
- Un tramo de **silencio de audio** (sin voz) que la clasificación de texto
  no puede evaluar (no hay transcripción ahí) debería tratarse como
  "mantener por defecto" en vez de "cortar por defecto" — para no perder
  cinemáticas silenciosas. Esto invierte la heurística actual de la v1.
- Un tramo con **voz pero clasificado como relleno** se marca para corte,
  aunque el `silencedetect` no lo hubiera marcado (porque hay volumen).
- Conclusión operativa: el criterio de corte pasa de ser primariamente
  "silencio de audio" a ser primariamente "clasificación de contenido",
  y el silencio de audio queda como señal secundaria solo para tramos sin
  transcripción posible.

### Paso 4: Curaduría activa del video LARGO por escenario (no solo para shorts)

**Aclaración importante de alcance** (corrige una interpretación anterior de
este spec): esto no es sobre de dónde salen los shorts — es sobre qué queda
adentro del video largo. El pedido es que los cortes/splits que arma el
video largo tomen preferentemente **la mejor parte de cada escenario, o las
mejores situaciones en general**, en vez de solo remover silencio y relleno
y dejar todo lo demás.

Esto es un cambio de objetivo respecto a v1/v3.1 Paso 3, no solo una feature
más encima:
- **v1 + Paso 3 (hasta acá):** el editor es un *filtro* — saca lo que
  sobra (silencio sin cinemática, relleno hablado) y deja el resto tal cual.
- **Paso 4:** el editor pasa a ser un *compositor* — entre varios tramos
  candidatos de un mismo escenario o situación, elige activamente el/los
  mejores y puede comprimir o descartar tramos que no son "relleno" en
  sentido estricto, pero que compiten por tiempo de video contra material
  mejor de otra parte de la sesión. Es más parecido a cómo el usuario
  editaría a mano, priorizando calidad narrativa sobre solo remover lo malo.

**Cómo agrupar en "escenarios/situaciones" con solo texto (sin visión):**
- Agrupar por continuidad temporal + señales explícitas en el texto (el
  jugador suele nombrar dónde está o qué va a hacer — "bueno ahora vamos
  al...", "esto es la parte de..."), reforzado ahora por el diálogo del
  juego (Paso 1) que también marca transiciones (cambios de nivel, cinemáticas
  que anuncian una nueva zona).
- Dentro de cada grupo, usar la clasificación del Paso 2 para rankear los
  sub-tramos (`divertido/interesante` > `neutro/contexto necesario` > `relleno`)
  y decidir cuánto tiempo de ese escenario se conserva en el video largo,
  priorizando los de mejor clasificación en vez de conservar todo por parejo.
- El resultado sigue siendo no-destructivo: esto decide qué segmentos entran
  al playlist del `.mlt`, el usuario puede revertir cualquier corte a mano
  en Kdenlive como siempre.

**Riesgo a vigilar:** con este cambio de objetivo, el largo puede terminar
más corto de lo esperado si el algoritmo es agresivo priorizando calidad —
conviene un parámetro de "duración objetivo" o "cuánto conservar por
escenario como mínimo" para no vaciar de contexto narrativo tramos que son
necesarios aunque no sean el punto más alto de emoción.

---

## Alcance v3.2 — Contexto visual (fase posterior, no bloqueante para v3.1)

Idea: samplear frames del gameplay y describirlos con un modelo con visión
(qué está pasando: combate, cinemática, exploración, menú, cambio de zona)
para dar contexto que el audio solo no puede dar, y para separar "escenarios"
de forma más confiable que la heurística de texto del Paso 4 — que hoy
depende de que el jugador o el juego lo digan explícitamente en voz alta,
lo cual no siempre pasa.

**Por qué es fase posterior:**
- Costo bastante mayor: no es una sola llamada de texto por ventana, son
  llamadas con imágenes — para 2-3h de contenido, cientos de frames si se
  samplea parejo. Necesita muestreo inteligente (solo samplear cerca de
  donde el texto ya sugiere algo interesante, no todo el video parejo).
- Conviene validar primero cuánto mejora realmente el resultado con solo
  transcripción + clasificación (v3.1) antes de sumar esta complejidad.
  Es posible que el texto solo resuelva la mayoría del problema reportado.

**Si se implementa:** el output sería una descripción de escena por frame
sampleado, usada para (a) reforzar la clasificación de "interesante vs
relleno" con contexto de lo que se ve en pantalla, y (b) detectar cambios
de escenario de forma más precisa que agrupar por huecos temporales.

---

## Puntos abiertos a resolver en la implementación

- **Costo/tiempo de transcribir 2-3h de audio localmente**: medir antes de
  comprometerse a un modelo de Whisper específico.
- **Qué LLM usar para clasificar**: local (más barato, potencialmente peor
  calidad de criterio) vs. API (mejor criterio, costo por uso — a evaluar
  si es viable para el volumen de contenido que se sube).
- **Formato de las categorías de clasificación**: las tres propuestas
  (`divertido/interesante`, `relleno`, `neutro/contexto necesario`) son un
  punto de partida, no una decisión cerrada — ajustar según qué tan bien
  discrimina en la práctica.
- **Cómo se integra en el `.analisis.json` existente**: decidir si se
  extiende el mismo archivo o se genera un archivo separado
  (`.clasificacion.json`) que el resto del pipeline consume además del
  original.

---

## Prioridad sugerida

1. Transcripción + clasificación básica (Pasos 1-2) — ataca directamente
   los dos falsos (positivo/negativo) reportados en uso real.
2. Integración con el corte existente (Paso 3) — sin esto, tener la
   clasificación no cambia nada del output real.
3. Curaduría por escenario para el video largo (Paso 4) vía heurística de
   texto — más barato, ya evalúa si vale la pena ir a v3.2.
4. v3.2 (visión) — solo si el Paso 4 con heurística de texto se queda corto
   en la práctica.
