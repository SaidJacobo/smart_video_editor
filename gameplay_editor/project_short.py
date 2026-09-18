"""Arma UN solo proyecto Kdenlive con una secuencia (timeline) por highlight,
todas compartiendo los mismos clips de origen en el bin. Por default, cada
secuencia son los dos videos a pelo apilados (webcam arriba, gameplay abajo,
sin blur). Opcionalmente (shorts.background_blur.enabled) se agrega el
gameplay duplicado + blureado como relleno de fondo."""
import itertools
import os
import time
import uuid

from . import ffmpeg_utils, mlt_xml
from .timecode import fps_to_rational, seconds_to_tc

GP_BIN_ID, WC_BIN_ID = 1, 2
FIRST_SEQUENCE_BIN_ID = 3


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


def _build_sequence(ids, gp_chain_id, wc_chain_id, gp_bin_id, wc_bin_id, sequence_bin_id,
                     gp_info, wc_info, cfg, fps, width, height, titulo, index, window, body):
    start, end, highlights_in = window
    duration = end - start
    out_tc = seconds_to_tc(duration, fps)
    in_tc, seg_out_tc = seconds_to_tc(start, fps), seconds_to_tc(end, fps)

    canvas_id = f"producer{next(ids)}"
    body.append(mlt_xml.color_producer(canvas_id, out_tc))

    shorts_cfg = cfg["shorts"]
    blur_cfg = shorts_cfg["background_blur"]

    webcam_box = _resolve_box(shorts_cfg["webcam_rect_pct"], width, height)
    wc_x, wc_y, wc_w, wc_h = _fit_rect_in_box(shorts_cfg["webcam_fit"], webcam_box, wc_info["width"], wc_info["height"])

    gameplay_box = _resolve_box(shorts_cfg["gameplay_rect_pct"], width, height)
    gp_x, gp_y, gp_w, gp_h = _fit_rect_in_box(shorts_cfg["gameplay_fit"], gameplay_box, gp_info["width"], gp_info["height"])

    gp_video_entry = mlt_xml.entry(
        in_tc, seg_out_tc, gp_chain_id,
        [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(gp_x, gp_y, gp_w, gp_h))],
        bin_id=gp_bin_id,
    )
    wc_clean_entry = mlt_xml.entry(
        in_tc, seg_out_tc, wc_chain_id,
        [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(wc_x, wc_y, wc_w, wc_h))],
        bin_id=wc_bin_id,
    )
    gp_audio_entry = mlt_xml.entry(in_tc, seg_out_tc, gp_chain_id, bin_id=gp_bin_id)
    wc_audio_entry = mlt_xml.entry(in_tc, seg_out_tc, wc_chain_id, bin_id=wc_bin_id)

    gp_blur_entry = None
    if blur_cfg["enabled"]:
        blur_box = (0, 0, width, height)
        blur_x, blur_y, blur_w, blur_h = _fit_rect_in_box("cover", blur_box, gp_info["width"], gp_info["height"])
        blur_filters = [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(blur_x, blur_y, blur_w, blur_h))]
        for _ in range(blur_cfg["passes"]):
            blur_filters.append(mlt_xml.squareblur_filter(f"filter{next(ids)}", blur_cfg["kernel"]))
        gp_blur_entry = mlt_xml.entry(in_tc, seg_out_tc, gp_chain_id, blur_filters, bin_id=gp_bin_id)

    audio_track_ids, video_track_ids = [], []

    def add_track(entries, hide):
        pl_a = mlt_xml.playlist(f"playlist{next(ids)}", entries)
        pl_b = mlt_xml.playlist(f"playlist{next(ids)}")
        tid = f"tractor{next(ids)}"
        tt = mlt_xml.track_tractor(tid, out_tc, pl_a.get("id"), pl_b.get("id"), hide)
        body.extend([pl_a, pl_b, tt])
        return tid

    if gp_info["has_audio"]:
        audio_track_ids.append(add_track([gp_audio_entry], "video"))
    if wc_info["has_audio"]:
        audio_track_ids.append(add_track([wc_audio_entry], "video"))
    if gp_blur_entry is not None:
        video_track_ids.append(add_track([gp_blur_entry], "audio"))
    video_track_ids.append(add_track([gp_video_entry], "audio"))
    video_track_ids.append(add_track([wc_clean_entry], "audio"))

    sequence_uuid = "{" + str(uuid.uuid4()) + "}"
    master_id = f"tractor{next(ids)}"
    clipname = f"{titulo}_short_{index:02d}"
    master = mlt_xml.master_tractor(
        master_id, out_tc, canvas_id, audio_track_ids, video_track_ids, sequence_uuid, clipname,
        sequence_bin_id, round(duration * fps),
    )

    highlight_frames = [
        {"frame": round((h["time"] - start) * fps), "comment": f"highlight ({h['score']:.1f}sigma)"}
        for h in highlights_in
    ]
    if highlight_frames:
        master.append(mlt_xml.guides_property(highlight_frames))

    body.append(master)

    return master_id, sequence_uuid, out_tc


