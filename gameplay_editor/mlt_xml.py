"""Construccion de XML MLT/Kdenlive desde cero: producers, playlists, tractors,
transiciones qtblend (PiP) y frei0r.squareblur (blur de fondo en los shorts).

No pretende reproducir toda la metadata que genera la propia Kdenlive (thumbs,
proxies, layout de paneles, etc) -- solo lo necesario para que el proyecto abra
y el usuario pueda seguir editando a mano.
"""
import functools
import json
import re
import uuid as uuid_module
import subprocess
import xml.etree.ElementTree as ET


@functools.lru_cache(maxsize=1)
def installed_mlt_version():
    # Kdenlive compara este atributo contra su propio MLT embebido; un valor
    # inventado/desactualizado hizo que rechazara abrir el proyecto ("Error
    # al abrir el archivo"). Mejor preguntarle a melt la version real.
    try:
        out = subprocess.run(["melt", "--version"], capture_output=True, text=True, timeout=5)
        match = re.search(r"(\d+\.\d+\.\d+)", out.stdout + out.stderr)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "7.0.0"


def prop(name, value):
    el = ET.Element("property", {"name": name})
    el.text = str(value)
    return el


def profile_element(width, height, fps_num, fps_den=1, vertical=False):
    fps = fps_num / fps_den
    description = f"HD {height}p {fps:g} fps" + (" (vertical)" if vertical else "")
    attrs = {
        "colorspace": "709",
        "description": description,
        "display_aspect_den": "9" if not vertical else "16",
        "display_aspect_num": "16" if not vertical else "9",
        "frame_rate_den": str(fps_den),
        "frame_rate_num": str(fps_num),
        "height": str(height),
        "progressive": "1",
        "sample_aspect_den": "1",
        "sample_aspect_num": "1",
        "width": str(width),
    }
    return ET.Element("profile", attrs)


def color_producer(id_, out_tc):
    p = ET.Element("producer", {"id": id_, "in": "00:00:00.000", "out": out_tc})
    for name, value in [
        ("length", "2147483647"),
        ("eof", "continue"),
        ("resource", "black"),
        ("aspect_ratio", "1"),
        ("mlt_service", "color"),
        # sin esto Kdenlive no reconoce este producer como el fondo interno
        # de la secuencia y lo marca "Medio no referenciado" en el chequeo de
        # clips al abrir (asi esta marcado en los .kdenlive reales).
        ("kdenlive:playlistid", "black_track"),
        ("mlt_image_format", "rgba"),
        ("set.test_audio", "0"),
    ]:
        p.append(prop(name, value))
    return p


def chain(id_, resource, out_tc, length_frames, bin_id, meta=None):
    c = ET.Element("chain", {"id": id_, "out": out_tc})
    for name, value in [
        ("length", str(length_frames)),
        ("eof", "pause"),
        ("resource", resource),
        ("aspect_ratio", "1"),
        ("mlt_service", "avformat-novalidate"),
        ("seekable", "1"),
    ]:
        c.append(prop(name, value))
    for name, value in (meta or {}).items():
        c.append(prop(name, value))
    # sin kdenlive:file_hash a proposito: un hash inventado que no matchea el
    # algoritmo real de Kdenlive hace que la trate como "el archivo cambio" y
    # pida relocalizarlo -- mejor dejar que lo calcule ella misma al abrir.
    c.append(prop("kdenlive:proxy", "-"))
    c.append(prop("kdenlive:duration", out_tc))
    c.append(prop("kdenlive:maxduration", str(length_frames)))
    c.append(prop("kdenlive:control_uuid", "{" + str(uuid_module.uuid4()) + "}"))
    # sin esto Kdenlive no puede linkear los clips del timeline con los del
    # bin ("INVALID BIN PLAYLIST"): cada entry del timeline necesita el mismo
    # kdenlive:id que su clip de origen en el bin.
    c.append(prop("kdenlive:id", str(bin_id)))
    c.append(prop("kdenlive:folderid", "-1"))
    return c


