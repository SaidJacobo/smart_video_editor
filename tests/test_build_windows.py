"""Tests de classification.build_windows: una ventana por tramo de habla,
cortada en silencios de silencio_corte_sec en las dos pistas, y los huecos
como ventanas sin texto."""
import os

import pytest

from gameplay_editor import classification
from gameplay_editor.config import load_config


@pytest.fixture
def cfg():
    return load_config()["classification"]


SETTINGS = {
    "contentType": "gameplay",
    "description": "",
    "categories": [
        {"label": "Neutro", "description": "", "keep": True},
        {"label": "Relleno", "description": "", "keep": False},
    ],
}


def _seg(inicio, fin, texto="algo", fuente="creador"):
    return {"inicio": inicio, "fin": fin, "texto": texto, "fuente": fuente}


def _ventanas(cfg, creador=(), contenido=(), duration=None):
    return classification.build_windows({"creador": list(creador), "contenido": list(contenido)}, cfg, duration)


def _con_texto(ventanas):
    return [w for w in ventanas if w["texto"] is not None]


def test_corta_en_silencio_de_las_dos_pistas(cfg):
    creador = [_seg(5.0, 8.0, "hola"), _seg(15.0, 17.0, "que tal"), _seg(27.0, 30.0, "chau")]

    ventanas = _ventanas(cfg, creador, duration=60.0)

    # 15 - 8 = 7 s sigue el tramo; 27 - 17 = 10 s lo corta
    assert [(w["inicio"], w["fin"], w["texto"]) for w in ventanas] == [
        (0.0, 5.0, None),
        (5.0, 17.0, "[voz del creador] hola\n[voz del creador] que tal"),
        (17.0, 27.0, None),
        (27.0, 30.0, "[voz del creador] chau"),
        (30.0, 60.0, None),
    ]
    assert [w["evidencia"] for w in _con_texto(ventanas)] == [(5.0, 17.0), (27.0, 30.0)]


def test_el_audio_del_contenido_no_deja_cortar(cfg):
    creador = [_seg(0.0, 2.0, "mira"), _seg(20.0, 22.0, "que bueno")]
    contenido = [_seg(8.0, 14.0, "te estaba esperando", "contenido")]

    [ventana] = _con_texto(_ventanas(cfg, creador, contenido, duration=30.0))

    assert (ventana["inicio"], ventana["fin"]) == (0.0, 22.0)
    assert ventana["tiene_voz_creador"]
    assert ventana["duracion_hablada"] == 10.0


def test_segmento_largo_que_tapa_a_otros_no_corta(cfg):
    # el fin maximo del tramo es 30, aunque el ultimo segmento visto termine en 6
    contenido = [_seg(0.0, 30.0, "cinematica", "contenido")]
    creador = [_seg(5.0, 6.0, "uh"), _seg(35.0, 36.0, "ya")]

    [ventana] = _con_texto(_ventanas(cfg, creador, contenido))

    assert (ventana["inicio"], ventana["fin"]) == (0.0, 36.0)


def test_tramo_largo_se_parte_en_su_pausa_mas_larga(cfg):
    # frases de 4 s cada 5 s de 0 a 99, con una pausa de 6 s entre 49 y 55: ningun
    # silencio llega a 10 s, pero el tramo dura 99 s
    creador = [_seg(float(t), t + 4.0) for t in [*range(0, 50, 5), *range(55, 100, 5)]]

    ventanas = _con_texto(_ventanas(cfg, creador))

    assert [(w["inicio"], w["fin"]) for w in ventanas] == [(0.0, 49.0), (55.0, 99.0)]


def test_parte_hasta_que_todas_entran(cfg):
    cfg = {**cfg, "max_ventana_sec": 20.0}
    creador = [_seg(0.0, 9.0), _seg(10.0, 19.0), _seg(21.0, 30.0), _seg(30.5, 39.0), _seg(42.0, 50.0)]

    ventanas = _con_texto(_ventanas(cfg, creador))

    assert [(w["inicio"], w["fin"]) for w in ventanas] == [(0.0, 19.0), (21.0, 39.0), (42.0, 50.0)]


def test_un_solo_segmento_largo_queda_entero(cfg):
    [ventana] = _con_texto(_ventanas(cfg, [_seg(0.0, 120.0)]))
    assert (ventana["inicio"], ventana["fin"]) == (0.0, 120.0)


