"""Tests de POST /api/analyze. Whisper y el clasificador (JEV) se reemplazan
por fakes (salvo en el test sin API key); los audios son silencio real
generado con ffmpeg, para que ffprobe y la duracion pasen por el camino de
verdad."""
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture
def fake_whisper(monkeypatch):
    """Reemplaza la transcripcion: el test carga en `pistas` lo que "se dijo"
    en cada pista. Guarda el output_dir que recibe para chequear que se borra."""
    pistas = {"jugador": [], "juego": [], "output_dirs": []}

    def transcribe(gameplay_path, webcam_path, cfg, titulo, output_dir=None, force=False):
        pistas["output_dirs"].append(output_dir)
        return {"jugador": list(pistas["jugador"]), "juego": list(pistas["juego"])}

    monkeypatch.setattr(transcription, "transcribe", transcribe)
    return pistas


@pytest.fixture
def fake_clasificador(monkeypatch):
    """Clasifica divertido_interesante (confianza 0.8) toda ventana que diga
    "jaja"; el resto es relleno. Guarda las ventanas que le llegan."""
    ventanas = []

    def classify(window, categories, cfg):
        ventanas.append(window)
        if "jaja" in window["texto"]:
            return {"categoria": "divertido_interesante", "razon": "se rie", "confianza": 0.8}
        return {"categoria": "relleno", "razon": "nada", "confianza": 0.9}

    monkeypatch.setitem(classification._BACKENDS, "jev", classify)
    return ventanas


def _seg(inicio, fin, texto, fuente):
    return {"inicio": inicio, "fin": fin, "texto": texto, "fuente": fuente}


def _post(client, gameplay, webcam, offset="0"):
    files = {
        "gameplay": ("gameplay.webm", gameplay, "audio/webm"),
        "webcam": ("webcam.webm", webcam, "audio/webm"),
    }
    return client.post("/api/analyze", files=files, data={"webcamOffset": offset})


def _cumple_contrato(body):
    assert set(body) == {"duration", "windows", "shorts"}
    # las ventanas pueden solaparse por el margen: solo van ordenadas por start
    anterior_start = 0.0
    for w in body["windows"]:
        assert set(w) == {"start", "end", "confidence"}
        assert anterior_start <= w["start"] < w["end"] <= body["duration"]
        anterior_start = w["start"]
    anterior_end = 0.0
    for s in body["shorts"]:
        assert set(s) == {"start", "end"}
        assert anterior_end <= s["start"] < s["end"] <= body["duration"]
        anterior_end = s["end"]


def test_correr_offset_resta_descarta_y_recorta():
    segmentos = [
        _seg(5.0, 8.0, "antes del gameplay", "jugador"),
        _seg(9.0, 12.0, "cruza el inicio", "jugador"),
        _seg(50.0, 55.0, "adentro", "jugador"),
        _seg(108.0, 115.0, "cruza el final", "jugador"),
        _seg(111.0, 120.0, "despues del gameplay", "jugador"),
    ]
    assert correr_offset(segmentos, 10.0, 100.0) == [
        _seg(0.0, 2.0, "cruza el inicio", "jugador"),
        _seg(40.0, 45.0, "adentro", "jugador"),
        _seg(98.0, 100.0, "cruza el final", "jugador"),
    ]


def test_correr_offset_negativo():
    assert correr_offset([_seg(10.0, 12.0, "hola", "jugador")], -5.0, 100.0) == [
        _seg(15.0, 17.0, "hola", "jugador"),
    ]


