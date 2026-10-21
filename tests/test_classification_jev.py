"""Tests de _classify_jev con urlopen reemplazado: lo que se manda a JEV, como
se lee la respuesta y que errores se reintentan."""
import io
import json
import urllib.error

import pytest

from gameplay_editor import classification
from gameplay_editor.config import load_config

RESPUESTA_OK = {
    "model": "jev-1.13.0",
    "answers": {"categoria": {
        "type": "choice", "choice": "Relleno", "confidence": 0.91,
        "probabilities": {"Divertido": 0.04, "Relleno": 0.93, "Consejos": 0.03},
    }},
    "usage": {"input_tokens": 812, "output_tokens": 34},
}


class _Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError(
        classification._JEV_URL, code, "error", {}, io.BytesIO(b'{"detail": "algo"}'),
    )


SETTINGS = {
    "contentType": "gameplay",
    "description": "Partida de Resident Evil 4 con amigos",
    "categories": [
        {"label": "Divertido", "description": "algo gracioso", "keep": True},
        {"label": "Consejos", "description": "", "keep": True},
        {"label": "Relleno", "description": "nada", "keep": False},
    ],
}


def _clasificar(ventana, cfg, settings=SETTINGS, dos_videos=True):
    return classification.classify_window(ventana, settings, dos_videos, cfg)


@pytest.fixture
def cfg():
    return {**load_config()["classification"], "retry_backoff_sec": 0}


@pytest.fixture
def jev(monkeypatch):
    """Cola de respuestas de JEV: cada llamada saca la primera (un dict es
    un 200, una excepcion se lanza). Guarda los requests que recibe."""
    estado = {"respuestas": [], "requests": []}

    def urlopen(req, timeout):
        estado["requests"].append(req)
        respuesta = estado["respuestas"].pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return _Respuesta(json.dumps(respuesta).encode("utf-8"))

    monkeypatch.setenv("JEV_API_KEY", "ts_test")
    monkeypatch.setattr(classification.urllib.request, "urlopen", urlopen)
    return estado


def _ventana(contexto_antes="", contexto_despues=""):
    return {
        "inicio": 45.0, "fin": 90.0, "texto": "[voz del creador] jaja que paso", "duracion_hablada": 12.3,
        "tiene_voz_creador": True,
        "contexto_antes": contexto_antes, "contexto_despues": contexto_despues,
        "silencio_antes": 30.0, "silencio_despues": 12.5,
    }


def test_manda_el_request_de_la_spec(jev, cfg):
    jev["respuestas"] = [RESPUESTA_OK]

    resultado = _clasificar(_ventana("[audio del contenido] antes", "[voz del creador] despues"), cfg)

    assert resultado == {"categoria": "Relleno", "razon": "confianza 0.91", "confianza": 0.91}
    [req] = jev["requests"]
    assert req.full_url == "https://api.typesafe.ai/v1/systemone"
    assert req.get_header("Authorization") == "Bearer ts_test"
    body = json.loads(req.data)
    assert body["model"] == "jev-latest"
    assert body["state"] == {
        "tipo_de_contenido": "gameplay",
        "descripcion_del_video": "Partida de Resident Evil 4 con amigos",
        "duracion_tramo_seg": 45.0,
        "habla_real_seg": 12.3,
        "silencio_antes_seg": 30.0,
        "silencio_despues_seg": 12.5,
        "contexto_antes": "[audio del contenido] antes",
        "tramo_a_clasificar": "[voz del creador] jaja que paso",
        "contexto_despues": "[voz del creador] despues",
    }
    pregunta = body["questions"]["categoria"]
    assert pregunta["type"] == "choice"
    # las que agrego el usuario (descripcion vacia) van con el label
    assert pregunta["criteria"] == {"Divertido": "algo gracioso", "Consejos": "Consejos", "Relleno": "nada"}
    assert pregunta["instructions"] == (
        classification._JEV_INSTRUCTIONS + classification._JEV_INSTRUCTIONS_DOS_VIDEOS
    )
    assert pregunta["instructions"].startswith("Sos un editor de video.")
    assert pregunta["instructions"].endswith("(el juego, la película, otra persona).")