def test_cada_segmento_en_exactamente_una_ventana(cfg):
    creador = [_seg(float(t), t + 3.0, f"j{t}") for t in range(0, 300, 7)]
    contenido = [_seg(t + 1.0, t + 2.0, f"g{t}", "contenido") for t in range(0, 300, 23)]

    textos = [w["texto"] for w in _ventanas(cfg, creador, contenido, duration=310.0) if w["texto"]]
    lineas = [linea for texto in textos for linea in texto.split("\n")]

    etiquetas = {"creador": "[voz del creador]", "contenido": "[audio del contenido]"}
    esperado = [f"{etiquetas[s['fuente']]} {s['texto']}" for s in creador + contenido]
    assert sorted(lineas) == sorted(esperado)


def test_contexto_y_silencio_alrededor(cfg):
    creador = [_seg(10.0, 12.0, "a"), _seg(30.0, 32.0, "b"), _seg(50.0, 52.0, "c"), _seg(80.0, 82.0, "d")]

    ventanas = _con_texto(_ventanas(cfg, creador, duration=100.0))

    assert [(w["silencio_antes"], w["silencio_despues"]) for w in ventanas] == [
        (10.0, 18.0), (18.0, 18.0), (18.0, 28.0), (28.0, 18.0),
    ]
    assert ventanas[0]["contexto_antes"] == ""
    assert ventanas[0]["contexto_despues"] == "[voz del creador] b\n---\n[voz del creador] c"
    assert ventanas[3]["contexto_antes"] == "[voz del creador] b\n---\n[voz del creador] c"


def test_sin_segmentos(cfg):
    assert [(w["inicio"], w["fin"], w["texto"]) for w in _ventanas(cfg, duration=40.0)] == [(0.0, 40.0, None)]
    assert _ventanas(cfg) == []


def test_silencio_largo_entre_ventanas_conservadas_se_descarta(cfg):
    clasificadas = [
        {**w, "categoria": classification.SIN_TRANSCRIPCION if w["texto"] is None else "divertido_interesante"}
        for w in _ventanas(cfg, [_seg(10.0, 20.0), _seg(200.0, 210.0)], duration=300.0)
    ]

    keep = classification.keep_segments_for_categories(clasificadas, 300.0, ["divertido_interesante"])
    assert keep == [(2.0, 28.0), (192.0, 218.0)]

    con_neutro = classification.keep_segments_for_categories(
        clasificadas, 300.0, ["divertido_interesante"], proteger_silencio=True,
    )
    assert con_neutro == [(0.0, 300.0)]


def test_ventanas_sin_texto_no_se_clasifican(cfg, monkeypatch, tmp_path):
    llamadas = []
    monkeypatch.setitem(
        classification._BACKENDS, cfg["backend"],
        lambda window, settings, dos_videos, cfg: llamadas.append(window) or {"categoria": "neutro", "razon": ""},
    )

    clasificadas = classification.classify(
        {"creador": [_seg(10.0, 20.0)], "contenido": []}, cfg, SETTINGS, "t", output_dir=str(tmp_path), duration=60.0,
    )

    assert [w["categoria"] for w in clasificadas] == [
        classification.SIN_TRANSCRIPCION, "neutro", classification.SIN_TRANSCRIPCION,
    ]
    assert len(llamadas) == 1


def test_no_reusa_cache_de_ventanas_fijas(cfg, monkeypatch, tmp_path):
    transcripcion = {"creador": [_seg(10.0, 20.0)], "contenido": []}
    llamadas = []
    monkeypatch.setitem(
        classification._BACKENDS, cfg["backend"],
        lambda window, settings, dos_videos, cfg: llamadas.append(window) or {"categoria": "neutro", "razon": ""},
    )
    classification.classify(transcripcion, cfg, SETTINGS, "t", output_dir=str(tmp_path), duration=60.0)
    path = os.path.join(tmp_path, "t.clasificacion.json")
    with open(path, encoding="utf-8") as fh:
        cache = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(cache.replace('"prompt_version": "v5"', '"prompt_version": "v1"'))

    classification.classify(transcripcion, cfg, SETTINGS, "t", output_dir=str(tmp_path), duration=60.0)

    assert len(llamadas) == 2


def test_con_un_solo_video_no_hay_etiquetas(cfg):
    ventanas = classification.build_windows(
        {"creador": None, "contenido": [_seg(5.0, 8.0, "hola", "contenido"), _seg(9.0, 10.0, "chau", "contenido")]},
        cfg, 20.0,
    )
    assert [w["texto"] for w in _con_texto(ventanas)] == ["hola\nchau"]
    assert not ventanas[1]["tiene_voz_creador"]