def rect_value(x, y, w, h, opacity=1.0, at_tc="00:00:00.000"):
    return f"{at_tc}={x:g} {y:g} {w:g} {h:g} {opacity:g}"


def qtblend_filter(id_, rect_str):
    f = ET.Element("filter", {"id": id_})
    for name, value in [
        ("rotate_center", "1"),
        ("mlt_service", "qtblend"),
        ("kdenlive_id", "qtblend"),
        ("compositing", "0"),
        ("distort", "0"),
        ("rect", rect_str),
        ("rotation", "0"),
    ]:
        f.append(prop(name, value))
    return f


def squareblur_filter(id_, kernel_size):
    f = ET.Element("filter", {"id": id_})
    for name, value in [
        ("version", "0.1"),
        ("mlt_service", "frei0r.squareblur"),
        ("kdenlive_id", "frei0r.squareblur"),
        ("Kernel size", str(kernel_size)),
    ]:
        f.append(prop(name, value))
    return f


def entry(in_tc, out_tc, producer_id, filters=None, bin_id=None):
    e = ET.Element("entry", {"in": in_tc, "out": out_tc, "producer": producer_id})
    if bin_id is not None:
        e.append(prop("kdenlive:id", str(bin_id)))
    for filt in filters or []:
        e.append(filt)
    return e


def playlist(id_, entries=None):
    p = ET.Element("playlist", {"id": id_})
    for e in entries or []:
        p.append(e)
    return p


def track_tractor(id_, out_tc, playlist_a_id, playlist_b_id, hide):
    t = ET.Element("tractor", {"id": id_, "in": "00:00:00.000", "out": out_tc})
    if hide == "video":
        # sin esto Kdenlive no reconoce esta pista como pista de audio (no
        # aparece en el timeline ni suena) -- lo lee de esta property, no
        # alcanza con el hide="video" de los <track> de abajo.
        t.append(prop("kdenlive:audio_track", "1"))
    t.append(ET.Element("track", {"hide": hide, "producer": playlist_a_id}))
    t.append(ET.Element("track", {"hide": hide, "producer": playlist_b_id}))
    return t


def master_tractor(id_, out_tc, canvas_producer_id, audio_track_ids, video_track_ids, sequence_uuid, clipname, bin_id, duration_frames):
    t = ET.Element("tractor", {"id": id_, "in": "00:00:00.000", "out": out_tc})
    t.append(prop("kdenlive:duration", out_tc))
    t.append(prop("kdenlive:maxduration", str(duration_frames)))
    t.append(prop("kdenlive:clipname", clipname))
    t.append(prop("kdenlive:uuid", sequence_uuid))
    t.append(prop("kdenlive:control_uuid", sequence_uuid))
    t.append(prop("kdenlive:producer_type", "17"))
    # sin esto el bin lo marca "BIN CLIP WITHOUT ID" y cae a INVALID BIN
    # PLAYLIST -- pasaba con los clips de media, pero tambien hace falta en
    # la propia secuencia (asi esta en los .kdenlive reales de Kdenlive).
    t.append(prop("kdenlive:id", str(bin_id)))
    t.append(prop("kdenlive:folderid", "-1"))
    t.append(prop("kdenlive:clip_type", "0"))
    t.append(prop("kdenlive:sequenceproperties.hasAudio", "1"))
    t.append(prop("kdenlive:sequenceproperties.hasVideo", "1"))
    t.append(prop("kdenlive:sequenceproperties.tracks", str(len(audio_track_ids) + len(video_track_ids))))
    t.append(prop("kdenlive:sequenceproperties.tracksCount", str(len(audio_track_ids) + len(video_track_ids))))
    # activeTrack es un indice de pista 0-based (sin contar el canvas) --
    # ponerlo igual a la CANTIDAD de pistas (no al ultimo indice valido)
    # queda fuera de rango y Kdenlive lo marca como bug ("activeTrack
    # property is N but track count is only N") al cargar.
    t.append(prop("kdenlive:sequenceproperties.activeTrack", str(len(audio_track_ids) + len(video_track_ids) - 1)))

    t.append(ET.Element("track", {"producer": canvas_producer_id}))
    for tid in audio_track_ids + video_track_ids:
        t.append(ET.Element("track", {"producer": tid}))

    b_track = 1
    for _ in audio_track_ids:
        tr = ET.Element("transition", {"id": f"{id_}_t{b_track}"})
        for name, value in [
            ("a_track", "0"),
            ("b_track", str(b_track)),
            ("mlt_service", "mix"),
            ("kdenlive_id", "mix"),
            # marca la composicion como generada por la propia app -- sin
            # esto Kdenlive la reporta como "metodo de composicion invalido"
            # al abrir y la fuerza a mano.
            ("internal_added", "237"),
            ("always_active", "1"),
            ("accepts_blanks", "1"),
            ("sum", "1"),
        ]:
            tr.append(prop(name, value))
        t.append(tr)
        b_track += 1
    for _ in video_track_ids:
        tr = ET.Element("transition", {"id": f"{id_}_t{b_track}"})
        for name, value in [
            ("a_track", "0"),
            ("b_track", str(b_track)),
            ("compositing", "0"),
            ("distort", "0"),
            ("rotate_center", "0"),
            ("mlt_service", "qtblend"),
            ("kdenlive_id", "qtblend"),
            ("internal_added", "237"),
            ("always_active", "1"),
        ]:
            tr.append(prop(name, value))
        t.append(tr)
        b_track += 1
    return t


