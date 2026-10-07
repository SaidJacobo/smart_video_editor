"""Arma el .kdenlive del timeline del editor: el mismo proyecto que
project_long.build (layout "long") o que project_short.build_all (layout
"shorts", un short por clip), pero con los clips que manda el frontend y la
metadata de los videos leida en el navegador (el backend no tiene los
videos). Ver SPEC-EXPORT-KDENLIVE.md y SPEC-SHORTS.md."""
import itertools
import os
import tempfile
import time
import uuid

from gameplay_editor import mlt_xml, project_short
from gameplay_editor.config import load_config
from gameplay_editor.project_long import _pip_rect
from gameplay_editor.timecode import fps_to_rational, frame_to_tc, seconds_to_frame, seconds_to_tc


def chain_metadata_from_info(video):
    """Las mismas claves meta.media.* que ffmpeg_utils.chain_metadata, con los
    datos que manda el navegador. Lo que el navegador no da usa los mismos
    defaults que chain_metadata."""
    props = {
        "meta.media.0.stream.type": "video",
        "meta.media.0.stream.frame_rate": f"{video.fps:g}",
        "meta.media.0.stream.sample_aspect_ratio": "1",
        "meta.media.0.codec.width": video.width,
        "meta.media.0.codec.height": video.height,
        "meta.media.0.codec.rotate": "0",
        "meta.media.0.codec.pix_fmt": "yuv420p",
        "meta.media.0.codec.sample_aspect_ratio": "1",
        "meta.media.0.codec.colorspace": "709",
        "meta.media.0.codec.name": "",
        "meta.media.0.codec.long_name": "",
    }
    if video.audio:
        props.update({
            "meta.media.1.stream.type": "audio",
            "meta.media.1.codec.sample_fmt": "fltp",
            "meta.media.1.codec.sample_rate": video.audio.sampleRate,
            "meta.media.1.codec.channels": video.audio.channels,
            "meta.media.1.codec.name": "",
            "meta.media.1.codec.long_name": "",
        })
    props["meta.media.nb_streams"] = "2" if video.audio else "1"
    props["meta.media.width"] = video.width
    props["meta.media.height"] = video.height
    props["meta.media.progressive"] = "1"
    return props


def _webcam_span(start, end, offset_frames, last_frame):
    """Tramo de webcam del clip [start, end] del gameplay (frames, end
    inclusive como el out de MLT), recortado a los frames que existen en la
    webcam. Devuelve (frames de blank antes, (in, out) o None, frames de
    blank despues), que suman lo mismo que el clip."""
    webcam_start, webcam_end = start + offset_frames, end + offset_frames
    visible_start, visible_end = max(webcam_start, 0), min(webcam_end, last_frame)
    if visible_start > visible_end:
        return end - start + 1, None, 0
    return visible_start - webcam_start, (visible_start, visible_end), webcam_end - visible_end


