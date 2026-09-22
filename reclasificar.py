#!/usr/bin/env python3
"""CLI para correr (o re-correr) transcripcion + clasificacion de contenido
(v3.1, ver spec_clasificacion_contenido.md) sobre una sesion gameplay+webcam
o una carpeta con varias, y generar el proyecto Kdenlive resultante.

Requiere ollama corriendo (`ollama serve`) con el modelo de cfg["classification"]["model"]
ya descargado (`ollama pull <modelo>`).

Ejemplos:
    # una sesion (corte completo, todo menos "relleno"), reusando cache si no cambio nada:
    python3 reclasificar.py --titulo partida_4__2026-07-05_14-04-37 \\
        --gameplay "/home/said/Vídeos/re_9/partida_4/2026-07-05 14-04-37-gameplay.mp4" \\
        --webcam   "/home/said/Vídeos/re_9/partida_4/2026-07-05 14-04-37-webcam.mp4" \\
        --output-dir "/home/said/Vídeos/re_9/partida_4"

    # todas las grabaciones de una carpeta (pares -gameplay/-webcam o -ps5/-webcam),
    # empalmadas en un solo proyecto largo, en orden cronologico:
    python3 reclasificar.py --titulo partida_5 --carpeta "/home/said/Vídeos/re_9/partida_5"

    # forzar re-transcripcion + re-clasificacion (p.ej. despues de un fix o
    # cambio de prompt/categorias) y ademas generar un corte "solo lo mejor":
    python3 reclasificar.py --titulo partida_4__2026-07-05_14-04-37 \\
        --gameplay "..." --webcam "..." --output-dir "..." \\
        --force-transcripcion --force-clasificacion \\
        --categorias divertido_interesante --nombre-salida partida_4_solo_relevante
"""
import argparse
import os
import sys
from collections import Counter

from gameplay_editor import audio_analysis, classification, project_long, project_short, session_folder, transcription
from gameplay_editor.config import load_config


def _process_session(gameplay_path, webcam_path, cfg, cache_key, output_dir, args):
    """Corre analisis + transcripcion + clasificacion para UN par gameplay/webcam
    y devuelve un dict {gameplay, webcam, analysis} listo para project_long/project_short."""
    print(f"--- {cache_key} ---")
    print("  [1/3] Analisis de silencio/RMS...")
    analysis = audio_analysis.analyze(
        gameplay_path, webcam_path, cfg, cache_key,
        output_dir=output_dir, force=args.force_analysis,
    )

    print("  [2/3] Transcripcion (Whisper, puede tardar varios minutos)...")
    transcripcion = transcription.transcribe(
        gameplay_path, webcam_path, cfg["transcription"], cache_key,
        output_dir=output_dir, force=args.force_transcripcion,
    )
    print(f"        jugador: {len(transcripcion['jugador'])} segmentos, juego: {len(transcripcion['juego'])} segmentos")

    print("  [3/3] Clasificacion por LLM (ollama, una llamada por ventana)...")
    classified = classification.classify(
        transcripcion, cfg["classification"], cache_key,
        output_dir=output_dir, force=args.force_clasificacion,
        duration=analysis["duration"],
    )
    print(f"        {len(classified)} ventanas -> {dict(Counter(w['categoria'] for w in classified))}")

    if args.categorias:
        keep = classification.keep_segments_for_categories(classified, analysis["duration"], args.categorias)
    else:
        keep = classification.merge_keep_segments(classified, analysis["duration"])
    kept_sec = sum(e - s for s, e in keep)
    print(f"        keep_segments: {len(keep)} tramos, {kept_sec:.1f}s de {analysis['duration']:.1f}s "
          f"({100 * kept_sec / analysis['duration']:.1f}%)")

    analysis_final = dict(analysis)
    analysis_final["keep_segments"] = keep
    analysis_final["highlights"] = classification.classification_highlights(classified)
    return {"gameplay": gameplay_path, "webcam": webcam_path, "analysis": analysis_final}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--titulo", required=True, help="Clave de cache (define los .analisis/.transcripcion/.clasificacion.json)")
    parser.add_argument("--gameplay", default=None, help="Usar junto con --webcam, para una sola sesion")
    parser.add_argument("--webcam", default=None, help="Usar junto con --gameplay, para una sola sesion")
    parser.add_argument("--carpeta", default=None,
                         help="Carpeta con varias grabaciones -gameplay/-webcam (o -ps5/-webcam) a "
                              "empalmar en un solo proyecto, en orden cronologico")
    parser.add_argument("--output-dir", default=None,
                         help="Carpeta donde viven/se escriben los .json de cache y el .kdenlive "
                              "(default: la misma --carpeta, si se uso esa opcion)")
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
    if args.carpeta and (args.gameplay or args.webcam):
        raise SystemExit("--carpeta no se puede combinar con --gameplay/--webcam")
    if not args.carpeta and not (args.gameplay and args.webcam):
        raise SystemExit("Especifica --gameplay junto con --webcam, o --carpeta")

    cfg = load_config(args.config)
    if args.model:
        cfg["transcription"]["model"] = args.model

    output_dir = args.output_dir or args.carpeta
    if not output_dir:
        raise SystemExit("--output-dir es obligatorio si no usaste --carpeta")
    os.makedirs(output_dir, exist_ok=True)

    if args.carpeta:
        pairs, warnings = session_folder.discover_pairs(args.carpeta)
        for w in warnings:
            print(f"AVISO: {w}")
        if not pairs:
            sys.exit(f"No se encontraron pares -gameplay/-webcam (o -ps5/-webcam) en {args.carpeta}")
        print(f"Encontradas {len(pairs)} sesion(es): {', '.join(p for p, _, _ in pairs)}")
        sessions = [
            _process_session(gp, wc, cfg, f"{args.titulo}__{session_folder.sanitize_prefix(prefix)}", output_dir, args)
            for prefix, gp, wc in pairs
        ]
    else:
        sessions = [_process_session(args.gameplay, args.webcam, cfg, args.titulo, output_dir, args)]

    nombre = args.nombre_salida or args.titulo
    multi = len(sessions) > 1

    if not args.sin_largo:
        if multi:
            out_path = project_long.build_multi(sessions, cfg, nombre, output_dir)
        else:
            s = sessions[0]
            out_path = project_long.build(s["gameplay"], s["webcam"], cfg, s["analysis"], nombre, output_dir)
        print("Proyecto largo generado:", out_path)
    else:
        print("Proyecto largo: omitido (--sin-largo)")

    if args.shorts:
        # los shorts se arman alrededor de cada highlight (ver highlight_windows
        # en project_short.py); con v3.1 esos highlights vienen de la clasificacion
        # (ventanas divertido_interesante), no del pico de RMS de v1.
        if multi:
            shorts_path = project_short.build_all_multi(sessions, cfg, nombre, output_dir)
        else:
            s = sessions[0]
            shorts_path = project_short.build_all(s["gameplay"], s["webcam"], cfg, s["analysis"], nombre, output_dir)
        print("Proyecto de shorts generado:", shorts_path)


if __name__ == "__main__":
    main()