# --- agrupado de clips (kdenlive:sequenceproperties.groups) ---------------
# DESHABILITADO por ahora: se investigo a fondo (ver historial) e incluso con
# todo lo siguiente corregido, Kdenlive solo respeta de forma confiable los
# primeros ~6 grupos de un archivo generado externamente -- despues de eso,
# clickear un clip ya no selecciona el resto del grupo (sin crashear, solo
# deja de funcionar). Se descarto como causa: cantidad de grupos (un archivo
# real con 217 grupos anda perfecto), huecos entre grupos, tamano del archivo,
# sesion unica vs multiple, y in/out incorrectos (verificado 100% exacto).
# Se encontraron y quedan corregidos 3 bugs reales en el camino:
#   1. Un grupo "Normal" plano con las 4 pistas (audio+video de
#      gameplay+webcam) como hijos directos crashea Kdenlive al abrir --
#      hay que anidar cada par video+audio como sub-grupo "AVSplit".
#   2. El indice de pista en "track:pos:-1" es 0-based SIN CONTAR EL CANVAS
#      (no 1-based incluyendolo como se asumio al principio).
#   3. kdenlive:sequenceproperties.activeTrack tiene que ser un indice 0-based
#      valido (cantidad de pistas - 1), no la cantidad de pistas.
# La causa de fondo del limite de ~6 grupos no se pudo identificar sin acceso
# al codigo fuente de Kdenlive. Estas funciones quedan listas para retomar.


def av_group_part(video_idx, audio_idx, pos):
    """Par video+audio (mismo clip) para anidar como AVSplit dentro de un
    grupo, o solo el video si no hay pista de audio."""
    if audio_idx is not None:
        return [(video_idx, pos), (audio_idx, pos)]
    return (video_idx, pos)


def groups_property(groups):
    """groups: lista de grupos; cada grupo es una lista de "partes", y cada
    parte es una tupla (track_index, position_frame) suelta o un par de dos
    tuplas [(track,pos), (track,pos)] que se agrupan como AVSplit (video+audio
    del mismo clip)."""
    def leaf(track, pos):
        return {"data": f"{track}:{pos}:-1", "leaf": "clip", "type": "Leaf"}

    def build_part(part):
        if len(part) == 2 and isinstance(part[0], tuple):
            return {"type": "AVSplit", "children": [leaf(*part[0]), leaf(*part[1])]}
        return leaf(*part)

    data = [
        {"type": "Normal", "children": [build_part(part) for part in group]}
        for group in groups if len(group) >= 2
    ]
    return prop("kdenlive:sequenceproperties.groups", json.dumps(data, indent=4))