def build_all(gameplay_path, webcam_path, cfg, analysis, titulo, output_dir="."):
    windows = highlight_windows(
        analysis["highlights"],
        analysis["duration"],
        cfg["shorts"]["pre_roll_sec"],
        cfg["shorts"]["post_roll_sec"],
        cfg["shorts"]["merge_gap_sec"],
    )

    ids = itertools.count(1)
    fps = cfg["project"]["fps"] or analysis["gameplay_info"]["fps"]
    fps_num, fps_den = fps_to_rational(fps)
    width, height = cfg["shorts"]["width"], cfg["shorts"]["height"]

    gp_info, wc_info = analysis["gameplay_info"], analysis["webcam_info"]
    gp_len = round(gp_info["duration"] * fps)
    wc_len = round(wc_info["duration"] * fps)
    gp_len_tc = seconds_to_tc(gp_info["duration"], fps)
    wc_len_tc = seconds_to_tc(wc_info["duration"], fps)

    gp_chain_id, wc_chain_id = "chain_gameplay", "chain_webcam"
    gp_chain = mlt_xml.chain(
        gp_chain_id, os.path.abspath(gameplay_path), gp_len_tc, gp_len, GP_BIN_ID,
        meta=ffmpeg_utils.chain_metadata(gameplay_path),
    )
    wc_chain = mlt_xml.chain(
        wc_chain_id, os.path.abspath(webcam_path), wc_len_tc, wc_len, WC_BIN_ID,
        meta=ffmpeg_utils.chain_metadata(webcam_path),
    )

    body = [gp_chain, wc_chain]
    sequences = []
    for i, window in enumerate(windows, start=1):
        master_id, sequence_uuid, out_tc = _build_sequence(
            ids, gp_chain_id, wc_chain_id, GP_BIN_ID, WC_BIN_ID, FIRST_SEQUENCE_BIN_ID + i - 1,
            gp_info, wc_info, cfg, fps, width, height, titulo, i, window, body,
        )
        sequences.append((master_id, sequence_uuid, out_tc))

    document_id = str(int(time.time() * 1000))
    chain_entries = [(gp_chain_id, gp_len_tc), (wc_chain_id, wc_len_tc)]
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    active_master_id, active_uuid, active_out_tc = sequences[0]
    opensequences = ";".join(seq_uuid for _, seq_uuid, _ in sequences)
    main_bin = mlt_xml.main_bin_playlist_multi(
        chain_entries, sequences, active_uuid, opensequences, document_id, profile_name,
    )
    body.append(main_bin)
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", active_master_id, active_out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=True)
    root = mlt_xml.build_document(profile_el, body)

    out_path = os.path.join(output_dir, f"{titulo}_shorts.kdenlive")
    mlt_xml.write_document(root, out_path)
    return out_path


def build_all_multi(sessions, cfg, titulo, output_dir="."):
    """Como build_all(), pero recorre varias sesiones (grabaciones distintas):
    cada sesion aporta sus propios highlights/ventanas (nunca se cruzan entre
    grabaciones) y sus propios clips de origen en el bin, todo en un solo
    proyecto con una secuencia por short, numeradas de forma continua."""
    ids = itertools.count(1)
    bin_id = itertools.count(1)
    fps = cfg["project"]["fps"] or sessions[0]["analysis"]["gameplay_info"]["fps"]
    fps_num, fps_den = fps_to_rational(fps)
    width, height = cfg["shorts"]["width"], cfg["shorts"]["height"]

    body = []
    chain_entries = []
    sequences = []
    short_index = 1

    for session in sessions:
        gameplay_path, webcam_path = session["gameplay"], session["webcam"]
        analysis = session["analysis"]
        gp_info, wc_info = analysis["gameplay_info"], analysis["webcam_info"]

        windows = highlight_windows(
            analysis["highlights"],
            analysis["duration"],
            cfg["shorts"]["pre_roll_sec"],
            cfg["shorts"]["post_roll_sec"],
            cfg["shorts"]["merge_gap_sec"],
        )
        if not windows:
            continue

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
            sequence_bin_id = next(bin_id)
            master_id, sequence_uuid, out_tc = _build_sequence(
                ids, gp_chain_id, wc_chain_id, gp_bin_id, wc_bin_id, sequence_bin_id,
                gp_info, wc_info, cfg, fps, width, height, titulo, short_index, window, body,
            )
            sequences.append((master_id, sequence_uuid, out_tc))
            short_index += 1

    document_id = str(int(time.time() * 1000))
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    active_master_id, active_uuid, active_out_tc = sequences[0]
    opensequences = ";".join(seq_uuid for _, seq_uuid, _ in sequences)
    main_bin = mlt_xml.main_bin_playlist_multi(
        chain_entries, sequences, active_uuid, opensequences, document_id, profile_name,
    )
    body.append(main_bin)
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", active_master_id, active_out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=True)
    root = mlt_xml.build_document(profile_el, body)

    out_path = os.path.join(output_dir, f"{titulo}_shorts.kdenlive")
    mlt_xml.write_document(root, out_path)
    return out_path
