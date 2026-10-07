"""Tests de project_short: una sola secuencia con todos los shorts seguidos y
una guide al inicio de cada uno. Se generan videos reales con ffmpeg porque
project_short lee la metadata de los chains con ffprobe."""
import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

from gameplay_editor import ffmpeg_utils, project_short
from gameplay_editor.config import load_config

FPS = 30


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    folder = tmp_path_factory.mktemp("videos")
    paths = {}
    for name, size, audio in [
        ("gameplay1", "1920x1080", True), ("webcam1", "640x360", True),
        ("gameplay2", "1280x720", False), ("webcam2", "640x480", True),
    ]:
        paths[name] = str(folder / f"{name}.mp4")
        audio_args = ["-f", "lavfi", "-i", "sine=sample_rate=48000", "-c:a", "aac"] if audio else []
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={FPS}", *audio_args,
             "-t", "20", "-c:v", "mpeg4", paths[name]],
            check=True,
        )
    return paths


@pytest.fixture
def cfg():
    cfg = load_config()
    cfg["shorts"].update({"pre_roll_sec": 2.0, "post_roll_sec": 2.0, "merge_gap_sec": 1.0})
    return cfg


def _session(videos, n, highlights):
    gp, wc = videos[f"gameplay{n}"], videos[f"webcam{n}"]
    gp_info = ffmpeg_utils.video_info(gp)
    return {"gameplay": gp, "webcam": wc, "analysis": {
        "duration": gp_info["duration"], "gameplay_info": gp_info, "webcam_info": ffmpeg_utils.video_info(wc),
        "keep_segments": [], "highlights": [{"time": t, "score": 1.0} for t in highlights],
    }}


def _prop(el, name):
    found = el.find(f"property[@name='{name}']")
    return None if found is None else found.text


def _secuencias(path):
    root = ET.parse(path).getroot()
    return [el for el in root if el.tag == "tractor" and _prop(el, "kdenlive:uuid")]


def _pistas(path):
    """Por cada pista de la secuencia (sin el canvas): (es_audio, [(tag, in, out, resource)])."""
    root = ET.parse(path).getroot()
    by_id = {el.get("id"): el for el in root}
    [master] = _secuencias(path)
    pistas = []
    for track in master.findall("track")[1:]:
        tractor = by_id[track.get("producer")]
        playlist = by_id[tractor.find("track").get("producer")]
        items = [
            ("blank", item.get("length")) if item.tag == "blank"
            else ("entry", item.get("in"), item.get("out"), _prop(by_id[item.get("producer")], "resource"))
            for item in playlist
        ]
        pistas.append((_prop(tractor, "kdenlive:audio_track") == "1", items))
    return pistas


def test_una_secuencia_con_una_guide_por_short(videos, cfg, tmp_path):
    s = _session(videos, 1, [3.0, 10.0])
    path = project_short.build_all(s["gameplay"], s["webcam"], cfg, s["analysis"], "partida", str(tmp_path))

    [master] = _secuencias(path)
    assert _prop(master, "kdenlive:clipname") == "partida_shorts"
    # [1, 5] son 121 frames (out inclusivo) y [8, 12] otros 121
    assert [(g["pos"], g["comment"]) for g in json.loads(_prop(master, "kdenlive:sequenceproperties.guides"))] == [
        (0, "Short 01"), (121, "Short 02"),
    ]
    assert _prop(master, "kdenlive:maxduration") == "242"
    root = ET.parse(path).getroot()
    main_bin = root.find("playlist[@id='main_bin']")
    assert [e.get("producer") for e in main_bin.findall("entry")][-1] == master.get("id")
    assert root.find("profile").get("height") == "1920"


def test_varias_sesiones_en_la_misma_secuencia(videos, cfg, tmp_path):
    sessions = [_session(videos, 1, [3.0]), _session(videos, 2, [5.0, 15.0])]

    path = project_short.build_all_multi(sessions, cfg, "partida", str(tmp_path))

    [master] = _secuencias(path)
    guides = json.loads(_prop(master, "kdenlive:sequenceproperties.guides"))
    assert [(g["pos"], g["comment"]) for g in guides] == [(0, "Short 01"), (121, "Short 02"), (242, "Short 03")]
    a_gp, a_wc, v_gp, v_wc = _pistas(path)
    assert [e[1:] for e in v_gp[1]] == [
        ("00:00:01.000", "00:00:05.000", videos["gameplay1"]),
        ("00:00:03.000", "00:00:07.000", videos["gameplay2"]),
        ("00:00:13.000", "00:00:17.000", videos["gameplay2"]),
    ]
    assert [e[3] for e in v_wc[1]] == [videos["webcam1"], videos["webcam2"], videos["webcam2"]]
    # el gameplay2 no tiene audio: blank para no correr los shorts siguientes
    assert a_gp == (True, [v_gp[1][0], ("blank", "00:00:04.033"), ("blank", "00:00:04.033")])
    assert [e[1:3] for e in a_wc[1]] == [e[1:3] for e in v_wc[1]]


def test_sesion_sin_momentos_no_aporta_shorts(videos, cfg, tmp_path):
    path = project_short.build_all_multi(
        [_session(videos, 1, []), _session(videos, 2, [5.0])], cfg, "partida", str(tmp_path),
    )
    # solo cuenta el audio de las sesiones con shorts: el gameplay2 no tiene
    pistas = _pistas(path)
    assert [es_audio for es_audio, _ in pistas] == [True, False, False]
    assert [e[3] for e in pistas[1][1]] == [videos["gameplay2"]]


def test_sin_momentos_no_escribe_nada(videos, cfg, tmp_path):
    assert project_short.build_all_multi([_session(videos, 1, [])], cfg, "partida", str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
