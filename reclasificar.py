#!/usr/bin/env python3
"""CLI para correr (o re-correr) transcripcion + clasificacion de contenido
(v3.1, ver spec_clasificacion_contenido.md) sobre UNA sesion gameplay+webcam,
y generar el proyecto Kdenlive largo resultante.

Requiere ollama corriendo (`ollama serve`) con el modelo de cfg["classification"]["model"]
ya descargado (`ollama pull <modelo>`).

Ejemplos:
    # corte completo (todo menos "relleno"), reusando cache si no cambio nada:
    python3 reclasificar.py --titulo partida_4__2026-07-05_14-04-37 \\
        --gameplay "/home/said/Vídeos/re_9/partida_4/2026-07-05 14-04-37-gameplay.mp4" \\
        --webcam   "/home/said/Vídeos/re_9/partida_4/2026-07-05 14-04-37-webcam.mp4" \\
        --output-dir "/home/said/Vídeos/re_9/partida_4"

    # forzar re-transcripcion + re-clasificacion (p.ej. despues de un fix o
    # cambio de prompt/categorias) y ademas generar un corte "solo lo mejor":
    python3 reclasificar.py --titulo partida_4__2026-07-05_14-04-37 \\
        --gameplay "..." --webcam "..." --output-dir "..." \\
        --force-transcripcion --force-clasificacion \\
        --categorias divertido_interesante --nombre-salida partida_4_solo_relevante
"""
import argparse
import os

from gameplay_editor import audio_analysis, classification, project_long, project_short, transcription
from gameplay_editor.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--titulo", required=True, help="Clave de cache (define los .analisis/.transcripcion/.clasificacion.json)")
    parser.add_argument("--gameplay", required=True)
    parser.add_argument("--webcam", required=True)
    parser.add_argument("--output-dir", required=True, help="Carpeta donde viven/se escriben los .json de cache y el .kdenlive")
    parser.add_argument("--config", default=None, help="Config JSON adicional (ver config.example.json)")
    parser.add_argument("--model", default=None, help="Override del modelo de whisper (default: medium)")
    parser.add_argument("--categorias", nargs="*", default=None,
                         help="Si se pasa, el corte final incluye SOLO estas categorias "
                              "(ej. --categorias divertido_interesante). Sin esto, incluye todo menos 'relleno'.")
    parser.add_argument("--nombre-salida", default=None, help="Sufijo del .kdenlive generado (default: --titulo)")
    parser.add_argument("--shorts", action="store_true", help="Ademas del video largo, genera <nombre-salida>_shorts.kdenlive")
    parser.add_argument("--sin-largo", action="store_true",
                         help="No (re)genera el proyecto largo -- util si ya lo editaste a mano en Kdenlive "
                              "y no queres pisarlo. Se usa junto con --shorts para generar SOLO los shorts.")
    parser.add_argument("--force-analysis", action="store_true", help="Ignora cache de silencio/RMS")
    parser.add_argument("--force-transcripcion", action="store_true", help="Ignora cache de transcripcion (re-corre Whisper)")
    parser.add_argument("--force-clasificacion", action="store_true", help="Ignora cache de clasificacion (re-corre ollama/API)")
    args = parser.parse_args()

    if args.sin_largo and not args.shorts:
        raise SystemExit("--sin-largo no genera nada por si solo: usalo junto con --shorts")

    cfg = load_config(args.config)
    if args.model:
        cfg["transcription"]["model"] = args.model

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"[1/3] Analisis de silencio/RMS ({args.titulo})...")
    analysis = audio_analysis.analyze(
        args.gameplay, args.webcam, cfg, args.titulo,
        output_dir=args.output_dir, force=args.force_analysis,
    )

    print("[2/3] Transcripcion (Whisper, puede tardar varios minutos)...")
    transcripcion = transcription.transcribe(
        args.gameplay, args.webcam, cfg["transcription"], args.titulo,
        output_dir=args.output_dir, force=args.force_transcripcion,
    )
    print(f"      jugador: {len(transcripcion['jugador'])} segmentos, juego: {len(transcripcion['juego'])} segmentos")

    print("[3/3] Clasificacion por LLM (ollama, una llamada por ventana)...")
    classified = classification.classify(
        transcripcion, cfg["classification"], args.titulo,
        output_dir=args.output_dir, force=args.force_clasificacion,
        duration=analysis["duration"],
    )
    from collections import Counter
    print(f"      {len(classified)} ventanas -> {dict(Counter(w['categoria'] for w in classified))}")

    if args.categorias:
        keep = classification.keep_segments_for_categories(classified, analysis["duration"], args.categorias)
    else:
        keep = classification.merge_keep_segments(classified, analysis["duration"])
    kept_sec = sum(e - s for s, e in keep)
    print(f"      keep_segments: {len(keep)} tramos, {kept_sec:.1f}s de {analysis['duration']:.1f}s "
          f"({100 * kept_sec / analysis['duration']:.1f}%)")

    analysis_final = dict(analysis)
    analysis_final["keep_segments"] = keep
    analysis_final["highlights"] = classification.classification_highlights(classified)

    nombre = args.nombre_salida or args.titulo

    if not args.sin_largo:
        out_path = project_long.build(args.gameplay, args.webcam, cfg, analysis_final, nombre, args.output_dir)
        print("Proyecto largo generado:", out_path)
    else:
        print("Proyecto largo: omitido (--sin-largo)")

    if args.shorts:
        # los shorts se arman alrededor de cada highlight (ver highlight_windows
        # en project_short.py); con v3.1 esos highlights vienen de la clasificacion
        # (ventanas divertido_interesante), no del pico de RMS de v1.
        shorts_path = project_short.build_all(args.gameplay, args.webcam, cfg, analysis_final, nombre, args.output_dir)
        print("Proyecto de shorts generado:", shorts_path)


if __name__ == "__main__":
    main()