def guides_property(highlights_frames):
    data = [{"comment": h.get("comment", "highlight"), "pos": h["frame"], "type": 3} for h in highlights_frames]
    return prop("kdenlive:sequenceproperties.guides", json.dumps(data, indent=4))


def guess_profile_name(width, height, fps_num, fps_den):
    fps = fps_num / fps_den
    if height == 1080 and width == 1920:
        for candidate_fps, suffix in [(23.98, "2398"), (24, "24"), (25, "25"), (29.97, "2997"), (30, "30"), (50, "50"), (59.94, "5994"), (60, "60")]:
            if abs(fps - candidate_fps) < 0.05:
                return f"atsc_1080p_{suffix}"
    return "custom"


def _main_bin_base(active_uuid, opensequences, document_id, profile_name):
    pl = ET.Element("playlist", {"id": "main_bin"})
    for name, value in [
        ("kdenlive:docproperties.version", "1.1"),
        ("kdenlive:docproperties.documentid", document_id),
        ("kdenlive:docproperties.uuid", active_uuid),
        ("kdenlive:docproperties.activetimeline", active_uuid),
        ("kdenlive:docproperties.opensequences", opensequences),
        ("kdenlive:docproperties.profile", profile_name),
        ("kdenlive:docproperties.audioChannels", "2"),
        ("kdenlive:docproperties.enableproxy", "0"),
        ("kdenlive:docproperties.generateproxy", "0"),
        ("xml_retain", "1"),
    ]:
        pl.append(prop(name, value))
    return pl


def main_bin_playlist(chain_entries, sequence_tractor_id, sequence_uuid, sequence_out_tc, document_id, profile_name):
    pl = _main_bin_base(sequence_uuid, sequence_uuid, document_id, profile_name)
    for chain_id, out_tc in chain_entries:
        pl.append(ET.Element("entry", {"in": "00:00:00.000", "out": out_tc, "producer": chain_id}))
    pl.append(ET.Element("entry", {"in": "00:00:00.000", "out": sequence_out_tc, "producer": sequence_tractor_id}))
    return pl


def main_bin_playlist_multi(chain_entries, sequences, active_uuid, opensequences, document_id, profile_name):
    """Como main_bin_playlist pero con varias secuencias (una entry por
    tractor de secuencia, todas listadas en docproperties.opensequences)."""
    pl = _main_bin_base(active_uuid, opensequences, document_id, profile_name)
    for chain_id, out_tc in chain_entries:
        pl.append(ET.Element("entry", {"in": "00:00:00.000", "out": out_tc, "producer": chain_id}))
    for master_id, _sequence_uuid, out_tc in sequences:
        pl.append(ET.Element("entry", {"in": "00:00:00.000", "out": out_tc, "producer": master_id}))
    return pl


def project_tractor(id_, sequence_tractor_id, out_tc):
    # Wrapper final que Kdenlive espera DESPUES de main_bin, apuntando a la
    # secuencia activa -- sin el, el loader no logra conectar el timeline
    # ("INVALID BIN PLAYLIST", DUR: 0) aunque el resto este bien armado.
    t = ET.Element("tractor", {"id": id_, "in": "00:00:00.000", "out": out_tc})
    t.append(prop("kdenlive:projectTractor", "1"))
    t.append(ET.Element("track", {"in": "00:00:00.000", "out": out_tc, "producer": sequence_tractor_id}))
    return t


def build_document(profile_el, body_elements):
    # Sin tractor "wrapper" extra envolviendo la secuencia: agregarlo (aunque
    # el propio Kdenlive a veces lo escribe) confundia al loader real -- vimos
    # bajar la tasa de crash al abrir de ~37% a 0% sacandolo.
    root = ET.Element("mlt", {"LC_NUMERIC": "C", "producer": "main_bin", "version": installed_mlt_version()})
    root.append(profile_el)
    for el in body_elements:
        root.append(el)
    return root


def write_document(root, path):
    ET.indent(root, space=" ")
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)
