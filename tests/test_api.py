"""Tests de POST /api/analyze. Whisper y el clasificador (JEV) se reemplazan
por fakes (salvo en el test sin API key); los audios son silencio real
generado con ffmpeg, para que ffprobe y la duracion pasen por el camino de
verdad."""
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import analyze
from app.analyze import correr_offset
from app.main import app as fastapi_app
from gameplay_editor import classification, transcription

DURACION = 200.0


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    path = tmp_path_factory.mktemp("audio") / "silencio.webm"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", str(DURACION), "-c:a", "libopus", str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture
def client():
    return TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def sin_debug(monkeypatch):
    # app.main carga el .env, que puede definir DEBUG_OUTPUT_DIR
    monkeypatch.delenv("DEBUG_OUTPUT_DIR", raising=False)


@pytest.fixture(autouse=True)
def cache(monkeypatch, tmp_path_factory):
    """Cache vacia en cada test: todos usan el mismo audio."""
    path = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(analyze, "_CACHE_DIR", str(path))
    return path


GAMEPLAY = {
    "contentType": "gameplay",
    "description": "Partida de Resident Evil 4",
    "categories": [
        {"label": "Divertido", "description": "se rie", "keep": True},
        {"label": "Narrativa / cinemática", "description": "cinematica", "keep": True},
        {"label": "Neutro", "description": "", "keep": False},
        {"label": "Relleno", "description": "nada", "keep": False},
    ],
}


@pytest.fixture
def fake_whisper(monkeypatch):
    """Reemplaza la transcripcion: el test carga en `pistas` lo que "se dijo"
    en cada pista. Guarda el output_dir que recibe para chequear que se borra."""
    pistas = {"creador": [], "contenido": [], "output_dirs": []}

    def transcribe(main_path, secondary_path, cfg, titulo, output_dir=None, force=False):
        pistas["output_dirs"].append(output_dir)
        creador = None if secondary_path is None else list(pistas["creador"])
        return {"creador": creador, "contenido": list(pistas["contenido"])}

    monkeypatch.setattr(transcription, "transcribe", transcribe)
    return pistas


@pytest.fixture
def fake_clasificador(monkeypatch):
    """Clasifica Divertido (confianza 0.8) toda ventana que diga "jaja",
    Narrativa (0.7) la que diga "cinematica" y el resto Relleno. Guarda las
    ventanas que le llegan."""
    ventanas = []

    def classify(window, settings, dos_videos, cfg):
        ventanas.append(window)
        if "jaja" in window["texto"]:
            return {"categoria": "Divertido", "razon": "se rie", "confianza": 0.8}
        if "cinematica" in window["texto"]:
            return {"categoria": "Narrativa / cinemática", "razon": "historia", "confianza": 0.7}
        return {"categoria": "Relleno", "razon": "nada", "confianza": 0.9}

    monkeypatch.setitem(classification._BACKENDS, "jev", classify)
    return ventanas


def _seg(inicio, fin, texto, fuente):
    return {"inicio": inicio, "fin": fin, "texto": texto, "fuente": fuente}


def _post(client, main, secondary=None, offset="0", settings=None):
    files = {"main": ("main.webm", main, "audio/webm")}
    data = {"settings": json.dumps(settings or GAMEPLAY)}
    if secondary is not None:
        files["secondary"] = ("secondary.webm", secondary, "audio/webm")
        data["secondaryOffset"] = offset
    return client.post("/api/analyze", files=files, data=data)


def _ventana(start, end, category="Divertido", confidence=0.8):
    return {"start": start, "end": end, "category": category, "confidence": confidence}


def _cumple_contrato(body):
    assert set(body) == {"duration", "windows", "shorts"}
    # pueden solaparse por el margen: solo van ordenadas por start
    for lista in (body["windows"], body["shorts"]):
        anterior_start = 0.0
        for w in lista:
            assert set(w) == {"start", "end", "category", "confidence"}
            assert anterior_start <= w["start"] < w["end"] <= body["duration"]
            assert 0 <= w["confidence"] <= 1
            anterior_start = w["start"]