def test_ventanas_candidatas_con_margen_y_confianza(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [
        _seg(100.0, 105.0, "jaja que paso", "jugador"),
        _seg(130.0, 131.0, "bueno nada", "jugador"),
    ]
    fake_whisper["juego"] = [_seg(150.0, 152.0, "te estaba esperando", "juego")]

    response = _post(client, audio, audio)

    assert response.status_code == 200
    body = response.json()
    _cumple_contrato(body)
    assert body["duration"] == pytest.approx(DURACION, abs=0.1)
    # el relleno sin dialogo del juego (130-131) no se manda; el del juego si, siempre visible
    assert body["windows"] == [
        {"start": 98.0, "end": 107.0, "confidence": 0.8},
        {"start": 148.0, "end": 154.0, "confidence": None},
    ]


def test_divertido_con_dialogo_del_juego_es_siempre_visible(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja", "jugador")]
    fake_whisper["juego"] = [_seg(106.0, 108.0, "corre", "juego")]

    body = _post(client, audio, audio).json()

    assert body["windows"] == [{"start": 98.0, "end": 110.0, "confidence": None}]


def test_ventanas_se_pueden_solapar_y_el_margen_respeta_los_bordes(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [
        _seg(1.0, 3.0, "jaja", "jugador"),
        _seg(14.0, 16.0, "jaja", "jugador"),
        _seg(197.0, 199.5, "jaja", "jugador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    starts_ends = [(w["start"], w["end"]) for w in body["windows"]]
    assert starts_ends[:2] == [(0.0, 5.0), (12.0, 18.0)]
    assert starts_ends[2][1] == pytest.approx(body["duration"])


def test_ollama_sin_confianza_cuenta_como_segura(client, audio, fake_whisper, monkeypatch):
    monkeypatch.setitem(
        classification._BACKENDS, "jev",
        lambda window, categories, cfg: {"categoria": "divertido_interesante", "razon": "se rie"},
    )
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja", "jugador")]

    assert _post(client, audio, audio).json()["windows"] == [{"start": 98.0, "end": 107.0, "confidence": 1.0}]


def test_shorts_alrededor_de_cada_momento(client, audio, fake_whisper, fake_clasificador):
    # el medio de la evidencia (102.5 y 182.5) +-18 s; el segundo se recorta al final del video
    fake_whisper["jugador"] = [
        _seg(100.0, 105.0, "jaja que paso", "jugador"),
        _seg(180.0, 185.0, "jaja otra vez", "jugador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["shorts"] == [{"start": 84.5, "end": 120.5}, {"start": 164.5, "end": body["duration"]}]


def test_shorts_cercanos_se_fusionan(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [
        _seg(40.0, 41.0, "jaja", "jugador"),
        _seg(55.0, 56.0, "jaja", "jugador"),
    ]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert len(fake_clasificador) == 2
    assert body["shorts"] == [{"start": 22.5, "end": 73.5}]


def test_sin_momentos_no_hay_shorts(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["juego"] = [_seg(150.0, 152.0, "te estaba esperando", "juego")]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["shorts"] == []


def test_offset_mueve_la_webcam_al_tiempo_del_gameplay(client, audio, fake_whisper, fake_clasificador):
    # en la webcam la frase esta en w=100; con offset 30 cae en 70 del gameplay
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja que paso", "jugador")]

    response = _post(client, audio, audio, offset="30")

    assert response.status_code == 200
    [ventana] = fake_clasificador
    assert (ventana["inicio"], ventana["fin"]) == (70.0, 75.0)
    assert response.json()["windows"] == [{"start": 68.0, "end": 77.0, "confidence": 0.8}]
    assert response.json()["shorts"] == [{"start": 54.5, "end": 90.5}]


def test_sin_nada_que_conservar_no_hay_ventanas(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [_seg(10.0, 12.0, "hola hola", "jugador")]

    body = _post(client, audio, audio).json()

    _cumple_contrato(body)
    assert body["windows"] == []


def test_no_deja_nada_en_disco(client, audio, fake_whisper, fake_clasificador):
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja", "jugador")]

    assert _post(client, audio, audio).status_code == 200
    assert _post(client, audio, b"no es audio").status_code == 400

    assert len(fake_whisper["output_dirs"]) == 1
    assert not os.path.exists(fake_whisper["output_dirs"][0])


def test_debug_output_dir_guarda_cada_corrida(client, audio, fake_whisper, fake_clasificador, monkeypatch, tmp_path):
    monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja", "jugador")]

    assert _post(client, audio, audio).status_code == 200
    assert _post(client, audio, audio).status_code == 200

    carpetas = sorted(tmp_path.iterdir())
    assert len(carpetas) == 2
    assert [str(c) for c in carpetas] == sorted(fake_whisper["output_dirs"])
    for carpeta in carpetas:
        assert (carpeta / "analisis.clasificacion.json").exists()
    # cada corrida clasifica de cero: la cache de la anterior no se reutiliza
    assert len(fake_clasificador) == 2


@pytest.mark.parametrize("data, files", [
    ({}, ("gameplay", "webcam")),
    ({"webcamOffset": "0"}, ("gameplay",)),
    ({"webcamOffset": "abc"}, ("gameplay", "webcam")),
    ({"webcamOffset": "nan"}, ("gameplay", "webcam")),
])
def test_faltan_datos(client, audio, data, files):
    response = client.post(
        "/api/analyze", data=data,
        files={name: (f"{name}.webm", audio, "audio/webm") for name in files},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "Faltan datos para analizar el video."}


def test_gameplay_que_no_es_audio(client, audio, fake_whisper):
    response = _post(client, b"esto no es un audio", audio)
    assert response.status_code == 400
    assert response.json() == {"error": "No se pudo leer el audio del gameplay."}
    assert fake_whisper["output_dirs"] == []


def test_webcam_que_no_es_audio(client, audio, fake_whisper):
    response = _post(client, audio, b"esto no es un audio")
    assert response.status_code == 400
    assert response.json() == {"error": "No se pudo leer el audio de la webcam."}


def test_sin_api_key_de_jev(client, audio, fake_whisper, monkeypatch, capsys):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    fake_whisper["jugador"] = [_seg(100.0, 105.0, "jaja", "jugador")]

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
