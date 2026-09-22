#!/usr/bin/env python3
"""Editor automatico de gameplays: transcribe ambas pistas (webcam + juego),
clasifica el contenido con un LLM para decidir que conservar, compone el
video largo (gameplay a pantalla completa + webcam en PiP) y genera shorts
verticales (9:16) alrededor de los momentos clasificados como interesantes.
Genera proyectos Kdenlive listos para ajuste fino manual. No renderiza nada.

Uso:
    python3 editor.py --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4

Con varias grabaciones de la misma partida (se empalman en orden cronologico
en un solo proyecto, por el prefijo del nombre de archivo):
    python3 editor.py --titulo re9_ep12 --folder /ruta/a/la/partida/

Requiere ollama corriendo (`ollama serve`) con el modelo de
cfg["classification"]["model"] ya descargado (`ollama pull <modelo>`).

Genera siempre <titulo>_long.kdenlive y <titulo>_shorts.kdenlive en
--output-dir. Si alguno ya existe, el script no lo pisa: hay que borrarlo a
mano antes de correr de nuevo.

Ver spec_clasificacion_contenido.md para el detalle del criterio de
clasificacion. El cortador de silencios/RMS original (gameplay_editor/audio_analysis.py:analyze)
sigue en el repo pero este script ya no lo usa.
"""
import argparse
import os
import sys
from collections import Counter

from gameplay_editor import audio_analysis, classification, project_long, project_short, session_folder, transcription
from gameplay_editor.config import load_config


def _build_session(gameplay_path, webcam_path, cfg, cache_key, output_dir, force_clasification):
    print(f"--- {cache_key} ---")
    gp_info, wc_info, duration = audio_analysis.probe_videos(gameplay_path, webcam_path)

    print("  Transcripcion (Whisper, puede tardar varios minutos)...")
    transcripcion = transcription.transcribe(
        gameplay_path, webcam_path, cfg["transcription"], cache_key, output_dir=output_dir,
    )
    print(f"    jugador: {len(transcripcion['jugador'])} segmentos, juego: {len(transcripcion['juego'])} segmentos")

    print("  Clasificacion por LLM (ollama, una llamada por ventana)...")
    classified = classification.classify(
        transcripcion, cfg["classification"], cache_key,
        output_dir=output_dir, force=force_clasification, duration=duration,
    )
    print(f"    {len(classified)} ventanas -> {dict(Counter(w['categoria'] for w in classified))}")

    keep = classification.merge_keep_segments(classified, duration)
    kept_sec = sum(e - s for s, e in keep)
    print(f"    keep_segments: {len(keep)} tramos, {kept_sec:.1f}s de {duration:.1f}s "
          f"({100 * kept_sec / duration:.1f}%)")

    analysis = {
        "duration": duration,
        "gameplay_info": gp_info,
        "webcam_info": wc_info,
        "keep_segments": keep,
        "highlights": classification.classification_highlights(classified),
    }
    return {"gameplay": gameplay_path, "webcam": webcam_path, "analysis": analysis}


def _resolve_sessions(args, cfg, output_dir):
    if args.folder:
        pairs, warnings = session_folder.discover_pairs(args.folder)
        for w in warnings:
            print(f"AVISO: {w}")
        if not pairs:
            sys.exit(f"No se encontraron pares -gameplay/-webcam (o -ps5/-webcam) en {args.folder}")
        print(f"Encontradas {len(pairs)} sesion(es): {', '.join(p for p, _, _ in pairs)}")
        return [
            _build_session(
                gp, wc, cfg, f"{args.titulo}__{session_folder.sanitize_prefix(prefix)}",
                output_dir, args.force_clasification,
            )
            for prefix, gp, wc in pairs
        ]
    return [_build_session(args.gameplay, args.webcam, cfg, args.titulo, output_dir, args.force_clasification)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--titulo", required=True, help="Nombre base de los archivos de salida y del cache")
    parser.add_argument("--gameplay", default=None, help="Video del gameplay (usar junto con --webcam)")
    parser.add_argument("--webcam", default=None, help="Video de la webcam (usar junto con --gameplay)")
    parser.add_argument("--folder", default=None,
                         help="Carpeta con varias grabaciones -gameplay/-webcam (o -ps5/-webcam) a "
                              "empalmar en orden cronologico en un solo proyecto")
    parser.add_argument("--output-dir", default=None,
                         help="Carpeta de salida (default: la propia --folder, si se uso esa opcion)")
    parser.add_argument("--force-clasification", action="store_true",
                         help="Ignora el cache de clasificacion y vuelve a llamar a ollama por ventana. "
                              "La transcripcion se sigue invalidando sola por mtime de los videos; para "
                              "forzarla sin eso, borrar el .transcripcion.json a mano.")
    args = parser.parse_args()

    if args.folder and (args.gameplay or args.webcam):
        sys.exit("--folder no se puede combinar con --gameplay/--webcam")
    if not args.folder and not (args.gameplay and args.webcam):
        sys.exit("Especifica --gameplay junto con --webcam, o --folder")

    output_dir = args.output_dir or args.folder
    if not output_dir:
        sys.exit("--output-dir es obligatorio si no usaste --folder")
    os.makedirs(output_dir, exist_ok=True)

    long_path = os.path.join(output_dir, f"{args.titulo}_long.kdenlive")
    shorts_path = os.path.join(output_dir, f"{args.titulo}_shorts.kdenlive")
    existentes = [p for p in (long_path, shorts_path) if os.path.exists(p)]
    if existentes:
        sys.exit(
            "Ya existe(n): " + ", ".join(existentes) +
            " -- borralos a mano si queres regenerarlos (este script no los pisa)."
        )

    cfg = load_config()
    sessions = _resolve_sessions(args, cfg, output_dir)

    if len(sessions) > 1:
        out_long = project_long.build_multi(sessions, cfg, args.titulo, output_dir)
        out_shorts = project_short.build_all_multi(sessions, cfg, args.titulo, output_dir)
    else:
        s = sessions[0]
        out_long = project_long.build(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, output_dir)
        out_shorts = project_short.build_all(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, output_dir)

    print("Proyecto largo generado:", out_long)
    print("Proyecto de shorts generado:", out_shorts)


if __name__ == "__main__":
    sys.exit(main())
