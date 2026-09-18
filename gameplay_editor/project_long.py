"""Arma el proyecto Kdenlive del video largo: gameplay a pantalla completa +
webcam en PiP, con los silencios ya recortados y los highlights como guides."""
import itertools
import os
import time
import uuid

from . import ffmpeg_utils, mlt_xml
from .timecode import fps_to_rational, seconds_to_tc


def _pip_rect(cfg_long, canvas_w, canvas_h, source_w, source_h):
    rect_cfg = cfg_long["webcam_rect_pct"]
    w = rect_cfg["w"] * canvas_w
    h = rect_cfg["h"] * canvas_h if rect_cfg["h"] else w * (source_h / source_w)
    x = rect_cfg["x"] * canvas_w
    y = rect_cfg["y"] * canvas_h
    return x, y, w, h


def _map_time_to_timeline(t, keep_segments):
    cumulative = 0.0
    for s, e in keep_segments:
        if s <= t <= e:
            return cumulative + (t - s)
        if t < s:
            return cumulative
        cumulative += e - s
    return cumulative


def build(gameplay_path, webcam_path, cfg, analysis, titulo, output_dir="."):
    ids = itertools.count(1)

    fps = cfg["project"]["fps"] or analysis["gameplay_info"]["fps"]
    fps_num, fps_den = fps_to_rational(fps)
    width = cfg["project"]["width"] or analysis["gameplay_info"]["width"]
    height = cfg["project"]["height"] or analysis["gameplay_info"]["height"]

    keep_segments = [tuple(seg) for seg in analysis["keep_segments"]]
    output_duration = sum(e - s for s, e in keep_segments)
    out_tc = seconds_to_tc(output_duration, fps)

    gp_len = round(analysis["gameplay_info"]["duration"] * fps)
    wc_len = round(analysis["webcam_info"]["duration"] * fps)
    gp_len_tc = seconds_to_tc(analysis["gameplay_info"]["duration"], fps)
    wc_len_tc = seconds_to_tc(analysis["webcam_info"]["duration"], fps)

    canvas_id = "producer0"
    gp_chain_id = "chain_gameplay"
    wc_chain_id = "chain_webcam"
    GP_BIN_ID, WC_BIN_ID = 1, 2

    canvas = mlt_xml.color_producer(canvas_id, out_tc)
    gp_chain = mlt_xml.chain(
        gp_chain_id, os.path.abspath(gameplay_path), gp_len_tc, gp_len, GP_BIN_ID,
        meta=ffmpeg_utils.chain_metadata(gameplay_path),
    )
    wc_chain = mlt_xml.chain(
        wc_chain_id, os.path.abspath(webcam_path), wc_len_tc, wc_len, WC_BIN_ID,
        meta=ffmpeg_utils.chain_metadata(webcam_path),
    )

    pip_x, pip_y, pip_w, pip_h = _pip_rect(
        cfg["long"], width, height,
        analysis["webcam_info"]["width"], analysis["webcam_info"]["height"],
    )
    opacity = cfg["long"]["webcam_opacity"]

    gp_video_entries, wc_video_entries = [], []
    gp_audio_entries, wc_audio_entries = [], []
    for s, e in keep_segments:
        in_tc, out_tc_seg = seconds_to_tc(s, fps), seconds_to_tc(e, fps)
        gp_video_entries.append(mlt_xml.entry(
            in_tc, out_tc_seg, gp_chain_id,
            [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(0, 0, width, height, 1.0))],
            bin_id=GP_BIN_ID,
        ))
        wc_video_entries.append(mlt_xml.entry(
            in_tc, out_tc_seg, wc_chain_id,
            [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(pip_x, pip_y, pip_w, pip_h, opacity))],
            bin_id=WC_BIN_ID,
        ))
        gp_audio_entries.append(mlt_xml.entry(in_tc, out_tc_seg, gp_chain_id, bin_id=GP_BIN_ID))
        wc_audio_entries.append(mlt_xml.entry(in_tc, out_tc_seg, wc_chain_id, bin_id=WC_BIN_ID))

    body = [canvas, gp_chain, wc_chain]
    audio_track_ids, video_track_ids = [], []

    def add_track(entries, hide, kind_label):
        pl_a = mlt_xml.playlist(f"playlist{next(ids)}", entries)
        pl_b = mlt_xml.playlist(f"playlist{next(ids)}")
        tid = f"tractor{next(ids)}"
        tt = mlt_xml.track_tractor(tid, out_tc, pl_a.get("id"), pl_b.get("id"), hide)
        body.extend([pl_a, pl_b, tt])
        return tid

    if analysis["gameplay_info"]["has_audio"]:
        audio_track_ids.append(add_track(gp_audio_entries, "video", "gameplay-audio"))
    if analysis["webcam_info"]["has_audio"]:
        audio_track_ids.append(add_track(wc_audio_entries, "video", "webcam-audio"))
    video_track_ids.append(add_track(gp_video_entries, "audio", "gameplay-video"))
    video_track_ids.append(add_track(wc_video_entries, "audio", "webcam-video"))

    sequence_uuid = "{" + str(uuid.uuid4()) + "}"
    master_id = f"tractor{next(ids)}"
    SEQUENCE_BIN_ID = 3
    master = mlt_xml.master_tractor(
        master_id, out_tc, canvas_id, audio_track_ids, video_track_ids, sequence_uuid, titulo,
        SEQUENCE_BIN_ID, round(output_duration * fps),
    )

    highlight_frames = []
    for h in analysis["highlights"]:
        t_out = _map_time_to_timeline(h["time"], keep_segments)
        highlight_frames.append({"frame": round(t_out * fps), "comment": f"highlight ({h['score']:.1f}sigma)"})
    if highlight_frames:
        master.append(mlt_xml.guides_property(highlight_frames))

    body.append(master)

    document_id = str(int(time.time() * 1000))
    chain_entries = [(gp_chain_id, gp_len_tc), (wc_chain_id, wc_len_tc)]
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    main_bin = mlt_xml.main_bin_playlist(chain_entries, master_id, sequence_uuid, out_tc, document_id, profile_name)
    body.append(main_bin)
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", master_id, out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=False)
    root = mlt_xml.build_document(profile_el, body)

    out_path = os.path.join(output_dir, f"{titulo}_long.kdenlive")
    mlt_xml.write_document(root, out_path)
    return out_path