def test_correr_offset_resta_descarta_y_recorta():
    segmentos = [
        _seg(5.0, 8.0, "antes del gameplay", "creador"),
        _seg(9.0, 12.0, "cruza el inicio", "creador"),
        _seg(50.0, 55.0, "adentro", "creador"),
        _seg(108.0, 115.0, "cruza el final", "creador"),
        _seg(111.0, 120.0, "despues del gameplay", "creador"),
    ]
    assert correr_offset(segmentos, 10.0, 100.0) == [
        _seg(0.0, 2.0, "cruza el inicio", "creador"),
        _seg(40.0, 45.0, "adentro", "creador"),
        _seg(98.0, 100.0, "cruza el final", "creador"),
    ]


def test_correr_offset_negativo():
    assert correr_offset([_seg(10.0, 12.0, "hola", "creador")], -5.0, 100.0) == [
        _seg(15.0, 17.0, "hola", "creador"),
    ]


def test_ventanas_candidatas_con_margen_categoria_y_confianza(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [
        _seg(100.0, 105.0, "jaja que paso", "creador"),
        _seg(130.0, 131.0, "bueno nada", "creador"),
    ]
    fake_whisper["contenido"] = [_seg(150.0, 152.0, "cinematica te estaba esperando", "contenido")]

    response = _post(client, audio, audio)

    assert response.status_code == 200
    body = response.json()
    _cumple_contrato(body)
    assert body["duration"] == pytest.approx(DURACION, abs=0.1)
    # el relleno (130-131) no se manda
    assert body["windows"] == [
        _ventana(98.0, 107.0),
        _ventana(148.0, 154.0, "Narrativa / cinemática", 0.7),
    ]


def test_las_categorias_en_descartar_no_se_mandan(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]
    fake_whisper["contenido"] = [_seg(150.0, 152.0, "cinematica", "contenido")]
    sin_narrativa = {
        **GAMEPLAY,
        "categories": [{**c, "keep": c["label"] == "Divertido"} for c in GAMEPLAY["categories"]],
    }

    body = _post(client, audio, audio, settings=sin_narrativa).json()

    assert body["windows"] == [_ventana(98.0, 107.0)]


def test_ventanas_se_pueden_solapar_y_el_margen_respeta_los_bordes(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [
        _seg(1.0, 3.0, "jaja", "creador"),
        _seg(14.0, 16.0, "jaja", "creador"),
        _seg(197.0, 199.5, "jaja", "creador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    starts_ends = [(w["start"], w["end"]) for w in body["windows"]]
    assert starts_ends[:2] == [(0.0, 5.0), (12.0, 18.0)]
    assert starts_ends[2][1] == pytest.approx(body["duration"])


def test_ollama_sin_confianza_cuenta_como_segura(client, audio, fake_whisper, monkeypatch):
    monkeypatch.setitem(
        classification._BACKENDS, "jev",
        lambda window, settings, dos_videos, cfg: {"categoria": "Divertido", "razon": "se rie"},
    )
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    assert _post(client, audio, audio).json()["windows"] == [_ventana(98.0, 107.0, confidence=1.0)]


def test_un_short_por_ventana_conservada(client, audio, fake_whisper, fake_clasificador):
    # mismo margen de 2 s que el video largo; el segundo se recorta al final del video
    fake_whisper["creador"] = [
        _seg(100.0, 105.0, "jaja que paso", "creador"),
        _seg(195.0, 199.0, "jaja otra vez", "creador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["shorts"] == [_ventana(98.0, 107.0), _ventana(193.0, body["duration"])]


def test_shorts_cercanos_no_se_fusionan(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [
        _seg(40.0, 41.0, "jaja", "creador"),
        _seg(52.0, 53.0, "jaja", "creador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert len(fake_clasificador) == 2
    assert body["shorts"] == [_ventana(38.0, 43.0), _ventana(50.0, 55.0)]


def test_el_contenido_solo_va_al_clasificador_pero_no_es_short(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["contenido"] = [_seg(100.0, 105.0, "cinematica te estaba esperando", "contenido")]

    body = _post(client, audio, audio).json()

    [ventana] = fake_clasificador
    assert ventana["texto"] == "[audio del contenido] cinematica te estaba esperando"
    assert body["windows"] == [_ventana(98.0, 107.0, "Narrativa / cinemática", 0.7)]
    assert body["shorts"] == []


def test_con_un_solo_video_cualquier_ventana_conservada_es_short(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(10.0, 12.0, "jaja no se usa", "creador")]
    fake_whisper["contenido"] = [_seg(100.0, 105.0, "jaja que paso", "contenido")]

    response = _post(client, audio)

    assert response.status_code == 200
    body = response.json()
    _cumple_contrato(body)
    [ventana] = fake_clasificador
    assert ventana["texto"] == "jaja que paso"
    assert body["windows"] == [_ventana(98.0, 107.0)]
    assert body["shorts"] == [_ventana(98.0, 107.0)]


def test_sin_momentos_no_hay_shorts(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["contenido"] = [_seg(150.0, 152.0, "te estaba esperando", "contenido")]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["shorts"] == []


def test_offset_mueve_la_webcam_al_tiempo_del_gameplay(client, audio, fake_whisper, fake_clasificador):
    # en la webcam la frase esta en w=100; con offset 30 cae en 70 del gameplay
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja que paso", "creador")]

    response = _post(client, audio, audio, offset="30")

    assert response.status_code == 200
    [ventana] = fake_clasificador
    assert (ventana["inicio"], ventana["fin"]) == (70.0, 75.0)
    assert response.json()["windows"] == [_ventana(68.0, 77.0)]
    assert response.json()["shorts"] == [_ventana(68.0, 77.0)]


def test_sin_nada_que_conservar_no_hay_ventanas(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(10.0, 12.0, "hola hola", "creador")]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["windows"] == []


def test_no_deja_nada_en_disco_salvo_la_cache(client, audio, fake_whisper, fake_clasificador, cache):
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    assert _post(client, audio, audio).status_code == 200
    assert _post(client, audio, b"no es audio").status_code == 400

    assert len(fake_whisper["output_dirs"]) == 1
    assert not os.path.exists(fake_whisper["output_dirs"][0])
    assert sorted(p.name.split(".", 1)[1] for p in cache.iterdir()) == [
        "clasificacion.json", "transcripcion.json",
    ]


def test_reutiliza_la_transcripcion_de_los_mismos_audios(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    primera = _post(client, audio, audio).json()
    segunda = _post(client, audio, audio, offset="30").json()

    assert len(fake_whisper["output_dirs"]) == 1
    # el offset cambia el texto de la ventana: se clasifica de nuevo
    assert len(fake_clasificador) == 2
    # la transcripcion se guarda sin el offset: se aplica en cada analisis
    assert primera["windows"] == [_ventana(98.0, 107.0)]
    assert segunda["windows"] == [_ventana(68.0, 77.0)]


def test_reutiliza_la_clasificacion_con_el_mismo_offset(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    primera = _post(client, audio, audio, offset="30").json()
    segunda = _post(client, audio, audio, offset="30").json()

    assert len(fake_clasificador) == 1
    assert primera == segunda


def test_otras_categorias_se_clasifican_de_nuevo(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    _post(client, audio, audio)
    _post(client, audio, audio, settings={**GAMEPLAY, "description": "otra cosa"})

    assert len(fake_clasificador) == 2


def test_otros_audios_se_transcriben_de_nuevo(client, audio, fake_whisper, fake_clasificador, tmp_path):
    otro = tmp_path / "otro.webm"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", "150", "-c:a", "libopus", str(otro)],
        check=True,
    )

    _post(client, audio, audio)
    _post(client, otro.read_bytes(), audio)

    assert len(fake_whisper["output_dirs"]) == 2


def test_debug_output_dir_guarda_cada_corrida(client, audio, fake_whisper, fake_clasificador, monkeypatch, tmp_path):
    monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    assert _post(client, audio, audio).status_code == 200
    assert _post(client, audio, audio).status_code == 200

    carpetas = sorted(tmp_path.iterdir())
    assert len(carpetas) == 2
    assert [str(carpetas[0])] == fake_whisper["output_dirs"]
    for carpeta in carpetas:
        assert {p.name for p in carpeta.iterdir()} >= {"main", "secondary"}


@pytest.mark.parametrize("data, files", [
    ({}, ("main",)),
    ({"settings": json.dumps(GAMEPLAY)}, ()),
    ({"settings": json.dumps(GAMEPLAY)}, ("main", "secondary")),
    ({"settings": json.dumps(GAMEPLAY), "secondaryOffset": "0"}, ("main",)),
    ({"settings": json.dumps(GAMEPLAY), "secondaryOffset": "abc"}, ("main", "secondary")),
    ({"settings": json.dumps(GAMEPLAY), "secondaryOffset": "nan"}, ("main", "secondary")),
])
def test_faltan_datos(client, audio, data, files):
    response = client.post(
        "/api/analyze", data=data,
        files={name: (f"{name}.webm", audio, "audio/webm") for name in files},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "Faltan datos para analizar el video."}


def _categorias(*labels_keep):
    return {**GAMEPLAY, "categories": [{"label": l, "description": "", "keep": k} for l, k in labels_keep]}


@pytest.mark.parametrize("settings", [
    "{roto",
    json.dumps({**GAMEPLAY, "contentType": "pelicula"}),
    json.dumps({**GAMEPLAY, "description": "x" * 201}),
    json.dumps(_categorias(("Relleno", False))),
    json.dumps(_categorias(*[(f"c{i}", True) for i in range(6)], ("Relleno", False))),
    json.dumps(_categorias(("Divertido", True), ("divertido", True), ("Relleno", False))),
    json.dumps(_categorias((" ", True), ("Relleno", False))),
    json.dumps(_categorias(("x" * 41, True), ("Relleno", False))),
    json.dumps(_categorias(("Divertido", True), ("Neutro", False))),
    json.dumps(_categorias(("Divertido", False), ("Relleno", False))),
])
def test_settings_invalido(client, audio, fake_whisper, settings):
    response = client.post(
        "/api/analyze", data={"settings": settings},
        files={"main": ("main.webm", audio, "audio/webm")},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "Faltan datos para analizar el video."}
    assert fake_whisper["output_dirs"] == []


def test_video_que_no_es_audio(client, audio, fake_whisper):
    response = _post(client, b"esto no es un audio", audio)
    assert response.status_code == 400
    assert response.json() == {"error": "No se pudo leer el audio del video."}
    assert fake_whisper["output_dirs"] == []


def test_camara_que_no_es_audio(client, audio, fake_whisper):
    response = _post(client, audio, b"esto no es un audio")
    assert response.status_code == 400
    assert response.json() == {"error": "No se pudo leer el audio de la cámara."}


def test_sin_api_key_de_jev(client, audio, fake_whisper, monkeypatch, capsys):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    fake_whisper["creador"] = [_seg(100.0, 105.0, "jaja", "creador")]

    response = _post(client, audio, audio)

    assert response.status_code == 502
    assert response.json() == {"error": "No se pudo conectar con el clasificador."}
    assert "Falta JEV_API_KEY" in capsys.readouterr().out


def test_cors_para_el_frontend(client):
    response = client.options("/api/analyze", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
    })
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
