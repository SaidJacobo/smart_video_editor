"""Descubre pares gameplay/webcam dentro de una carpeta (una grabacion por
archivo, nombrados con el mismo prefijo + sufijo -gameplay/-webcam) y los
ordena cronologicamente por ese prefijo."""
import os
import re

_SUFFIX_RE = re.compile(r"^(?P<prefix>.+)-(?P<kind>gameplay|webcam)\.(?P<ext>[^.]+)$", re.IGNORECASE)
_VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "ts", "m2ts"}


def discover_pairs(folder):
    """Devuelve (pairs, warnings). pairs es una lista de (prefix, gameplay_path,
    webcam_path) ordenada cronologicamente por prefix (asume nombres tipo
    'YYYY-MM-DD HH-MM-SS', que ordenan bien como texto). warnings son strings
    para avisar de archivos -gameplay/-webcam sin su par."""
    gp_by_prefix, wc_by_prefix = {}, {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        m = _SUFFIX_RE.match(name)
        if not m or m.group("ext").lower() not in _VIDEO_EXTS:
            continue
        prefix, kind = m.group("prefix"), m.group("kind").lower()
        (gp_by_prefix if kind == "gameplay" else wc_by_prefix)[prefix] = path

    prefixes = sorted(set(gp_by_prefix) | set(wc_by_prefix))
    pairs, warnings = [], []
    for prefix in prefixes:
        gp, wc = gp_by_prefix.get(prefix), wc_by_prefix.get(prefix)
        if gp and wc:
            pairs.append((prefix, gp, wc))
        elif gp:
            warnings.append(f"Sin webcam para: {gp}")
        else:
            warnings.append(f"Sin gameplay para: {wc}")
    return pairs, warnings