def build_multi(sessions, cfg, titulo, output_dir="."):
    """Como build(), pero empalma varias grabaciones (gameplay+webcam) en UNA
    sola linea de tiempo continua, en el orden en que vienen en `sessions`
    (se espera cronologico). Cada sesion es un dict con gameplay/webcam/analysis
    (el resultado de audio_analysis.analyze para ese par)."""
    ids = itertools.count(1)

    first_gp_info = sessions[0]["analysis"]["gameplay_info"]
    fps = cfg["project"]["fps"] or first_gp_info["fps"]
    fps_num, fps_den = fps_to_rational(fps)
    width = cfg["project"]["width"] or first_gp_info["width"]
    height = cfg["project"]["height"] or first_gp_info["height"]

    gp_has_audio = any(s["analysis"]["gameplay_info"]["has_audio"] for s in sessions)
    wc_has_audio = any(s["analysis"]["webcam_info"]["has_audio"] for s in sessions)

    body = []
    chain_entries = []
    gp_video_entries, wc_video_entries = [], []
    gp_audio_entries, wc_audio_entries = [], []
    highlight_frames = []
    cumulative_out = 0.0
    bin_id = itertools.count(1)

    for session in sessions:
        gameplay_path, webcam_path = session["gameplay"], session["webcam"]
        analysis = session["analysis"]
        gp_info, wc_info = analysis["gameplay_info"], analysis["webcam_info"]
        keep_segments = [tuple(seg) for seg in analysis["keep_segments"]]
        session_duration = sum(e - s for s, e in keep_segments)

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

        pip_x, pip_y, pip_w, pip_h = _pip_rect(cfg["long"], width, height, wc_info["width"], wc_info["height"])
        opacity = cfg["long"]["webcam_opacity"]

        for s, e in keep_segments:
            in_tc, out_tc_seg = seconds_to_tc(s, fps), seconds_to_tc(e, fps)
            gp_video_entries.append(mlt_xml.entry(
                in_tc, out_tc_seg, gp_chain_id,
                [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(0, 0, width, height, 1.0))],
                bin_id=gp_bin_id,
            ))
            wc_video_entries.append(mlt_xml.entry(
                in_tc, out_tc_seg, wc_chain_id,
                [mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(pip_x, pip_y, pip_w, pip_h, opacity))],
                bin_id=wc_bin_id,
            ))
            if gp_info["has_audio"]:
                gp_audio_entries.append(mlt_xml.entry(in_tc, out_tc_seg, gp_chain_id, bin_id=gp_bin_id))
            if wc_info["has_audio"]:
                wc_audio_entries.append(mlt_xml.entry(in_tc, out_tc_seg, wc_chain_id, bin_id=wc_bin_id))

        for h in analysis["highlights"]:
            t_out = cumulative_out + _map_time_to_timeline(h["time"], keep_segments)
            highlight_frames.append({"frame": round(t_out * fps), "comment": f"highlight ({h['score']:.1f}sigma)"})

        cumulative_out += session_duration

    output_duration = cumulative_out
    out_tc = seconds_to_tc(output_duration, fps)

    audio_track_ids, video_track_ids = [], []

    def add_track(entries, hide):
        pl_a = mlt_xml.playlist(f"playlist{next(ids)}", entries)
        pl_b = mlt_xml.playlist(f"playlist{next(ids)}")
        tid = f"tractor{next(ids)}"
        tt = mlt_xml.track_tractor(tid, out_tc, pl_a.get("id"), pl_b.get("id"), hide)
        body.extend([pl_a, pl_b, tt])
        return tid

    if gp_has_audio:
        audio_track_ids.append(add_track(gp_audio_entries, "video"))
    if wc_has_audio:
        audio_track_ids.append(add_track(wc_audio_entries, "video"))
    video_track_ids.append(add_track(gp_video_entries, "audio"))
    video_track_ids.append(add_track(wc_video_entries, "audio"))

    canvas_id = f"producer{next(ids)}"
    body.insert(0, mlt_xml.color_producer(canvas_id, out_tc))

    sequence_uuid = "{" + str(uuid.uuid4()) + "}"
    master_id = f"tractor{next(ids)}"
    SEQUENCE_BIN_ID = next(bin_id)
    master = mlt_xml.master_tractor(
        master_id, out_tc, canvas_id, audio_track_ids, video_track_ids, sequence_uuid, titulo,
        SEQUENCE_BIN_ID, round(output_duration * fps),
    )
    if highlight_frames:
        master.append(mlt_xml.guides_property(highlight_frames))

    body.append(master)

    document_id = str(int(time.time() * 1000))
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    main_bin = mlt_xml.main_bin_playlist(chain_entries, master_id, sequence_uuid, out_tc, document_id, profile_name)
    body.append(main_bin)
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", master_id, out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=False)
    root = mlt_xml.build_document(profile_el, body)

    out_path = os.path.join(output_dir, f"{titulo}_long.kdenlive")
    mlt_xml.write_document(root, out_path)
    return out_path