def test_con_un_solo_video_sin_explicar_las_etiquetas(jev, cfg):
    jev["respuestas"] = [RESPUESTA_OK]
    _clasificar(_ventana(), cfg, dos_videos=False)
    assert json.loads(jev["requests"][0].data)["questions"]["categoria"]["instructions"] == (
        classification._JEV_INSTRUCTIONS
    )


def test_omite_contextos_y_descripcion_vacios(jev, cfg):
    jev["respuestas"] = [RESPUESTA_OK]
    _clasificar(_ventana(), cfg, {**SETTINGS, "description": ""})
    assert set(json.loads(jev["requests"][0].data)["state"]) == {
        "tipo_de_contenido", "duracion_tramo_seg", "habla_real_seg", "silencio_antes_seg", "silencio_despues_seg", "tramo_a_clasificar",
    }


def test_ventana_sin_texto_no_llama_a_jev(jev, cfg):
    resultado = _clasificar({**_ventana(), "texto": None}, cfg)
    assert resultado["categoria"] == classification.SIN_TRANSCRIPCION
    assert jev["requests"] == []


@pytest.mark.parametrize("error", [
    _http_error(429), _http_error(529), _http_error(503), urllib.error.URLError("sin red"), TimeoutError(),
])
def test_reintenta_y_termina_normal(jev, cfg, error):
    jev["respuestas"] = [error, RESPUESTA_OK]
    assert _clasificar(_ventana(), cfg)["categoria"] == "Relleno"
    assert len(jev["requests"]) == 2


@pytest.mark.parametrize("code", [401, 422])
def test_no_reintenta_errores_del_cliente(jev, cfg, code):
    jev["respuestas"] = [_http_error(code)]
    with pytest.raises(RuntimeError, match=f'JEV respondio {code}: {{"detail": "algo"}}'):
        _clasificar(_ventana(), cfg)
    assert len(jev["requests"]) == 1


def test_agota_los_reintentos(jev, cfg):
    jev["respuestas"] = [_http_error(529)] * cfg["retries"]
    with pytest.raises(RuntimeError, match="No se pudo contactar a JEV"):
        _clasificar(_ventana(), cfg)
    assert len(jev["requests"]) == cfg["retries"]


def test_sin_api_key(jev, cfg, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY")
    with pytest.raises(RuntimeError, match="Falta JEV_API_KEY en el .env del backend"):
        _clasificar(_ventana(), cfg)
    assert jev["requests"] == []


def test_prompt_de_ollama_con_el_silencio_alrededor():
    prompt = classification._build_prompt(_ventana(), ["relleno"])
    assert prompt.startswith(classification._EDITOR_PROMPT)
    assert "Antes de este tramo hubo 30s sin dialogo y despues 12s." in prompt


def test_cache_vieja_sin_confianza_la_lee_de_razon(cfg, monkeypatch, tmp_path):
    transcripcion = {"creador": [{"inicio": 10.0, "fin": 12.0, "texto": "jaja", "fuente": "creador"}], "contenido": []}
    monkeypatch.setitem(
        classification._BACKENDS, cfg["backend"],
        lambda window, settings, dos_videos, cfg: {"categoria": "Divertido", "razon": "confianza 0.64"},
    )
    classification.classify(transcripcion, cfg, SETTINGS, "t", output_dir=str(tmp_path), duration=30.0)
    cache_file = tmp_path / "t.clasificacion.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    for w in cache["windows"]:
        del w["confianza"]
    cache_file.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setitem(classification._BACKENDS, cfg["backend"], None)

    clasificadas = classification.classify(transcripcion, cfg, SETTINGS, "t", output_dir=str(tmp_path), duration=30.0)

    assert [w["confianza"] for w in clasificadas] == [None, 0.64, None]
