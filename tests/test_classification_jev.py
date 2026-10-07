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
        "type": "choice", "choice": "relleno", "confidence": 0.91,
        "probabilities": {"divertido_interesante": 0.04, "relleno": 0.93, "neutro": 0.03},
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
        "inicio": 45.0, "fin": 90.0, "texto": "[jugador] jaja que paso", "duracion_hablada": 12.3,
        "contexto_antes": contexto_antes, "contexto_despues": contexto_despues,
        "silencio_antes": 30.0, "silencio_despues": 12.5,
    }


def test_manda_el_request_de_la_spec(jev, cfg):
    jev["respuestas"] = [RESPUESTA_OK]

    resultado = classification.classify_window(_ventana("[juego] antes", "[jugador] despues"), cfg)

    assert resultado == {"categoria": "relleno", "razon": "confianza 0.91"}
    [req] = jev["requests"]
    assert req.full_url == "https://api.typesafe.ai/v1/systemone"
    assert req.get_header("Authorization") == "Bearer ts_test"
    body = json.loads(req.data)
    assert body["model"] == "jev-latest"
    assert body["state"] == {
        "duracion_tramo_seg": 45.0,
        "habla_real_seg": 12.3,
        "silencio_antes_seg": 30.0,
        "silencio_despues_seg": 12.5,
        "contexto_antes": "[juego] antes",
        "tramo_a_clasificar": "[jugador] jaja que paso",
        "contexto_despues": "[jugador] despues",
    }
    pregunta = body["questions"]["categoria"]
    assert pregunta["type"] == "choice"
    assert pregunta["criteria"] == classification._CATEGORY_DESCRIPTIONS
    assert pregunta["instructions"].endswith(
        "Tene en cuenta `silencio_antes_seg` y `silencio_despues_seg`: una frase corta con mucho "
        "silencio antes y despues no es lo mismo que la misma frase en medio de una charla."
    )


def test_omite_contextos_vacios(jev, cfg):
    jev["respuestas"] = [RESPUESTA_OK]
    classification.classify_window(_ventana(), cfg)
    assert set(json.loads(jev["requests"][0].data)["state"]) == {
        "duracion_tramo_seg", "habla_real_seg", "silencio_antes_seg", "silencio_despues_seg", "tramo_a_clasificar",
    }


def test_ventana_sin_texto_no_llama_a_jev(jev, cfg):
    resultado = classification.classify_window({**_ventana(), "texto": None}, cfg)
    assert resultado["categoria"] == classification.SIN_TRANSCRIPCION
    assert jev["requests"] == []


@pytest.mark.parametrize("error", [
    _http_error(429), _http_error(529), _http_error(503), urllib.error.URLError("sin red"), TimeoutError(),
])
def test_reintenta_y_termina_normal(jev, cfg, error):
    jev["respuestas"] = [error, RESPUESTA_OK]
    assert classification.classify_window(_ventana(), cfg)["categoria"] == "relleno"
    assert len(jev["requests"]) == 2


@pytest.mark.parametrize("code", [401, 422])
def test_no_reintenta_errores_del_cliente(jev, cfg, code):
    jev["respuestas"] = [_http_error(code)]
    with pytest.raises(RuntimeError, match=f'JEV respondio {code}: {{"detail": "algo"}}'):
        classification.classify_window(_ventana(), cfg)
    assert len(jev["requests"]) == 1


def test_agota_los_reintentos(jev, cfg):
    jev["respuestas"] = [_http_error(529)] * cfg["retries"]
    with pytest.raises(RuntimeError, match="No se pudo contactar a JEV"):
        classification.classify_window(_ventana(), cfg)
    assert len(jev["requests"]) == cfg["retries"]


def test_sin_api_key(jev, cfg, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY")
    with pytest.raises(RuntimeError, match="Falta JEV_API_KEY en el .env del backend"):
        classification.classify_window(_ventana(), cfg)
    assert jev["requests"] == []


def test_prompt_de_ollama_con_el_silencio_alrededor():
    prompt = classification._build_prompt(_ventana(), ["relleno"])
    assert "Antes de este tramo hubo 30s sin dialogo y despues 12s." in prompt
