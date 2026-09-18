#!/usr/bin/env python3
"""Editor automatico de gameplays: pre-corte de silencios + composicion PiP +
deteccion de highlights, generando proyectos Kdenlive listos para ajuste fino manual.

Uso:
    python3 editor.py both  --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
    python3 editor.py long  --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4
    python3 editor.py short --titulo re9_ep12 --gameplay gameplay.mp4 --webcam webcam.mp4

Con varias grabaciones de la misma partida (se empalman en orden cronologico
en un solo proyecto, por el prefijo del nombre de archivo):
    python3 editor.py both --titulo re9_ep12 --carpeta /ruta/a/la/partida/
"""
import argparse
import os
import sys

from gameplay_editor import audio_analysis, project_long, project_short, session_folder
from gameplay_editor.config import load_config


def _common_args(parser):
    parser.add_argument("--titulo", required=True, help="Nombre base, sin sufijo (_long/_short se agrega solo)")
    parser.add_argument("--gameplay", default=None, help="Archivo de video+audio del gameplay (usar junto con --webcam)")
    parser.add_argument("--webcam", default=None, help="Archivo de video+audio de la webcam (usar junto con --gameplay)")
    parser.add_argument("--carpeta", default=None,
                         help="Carpeta con varias grabaciones -gameplay/-webcam (mismo prefijo de nombre) a "
                              "empalmar en orden cronologico en un solo proyecto")
    parser.add_argument("--config", default=None, help="Config JSON (ver config.example.json)")
    parser.add_argument("--output-dir", default=".",
                         help="Carpeta base de salida (adentro se crea una carpeta con el nombre del titulo). "
                              "Se ignora si se usa --carpeta: ahi el proyecto largo se guarda directo en esa "
                              "carpeta y los shorts en una subcarpeta 'shorts' dentro de ella")
    parser.add_argument("--force-analysis", action="store_true", help="Ignora el cache de analisis y lo recalcula")


def _sanitize(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def _resolve_sessions(args, cfg, project_dir):
    if args.carpeta:
        if args.gameplay or args.webcam:
            sys.exit("--carpeta no se puede combinar con --gameplay/--webcam")
        pairs, warnings = session_folder.discover_pairs(args.carpeta)
        for w in warnings:
            print(f"AVISO: {w}")
        if not pairs:
            sys.exit(f"No se encontraron pares -gameplay/-webcam en {args.carpeta}")
        sessions = []
        for prefix, gp, wc in pairs:
            cache_key = f"{args.titulo}__{_sanitize(prefix)}"
            analysis = audio_analysis.analyze(gp, wc, cfg, cache_key, output_dir=project_dir, force=args.force_analysis)
            sessions.append({"gameplay": gp, "webcam": wc, "analysis": analysis})
        return sessions

    if not (args.gameplay and args.webcam):
        sys.exit("Especifica --gameplay junto con --webcam, o --carpeta")
    analysis = audio_analysis.analyze(args.gameplay, args.webcam, cfg, args.titulo, output_dir=project_dir, force=args.force_analysis)
    return [{"gameplay": args.gameplay, "webcam": args.webcam, "analysis": analysis}]


def _print_report(analyses, long_path=None, short_path=None):
    print("--- Reporte ---")
    total_original = total_kept = 0.0
    total_highlights = total_peaks = 0
    total_peaks_sec = 0.0
    for i, analysis in enumerate(analyses, start=1):
        original = analysis["duration"]
        kept = sum(e - s for s, e in analysis["keep_segments"])
        peaks = analysis.get("protected_peaks") or []
        total_original += original
        total_kept += kept
        total_highlights += len(analysis["highlights"])
        total_peaks += len(peaks)
        total_peaks_sec += sum(e - s for s, e in peaks)
        if len(analyses) > 1:
            pct = 100 * (1 - kept / original) if original else 0
            print(f"  Sesion {i}: {original:.1f}s -> {kept:.1f}s ({pct:.1f}% recortado), "
                  f"{len(analysis['highlights'])} highlights")
    pct = 100 * (1 - total_kept / total_original) if total_original else 0
    print(f"Duracion original: {total_original:.1f}s  ->  post-corte de silencios: {total_kept:.1f}s ({pct:.1f}% recortado)")
    print(f"Highlights detectados: {total_highlights}")
    if total_peaks:
        print(f"Picos de gameplay protegidos del corte: {total_peaks} ({total_peaks_sec:.1f}s)")
    if long_path:
        print(f"Proyecto largo: {long_path}")
    if short_path:
        print(f"Proyecto de shorts: {short_path}")


def _long_dir(args):
    """Carpeta donde va el proyecto largo (y el cache de analisis). Con
    --carpeta, es la propia carpeta de grabaciones; si no, output-dir/titulo."""
    if args.carpeta:
        path = args.carpeta
    else:
        path = os.path.join(args.output_dir, args.titulo)
    os.makedirs(path, exist_ok=True)
    return path


def _shorts_dir(args, long_dir):
    """Carpeta donde va el proyecto de shorts. Con --carpeta, una subcarpeta
    'shorts' dentro de la carpeta de grabaciones; si no, la misma que el largo."""
    if args.carpeta:
        path = os.path.join(args.carpeta, "shorts")
        os.makedirs(path, exist_ok=True)
        return path
    return long_dir


def cmd_long(args):
    cfg = load_config(args.config)
    long_dir = _long_dir(args)
    sessions = _resolve_sessions(args, cfg, long_dir)
    if len(sessions) == 1:
        s = sessions[0]
        out_path = project_long.build(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, long_dir)
    else:
        out_path = project_long.build_multi(sessions, cfg, args.titulo, long_dir)
    _print_report([s["analysis"] for s in sessions], long_path=out_path)


def cmd_short(args):
    cfg = load_config(args.config)
    long_dir = _long_dir(args)
    short_dir = _shorts_dir(args, long_dir)
    sessions = _resolve_sessions(args, cfg, long_dir)
    if len(sessions) == 1:
        s = sessions[0]
        out_path = project_short.build_all(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, short_dir)
    else:
        out_path = project_short.build_all_multi(sessions, cfg, args.titulo, short_dir)
    _print_report([s["analysis"] for s in sessions], short_path=out_path)


def cmd_both(args):
    cfg = load_config(args.config)
    long_dir = _long_dir(args)
    short_dir = _shorts_dir(args, long_dir)
    sessions = _resolve_sessions(args, cfg, long_dir)
    if len(sessions) == 1:
        s = sessions[0]
        long_path = project_long.build(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, long_dir)
        short_path = project_short.build_all(s["gameplay"], s["webcam"], cfg, s["analysis"], args.titulo, short_dir)
    else:
        long_path = project_long.build_multi(sessions, cfg, args.titulo, long_dir)
        short_path = project_short.build_all_multi(sessions, cfg, args.titulo, short_dir)
    _print_report([s["analysis"] for s in sessions], long_path=long_path, short_path=short_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_long = sub.add_parser("long", help="Genera solo el proyecto largo")
    _common_args(p_long)
    p_long.set_defaults(func=cmd_long)

    p_short = sub.add_parser("short", help="Genera solo los proyectos de shorts")
    _common_args(p_short)
    p_short.set_defaults(func=cmd_short)

    p_both = sub.add_parser("both", help="Genera ambos proyectos")
    _common_args(p_both)
    p_both.set_defaults(func=cmd_both)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
