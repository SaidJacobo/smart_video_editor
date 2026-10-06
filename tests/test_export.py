"""Tests de POST /api/export/kdenlive. Para comparar contra project_long.build
se generan videos reales con ffmpeg (project_long los lee con ffprobe)."""
import subprocess
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from gameplay_editor import ffmpeg_utils, project_long
from gameplay_editor.config import load_config

FPS = 30
AUDIO = {"sampleRate": 48000, "channels": 2}


@pytest.fixture
def client():
    return TestClient(fastapi_app)


def _video(name, duration=60.0, width=1920, height=1080, audio=AUDIO):
    return {"name": name, "duration": duration, "width": width, "height": height, "fps": FPS, "audio": audio}


def _media(id_="m1", webcam_offset=0.0, webcam_duration=60.0, webcam_audio=AUDIO, con_webcam=True):
    webcam = None
    if con_webcam:
        webcam = {**_video("webcam.mp4", webcam_duration, 640, 360, webcam_audio), "offset": webcam_offset}
    return {"id": id_, "file": _video(f"{id_}-gameplay.mp4"), "webcam": webcam}


def _clip(start, end, media_id="m1"):
    return {"mediaId": media_id, "in": start, "out": end}


def _post(client, media, clips):
    return client.post("/api/export/kdenlive", json={"media": media, "clips": clips})


def _prop(el, name):
    found = el.find(f"property[@name='{name}']")
    return None if found is None else found.text


def _pistas(xml):
    """Pistas de la secuencia (sin el canvas), en orden: cada una es
    (es_audio, [items]) con items ("blank", length) o ("entry", in, out,
    producer, rect del qtblend o None)."""
    root = ET.fromstring(xml)
    by_id = {el.get("id"): el for el in root}
    master = next(el for el in root if el.tag == "tractor" and _prop(el, "kdenlive:uuid"))
    pistas = []
    for track in master.findall("track")[1:]:
        tractor = by_id[track.get("producer")]
        playlist = by_id[tractor.find("track").get("producer")]
        items = []
        for item in playlist:
            if item.tag == "blank":
                items.append(("blank", item.get("length")))
            else:
                qtblend = item.find("filter")
                rect = None if qtblend is None else _prop(qtblend, "rect")
                items.append(("entry", item.get("in"), item.get("out"), _prop(by_id[item.get("producer")], "resource"), rect))
        pistas.append((_prop(tractor, "kdenlive:audio_track") == "1", items))
    return pistas


def test_exporta_el_timeline_en_orden(client):
    response = _post(client, [_media()], [_clip(30.0, 40.5), _clip(2.0, 10.0)])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/xml"
    a1, a2, v1, v2 = _pistas(response.text)
    assert [a1[0], a2[0], v1[0], v2[0]] == [True, True, False, False]
    assert [e[1:4] for e in v1[1]] == [
        ("00:00:30.000", "00:00:40.500", "m1-gameplay.mp4"),
        ("00:00:02.000", "00:00:10.000", "m1-gameplay.mp4"),
    ]
    assert [e[1:4] for e in a1[1]] == [e[1:4] for e in v1[1]]
    assert [e[1:4] for e in v2[1]] == [
        ("00:00:30.000", "00:00:40.500", "webcam.mp4"),
        ("00:00:02.000", "00:00:10.000", "webcam.mp4"),
    ]
    assert v1[1][0][4] == "00:00:00.000=0 0 1920 1080 1"


def test_titulo_y_perfil_del_primer_clip(client):
    root = ET.fromstring(_post(client, [_media()], [_clip(0.0, 5.0)]).text)
    profile = root.find("profile")
    assert (profile.get("width"), profile.get("height"), profile.get("frame_rate_num")) == ("1920", "1080", "30")
    master = next(el for el in root if el.tag == "tractor" and _prop(el, "kdenlive:uuid"))
    assert _prop(master, "kdenlive:clipname") == "m1-gameplay"


def test_offset_positivo_corre_la_webcam(client):
    _, a2, _, v2 = _pistas(_post(client, [_media(webcam_offset=1.5)], [_clip(10.0, 20.0)]).text)
    assert [e[:3] for e in v2[1]] == [("entry", "00:00:11.500", "00:00:21.500")]
    assert [e[:3] for e in a2[1]] == [("entry", "00:00:11.500", "00:00:21.500")]


