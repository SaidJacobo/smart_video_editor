"""Arma UN solo proyecto Kdenlive con una sola secuencia (timeline) que tiene
todos los shorts seguidos, con una guide al inicio de cada uno para poder
renderizarlos por guides, cada uno en su archivo. Por default, cada short son
los dos videos a pelo apilados (webcam arriba, gameplay abajo, sin blur).
Opcionalmente (shorts.background_blur.enabled) se agrega el gameplay
duplicado + blureado como relleno de fondo."""
import itertools
import os
import time
import uuid

from . import ffmpeg_utils, mlt_xml
from .timecode import fps_to_rational, frame_to_tc, seconds_to_frame, seconds_to_tc


def _fit_rect_in_box(mode, box, source_w, source_h):
    box_x, box_y, box_w, box_h = box
    if mode == "cover":
        scale = max(box_w / source_w, box_h / source_h)
    else:
        scale = min(box_w / source_w, box_h / source_h)
    w, h = source_w * scale, source_h * scale
    x = box_x + (box_w - w) / 2
    y = box_y + (box_h - h) / 2
    return x, y, w, h


def _resolve_box(rect_pct, canvas_w, canvas_h):
    x = rect_pct["x"] * canvas_w
    y = rect_pct["y"] * canvas_h
    w = rect_pct["w"] * canvas_w
    h = rect_pct["h"] * canvas_h if rect_pct["h"] else canvas_h - y
    return x, y, w, h


def highlight_windows(highlights, duration, pre_roll, post_roll, merge_gap):
    raw = [(max(0.0, h["time"] - pre_roll), min(duration, h["time"] + post_roll), [h]) for h in highlights]
    raw.sort(key=lambda w: w[0])
    merged = []
    for s, e, hs in raw:
        if merged and s - merged[-1][1] <= merge_gap:
            ps, pe, phs = merged[-1]
            merged[-1] = (ps, max(pe, e), phs + hs)
        else:
            merged.append((s, e, hs))
    return merged


def short_rects(cfg_shorts, width, height, gp_w, gp_h, wc_w, wc_h):
    """Recuadros (x, y, w, h) del gameplay, de la webcam y del gameplay
    blureado de fondo en un canvas de width x height."""
    gp_box = _resolve_box(cfg_shorts["gameplay_rect_pct"], width, height)
    wc_box = _resolve_box(cfg_shorts["webcam_rect_pct"], width, height)
    return (
        _fit_rect_in_box(cfg_shorts["gameplay_fit"], gp_box, gp_w, gp_h),
        _fit_rect_in_box(cfg_shorts["webcam_fit"], wc_box, wc_w, wc_h),
        _fit_rect_in_box("cover", (0, 0, width, height), gp_w, gp_h),
    )


def _short_entries(ids, window, gp_chain, wc_chain, gp_info, wc_info, cfg_shorts, fps, width, height):
    """Entries de una ventana para cada pista: gp_audio, wc_audio, [blur],
    gp_video, wc_video. gp_chain/wc_chain son (chain_id, bin_id)."""
    start, end, _ = window
    in_tc, out_tc = seconds_to_tc(start, fps), seconds_to_tc(end, fps)
    frames = seconds_to_frame(end, fps) - seconds_to_frame(start, fps) + 1
    blur_cfg = cfg_shorts["background_blur"]
    gp_rect, wc_rect, blur_rect = short_rects(
        cfg_shorts, width, height, gp_info["width"], gp_info["height"], wc_info["width"], wc_info["height"],
    )

    def qtblend(rect):
        return mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(*rect))

    def audio(chain, has_audio):
        # blank si esta grabacion no tiene audio pero otra si: la pista existe
        # igual y sin el blank se correrian los shorts siguientes
        if has_audio:
            return mlt_xml.entry(in_tc, out_tc, chain[0], bin_id=chain[1])
        return mlt_xml.blank(frame_to_tc(frames, fps))

    entries = {
        "gp_audio": audio(gp_chain, gp_info["has_audio"]),
        "wc_audio": audio(wc_chain, wc_info["has_audio"]),
        "gp_video": mlt_xml.entry(in_tc, out_tc, gp_chain[0], [qtblend(gp_rect)], bin_id=gp_chain[1]),
        "wc_video": mlt_xml.entry(in_tc, out_tc, wc_chain[0], [qtblend(wc_rect)], bin_id=wc_chain[1]),
    }
    if blur_cfg["enabled"]:
        blur_filters = [qtblend(blur_rect)] + [
            mlt_xml.squareblur_filter(f"filter{next(ids)}", blur_cfg["kernel"]) for _ in range(blur_cfg["passes"])
        ]
        entries["blur"] = mlt_xml.entry(in_tc, out_tc, gp_chain[0], blur_filters, bin_id=gp_chain[1])
    return entries


def build_all(gameplay_path, webcam_path, cfg, analysis, titulo, output_dir="."):
    return build_all_multi(
        [{"gameplay": gameplay_path, "webcam": webcam_path, "analysis": analysis}], cfg, titulo, output_dir,
    )


