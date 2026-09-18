"""Analisis previo (siempre se corre, se cachea en <titulo>.analisis.json):
- deteccion + recorte de silencios (v1)
- deteccion de highlights por picos de RMS (v2.B), guardados como metadata/markers
"""
import json
import os

import numpy as np

from . import ffmpeg_utils


def merge_and_pad_silences(silences, padding_sec):
    shrunk = []
    for s, e in silences:
        ns, ne = s + padding_sec, e - padding_sec
        if ne > ns:
            shrunk.append((ns, ne))
    return shrunk


def complement(intervals, duration):
    keep = []
    cursor = 0.0
    for s, e in sorted(intervals):
        s = max(0.0, min(s, duration))
        e = max(0.0, min(e, duration))
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def drop_short_segments(segments, min_keep_sec):
    return [(s, e) for s, e in segments if e - s >= min_keep_sec]


def intersect_intervals(a, b):
    a, b = sorted(a), sorted(b)
    i = j = 0
    result = []
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            result.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def in_any_segment(t, segments):
    return any(s <= t <= e for s, e in segments)


def merge_intervals(intervals):
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def subtract_intervals(base, remove):
    """base menos lo que se solape con remove (ambas listas de intervalos)."""
    remove = sorted(remove)
    result = []
    for s, e in sorted(base):
        cursor = s
        for rs, re in remove:
            if re <= cursor or rs >= e:
                continue
            if rs > cursor:
                result.append((cursor, min(rs, e)))
            cursor = max(cursor, re)
            if cursor >= e:
                break
        if cursor < e:
            result.append((cursor, e))
    return result


def detect_peak_intervals(samples, sr, window_sec, z_threshold, pad_before_sec, pad_after_sec):
    """Ventanas de audio con RMS muy por encima del nivel reciente (z-score
    contra la media/desvio de todo el clip) -- disparos, explosiones, etc.
    A diferencia de detect_highlights, no aplica top-N ni min_gap: devuelve
    todas las ventanas que superan el umbral, ya fusionadas en intervalos."""
    window = max(1, int(window_sec * sr))
    n_windows = len(samples) // window
    if n_windows == 0:
        return []
    trimmed = samples[: n_windows * window].reshape(n_windows, window)
    rms = np.sqrt(np.mean(trimmed**2, axis=1))
    std = rms.std()
    if std < 1e-9:
        return []
    z = (rms - rms.mean()) / std

    intervals = []
    for i in np.where(z >= z_threshold)[0]:
        center = (i + 0.5) * window_sec
        intervals.append((center - window_sec / 2 - pad_before_sec, center + window_sec / 2 + pad_after_sec))
    return merge_intervals(intervals)


def detect_highlights(samples, sr, window_sec, z_threshold, min_gap_sec, max_highlights):
    window = max(1, int(window_sec * sr))
    n_windows = len(samples) // window
    if n_windows == 0:
        return []
    trimmed = samples[: n_windows * window].reshape(n_windows, window)
    rms = np.sqrt(np.mean(trimmed**2, axis=1))
    std = rms.std()
    if std < 1e-9:
        return []
    z = (rms - rms.mean()) / std

    candidates = sorted(np.where(z >= z_threshold)[0].tolist(), key=lambda i: -z[i])
    chosen = []
    for i in candidates:
        if len(chosen) >= max_highlights:
            break
        t = (i + 0.5) * window_sec
        if all(abs(t - h["time"]) >= min_gap_sec for h in chosen):
            chosen.append({"time": t, "score": float(z[i])})
    chosen.sort(key=lambda h: h["time"])
    return chosen


def _cache_path(titulo, output_dir):
    return os.path.join(output_dir or ".", f"{titulo}.analisis.json")


def _sources_signature(paths):
    return {p: os.path.getmtime(p) for p in paths}


def analyze(gameplay_path, webcam_path, cfg, titulo, output_dir=None, force=False):
    cache_file = _cache_path(titulo, output_dir)
    sig = _sources_signature([gameplay_path, webcam_path])

    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("sources") == sig:
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    gp_info = ffmpeg_utils.video_info(gameplay_path)
    wc_info = ffmpeg_utils.video_info(webcam_path)
    duration = min(gp_info["duration"], wc_info["duration"])

    ref = cfg["silence"]["reference"]
    noise_db = cfg["silence"]["noise_db"]
    min_sil = cfg["silence"]["min_silence_sec"]

    if ref == "mix":
        sil_gp = ffmpeg_utils.detect_silence(gameplay_path, noise_db, min_sil) if gp_info["has_audio"] else [(0, duration)]
        sil_wc = ffmpeg_utils.detect_silence(webcam_path, noise_db, min_sil) if wc_info["has_audio"] else [(0, duration)]
        silences = intersect_intervals(sil_gp, sil_wc)
    else:
        ref_path, ref_info = (webcam_path, wc_info) if ref == "webcam" else (gameplay_path, gp_info)
        silences = ffmpeg_utils.detect_silence(ref_path, noise_db, min_sil) if ref_info["has_audio"] else []

    protect_cfg = cfg["silence"]["protect_peaks"]
    protected_peaks = []
    if protect_cfg["enabled"] and gp_info["has_audio"]:
        gp_samples, gp_sr = ffmpeg_utils.extract_mono_pcm(gameplay_path, sample_rate=4000)
        protected_peaks = detect_peak_intervals(
            gp_samples, gp_sr,
            protect_cfg["window_sec"], protect_cfg["z_threshold"],
            protect_cfg["pad_before_sec"], protect_cfg["pad_after_sec"],
        )
        silences = subtract_intervals(silences, protected_peaks)

    padded = merge_and_pad_silences(silences, cfg["silence"]["padding_sec"])
    keep = complement(padded, duration)
    keep = drop_short_segments(keep, cfg["silence"]["min_keep_sec"])
    if not keep:
        keep = [(0.0, duration)]

    highlight_source = webcam_path if wc_info["has_audio"] else gameplay_path
    samples, sr = ffmpeg_utils.extract_mono_pcm(highlight_source, sample_rate=4000)
    highlights = detect_highlights(
        samples,
        sr,
        cfg["highlights"]["window_sec"],
        cfg["highlights"]["z_threshold"],
        cfg["highlights"]["min_gap_sec"],
        cfg["highlights"]["max_highlights"],
    )
    highlights = [h for h in highlights if in_any_segment(h["time"], keep)]

    result = {
        "titulo": titulo,
        "sources": sig,
        "gameplay": gameplay_path,
        "webcam": webcam_path,
        "duration": duration,
        "gameplay_info": gp_info,
        "webcam_info": wc_info,
        "silence_reference": ref,
        "keep_segments": keep,
        "highlights": highlights,
        "protected_peaks": protected_peaks,
    }
    os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result