def test_offset_negativo_deja_blank_al_principio(client):
    _, a2, _, v2 = _pistas(_post(client, [_media(webcam_offset=-2.0)], [_clip(0.0, 5.0), _clip(10.0, 12.0)]).text)
    esperado = [
        ("blank", "00:00:02.000"),
        ("entry", "00:00:00.000", "00:00:03.000"),
        ("entry", "00:00:08.000", "00:00:10.000"),
    ]
    assert [e[:3] for e in v2[1]] == esperado
    assert [e[:3] for e in a2[1]] == esperado


def test_webcam_que_termina_antes_deja_blank_al_final(client):
    # webcam de 30 s (frames 0..899): el clip [25, 35] tiene webcam hasta el frame 899
    _, _, _, v2 = _pistas(_post(client, [_media(webcam_duration=30.0)], [_clip(25.0, 35.0)]).text)
    assert [e[:3] for e in v2[1]] == [
        ("entry", "00:00:25.000", "00:00:29.967"),
        ("blank", "00:00:05.033"),
    ]


def test_clip_sin_webcam_va_como_blank(client):
    media = [_media("m1"), _media("m2", con_webcam=False)]
    _, a2, _, v2 = _pistas(_post(client, media, [_clip(0.0, 2.0, "m2"), _clip(0.0, 3.0, "m1")]).text)
    for pista in (a2, v2):
        assert [e[:3] for e in pista[1]] == [("blank", "00:00:02.033"), ("entry", "00:00:00.000", "00:00:03.000")]


def test_sin_webcam_no_hay_v2_ni_a2(client):
    pistas = _pistas(_post(client, [_media(con_webcam=False)], [_clip(0.0, 2.0)]).text)
    assert [es_audio for es_audio, _ in pistas] == [True, False]


def test_sin_audio_no_hay_pista_de_audio(client):
    pistas = _pistas(_post(client, [_media(webcam_audio=None)], [_clip(0.0, 2.0)]).text)
    assert [es_audio for es_audio, _ in pistas] == [True, False, False]


@pytest.mark.parametrize("body", [
    {"media": [_media()], "clips": []},
    {"media": [_media()]},
    {"media": [_media()], "clips": [_clip(0.0, 2.0, "otro")]},
    {"media": [_media()], "clips": [_clip(5.0, 2.0)]},
    {"media": [{**_media(), "file": {**_video("a.mp4"), "fps": "abc"}}], "clips": [_clip(0.0, 2.0)]},
])
def test_body_invalido(client, body):
    response = client.post("/api/export/kdenlive", json=body)
    assert response.status_code == 422
    assert response.json() == {"error": "Datos inválidos para exportar."}


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    folder = tmp_path_factory.mktemp("videos")
    paths = {}
    for name, size in [("gameplay.mp4", "1920x1080"), ("webcam.mp4", "640x360")]:
        paths[name] = folder / name
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={FPS}",
             "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "20", "-c:v", "mpeg4", "-c:a", "aac",
             str(paths[name])],
            check=True,
        )
    return paths


def _sin_resource(pistas):
    return [(es_audio, [item[:3] + item[4:] if item[0] == "entry" else item for item in items])
            for es_audio, items in pistas]


def test_equivalente_a_project_long(client, videos, tmp_path):
    keep = [(1.0, 4.5), (8.0, 12.0), (15.2, 19.0)]
    gp_info = ffmpeg_utils.video_info(str(videos["gameplay.mp4"]))
    wc_info = ffmpeg_utils.video_info(str(videos["webcam.mp4"]))
    analysis = {"gameplay_info": gp_info, "webcam_info": wc_info, "keep_segments": keep, "highlights": []}
    long_path = project_long.build(
        str(videos["gameplay.mp4"]), str(videos["webcam.mp4"]), load_config(), analysis, "gameplay", str(tmp_path),
    )

    def file(name, info):
        return {"name": name, "duration": info["duration"], "width": info["width"], "height": info["height"],
                "fps": info["fps"], "audio": AUDIO}

    media = [{"id": "m1", "file": file("gameplay.mp4", gp_info), "webcam": {**file("webcam.mp4", wc_info), "offset": 0}}]
    response = _post(client, media, [_clip(s, e) for s, e in keep])

    with open(long_path, encoding="utf-8") as fh:
        long_xml = fh.read()
    assert _sin_resource(_pistas(response.text)) == _sin_resource(_pistas(long_xml))
    assert ET.fromstring(response.text).find("profile").attrib == ET.fromstring(long_xml).find("profile").attrib