def build_all_multi(sessions, cfg, titulo, output_dir="."):
    """Recorre varias sesiones (grabaciones distintas): cada sesion aporta sus
    propios highlights/ventanas (nunca se cruzan entre grabaciones) y sus
    propios clips de origen en el bin. Todos los shorts van seguidos en una
    sola secuencia, numerados de forma continua. Si no hay ningun short no
    escribe nada y devuelve None."""
    ids = itertools.count(1)
    bin_id = itertools.count(1)
    shorts_cfg = cfg["shorts"]
    fps = cfg["project"]["fps"] or sessions[0]["analysis"]["gameplay_info"]["fps"]
    fps_num, fps_den = fps_to_rational(fps)
    width, height = shorts_cfg["width"], shorts_cfg["height"]

    body = []
    chain_entries = []
    tracks = {"gp_audio": [], "wc_audio": [], "blur": [], "gp_video": [], "wc_video": []}
    guides = []
    # frames reales de cada entry (out inclusivo) para que cada guide caiga
    # justo en el primer frame de su short
    total_frames = 0
    gp_has_audio = wc_has_audio = False

    for session in sessions:
        gameplay_path, webcam_path = session["gameplay"], session["webcam"]
        analysis = session["analysis"]
        gp_info, wc_info = analysis["gameplay_info"], analysis["webcam_info"]

        windows = highlight_windows(
            analysis["highlights"],
            analysis["duration"],
            shorts_cfg["pre_roll_sec"],
            shorts_cfg["post_roll_sec"],
            shorts_cfg["merge_gap_sec"],
        )
        if not windows:
            continue
        gp_has_audio = gp_has_audio or gp_info["has_audio"]
        wc_has_audio = wc_has_audio or wc_info["has_audio"]

        gp_bin_id, wc_bin_id = next(bin_id), next(bin_id)
        gp_chain_id, wc_chain_id = f"chain_gameplay_{gp_bin_id}", f"chain_webcam_{wc_bin_id}"
        gp_len = round(gp_info["duration"] * fps)
        wc_len = round(wc_info["duration"] * fps)
        gp_len_tc = seconds_to_tc(gp_info["duration"], fps)
        wc_len_tc = seconds_to_tc(wc_info["duration"], fps)

        body.append(mlt_xml.chain(
            gp_chain_id, os.path.abspath(gameplay_path), gp_len_tc, gp_len, gp_bin_id,
            meta=ffmpeg_utils.chain_metadata(gameplay_path),
        ))
        body.append(mlt_xml.chain(
            wc_chain_id, os.path.abspath(webcam_path), wc_len_tc, wc_len, wc_bin_id,
            meta=ffmpeg_utils.chain_metadata(webcam_path),
        ))
        chain_entries.append((gp_chain_id, gp_len_tc))
        chain_entries.append((wc_chain_id, wc_len_tc))

        for window in windows:
            entries = _short_entries(
                ids, window, (gp_chain_id, gp_bin_id), (wc_chain_id, wc_bin_id),
                gp_info, wc_info, shorts_cfg, fps, width, height,
            )
            for track, entry in entries.items():
                tracks[track].append(entry)
            guides.append({"frame": total_frames, "comment": f"Short {len(guides) + 1:02d}"})
            start, end, _ = window
            total_frames += seconds_to_frame(end, fps) - seconds_to_frame(start, fps) + 1

    if not guides:
        return None

    out_tc = frame_to_tc(total_frames, fps)
    audio_track_ids, video_track_ids = [], []

    def add_track(entries, hide):
        pl_a = mlt_xml.playlist(f"playlist{next(ids)}", entries)
        pl_b = mlt_xml.playlist(f"playlist{next(ids)}")
        tid = f"tractor{next(ids)}"
        tt = mlt_xml.track_tractor(tid, out_tc, pl_a.get("id"), pl_b.get("id"), hide)
        body.extend([pl_a, pl_b, tt])
        return tid

    if gp_has_audio:
        audio_track_ids.append(add_track(tracks["gp_audio"], "video"))
    if wc_has_audio:
        audio_track_ids.append(add_track(tracks["wc_audio"], "video"))
    if tracks["blur"]:
        video_track_ids.append(add_track(tracks["blur"], "audio"))
    video_track_ids.append(add_track(tracks["gp_video"], "audio"))
    video_track_ids.append(add_track(tracks["wc_video"], "audio"))

    canvas_id = f"producer{next(ids)}"
    body.insert(0, mlt_xml.color_producer(canvas_id, out_tc))

    sequence_uuid = "{" + str(uuid.uuid4()) + "}"
    master_id = f"tractor{next(ids)}"
    master = mlt_xml.master_tractor(
        master_id, out_tc, canvas_id, audio_track_ids, video_track_ids, sequence_uuid, f"{titulo}_shorts",
        next(bin_id), total_frames,
    )
    master.append(mlt_xml.guides_property(guides))
    body.append(master)

    document_id = str(int(time.time() * 1000))
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    body.append(mlt_xml.main_bin_playlist(chain_entries, master_id, sequence_uuid, out_tc, document_id, profile_name))
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", master_id, out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=True)
    root = mlt_xml.build_document(profile_el, body)

    out_path = os.path.join(output_dir, f"{titulo}_shorts.kdenlive")
    mlt_xml.write_document(root, out_path)
    return out_path