def build(request):
    cfg = load_config()
    shorts = request.layout == "shorts"
    blur_cfg = cfg["shorts"]["background_blur"]
    ids = itertools.count(1)
    bin_ids = itertools.count(1)

    media_by_id = {m.id: m for m in request.media}
    first = media_by_id[request.clips[0].mediaId].file
    fps = cfg["project"]["fps"] or first.fps
    fps_num, fps_den = fps_to_rational(fps)
    titulo = os.path.splitext(first.name)[0]
    if shorts:
        width, height = cfg["shorts"]["width"], cfg["shorts"]["height"]
        titulo += "_shorts"
    else:
        width = cfg["project"]["width"] or first.width
        height = cfg["project"]["height"] or first.height

    def clip_frames(clip):
        # out inclusivo, como el out de MLT
        return seconds_to_frame(clip.out, fps) - seconds_to_frame(clip.in_, fps) + 1

    if shorts:
        # se suman frames y no segundos para que cada guide caiga justo en el
        # primer frame de su short (ver project_short.build_all_multi)
        duration_frames = sum(clip_frames(c) for c in request.clips)
        out_tc = frame_to_tc(duration_frames, fps)
    else:
        output_duration = sum(c.out - c.in_ for c in request.clips)
        duration_frames = round(output_duration * fps)
        out_tc = seconds_to_tc(output_duration, fps)

    canvas_id = "producer0"
    body = [mlt_xml.color_producer(canvas_id, out_tc)]
    chain_entries = []

    def add_chain(video):
        bin_id = next(bin_ids)
        chain_id = f"chain{bin_id}"
        length = round(video.duration * fps)
        len_tc = seconds_to_tc(video.duration, fps)
        body.append(mlt_xml.chain(
            chain_id, video.name, len_tc, length, bin_id,
            meta=chain_metadata_from_info(video),
        ))
        chain_entries.append((chain_id, len_tc))
        return chain_id, bin_id, length

    gameplay_chains, webcam_chains = {}, {}
    for m in request.media:
        gameplay_chains[m.id] = add_chain(m.file)
        if m.webcam:
            webcam_chains[m.id] = add_chain(m.webcam)

    has_webcam = bool(webcam_chains)
    gp_has_audio = any(m.file.audio for m in request.media)
    wc_has_audio = any(m.webcam and m.webcam.audio for m in request.media)

    def blank(frames):
        return mlt_xml.blank(frame_to_tc(frames, fps))

    def qtblend(x, y, w, h, opacity=1.0):
        return mlt_xml.qtblend_filter(f"filter{next(ids)}", mlt_xml.rect_value(x, y, w, h, opacity))

    gp_video_entries, wc_video_entries = [], []
    gp_audio_entries, wc_audio_entries = [], []
    blur_entries = []
    guides = []
    timeline_frame = 0
    for clip in request.clips:
        media = media_by_id[clip.mediaId]
        start, end = seconds_to_frame(clip.in_, fps), seconds_to_frame(clip.out, fps)
        in_tc, out_tc_clip = frame_to_tc(start, fps), frame_to_tc(end, fps)
        frames = clip_frames(clip)

        if shorts:
            guides.append({"frame": timeline_frame, "comment": f"Short {len(guides) + 1:02d}"})
            timeline_frame += frames
            # sin webcam el rect de la webcam no se usa: van las medidas del gameplay de relleno
            webcam_info = media.webcam or media.file
            gp_rect, wc_rect, blur_rect = project_short.short_rects(
                cfg["shorts"], width, height,
                media.file.width, media.file.height, webcam_info.width, webcam_info.height,
            )
            wc_opacity = 1.0
        else:
            gp_rect = (0, 0, width, height)
            if media.webcam:
                wc_rect = _pip_rect(cfg["long"], width, height, media.webcam.width, media.webcam.height)
            wc_opacity = cfg["long"]["webcam_opacity"]

        gp_chain_id, gp_bin_id, _ = gameplay_chains[media.id]
        gp_video_entries.append(mlt_xml.entry(in_tc, out_tc_clip, gp_chain_id, [qtblend(*gp_rect)], bin_id=gp_bin_id))
        gp_audio_entries.append(
            mlt_xml.entry(in_tc, out_tc_clip, gp_chain_id, bin_id=gp_bin_id)
            if media.file.audio else blank(frames)
        )
        if shorts and blur_cfg["enabled"]:
            blur_filters = [qtblend(*blur_rect)] + [
                mlt_xml.squareblur_filter(f"filter{next(ids)}", blur_cfg["kernel"]) for _ in range(blur_cfg["passes"])
            ]
            blur_entries.append(mlt_xml.entry(in_tc, out_tc_clip, gp_chain_id, blur_filters, bin_id=gp_bin_id))

        if not media.webcam:
            wc_video_entries.append(blank(frames))
            wc_audio_entries.append(blank(frames))
            continue

        wc_chain_id, wc_bin_id, wc_length = webcam_chains[media.id]
        before, span, after = _webcam_span(start, end, round(media.webcam.offset * fps), wc_length - 1)

        def webcam_items(filters):
            items = [blank(before)] if before else []
            if span:
                items.append(mlt_xml.entry(
                    frame_to_tc(span[0], fps), frame_to_tc(span[1], fps), wc_chain_id, filters, bin_id=wc_bin_id,
                ))
            if after:
                items.append(blank(after))
            return items

        wc_video_entries.extend(webcam_items([qtblend(*wc_rect, wc_opacity)]))
        wc_audio_entries.extend(webcam_items([]) if media.webcam.audio else [blank(frames)])

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
    if blur_entries:
        video_track_ids.append(add_track(blur_entries, "audio"))
    video_track_ids.append(add_track(gp_video_entries, "audio"))
    if has_webcam:
        video_track_ids.append(add_track(wc_video_entries, "audio"))

    sequence_uuid = "{" + str(uuid.uuid4()) + "}"
    master_id = f"tractor{next(ids)}"
    master = mlt_xml.master_tractor(
        master_id, out_tc, canvas_id, audio_track_ids, video_track_ids, sequence_uuid, titulo,
        next(bin_ids), duration_frames,
    )
    if guides:
        master.append(mlt_xml.guides_property(guides))
    body.append(master)

    document_id = str(int(time.time() * 1000))
    profile_name = mlt_xml.guess_profile_name(width, height, fps_num, fps_den)
    body.append(mlt_xml.main_bin_playlist(chain_entries, master_id, sequence_uuid, out_tc, document_id, profile_name))
    body.append(mlt_xml.project_tractor(f"tractor{next(ids)}", master_id, out_tc))

    profile_el = mlt_xml.profile_element(width, height, fps_num, fps_den, vertical=shorts)
    root = mlt_xml.build_document(profile_el, body)

    with tempfile.TemporaryDirectory(prefix="video_editor_") as tmp:
        path = os.path.join(tmp, "export.kdenlive")
        mlt_xml.write_document(root, path)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
