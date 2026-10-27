"""Couleurs de dossiers et emblèmes (reprise de Folder Color Revival).

Mêmes attributs de métadonnées GVfs que Folder Color pour Nautilus (metadata::custom-icon,
metadata::custom-icon-name, metadata::emblems) : les couleurs posées ici s'affichent aussi dans
Nautilus, et inversement. Si le thème d'icônes n'a pas de dossiers colorés, Odysseus dessine
lui-même un dossier de la couleur voulue (SVG dans ~/.cache/odysseus/folders).
Sans GVfs (métadonnées indisponibles), un petit fichier JSON prend le relais.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from gi.repository import Gio, GLib

from ..i18n import tr
from ..settings import CACHE_DIR, CONFIG_DIR

COLORS = [
    ("black", tr("Noir"), "#3d3846"), ("blue", tr("Bleu"), "#3584e4"), ("brown", tr("Marron"), "#986a44"),
    ("cyan", tr("Cyan"), "#2ec2c4"), ("green", tr("Vert"), "#33d17a"), ("grey", tr("Gris"), "#9a9996"),
    ("magenta", tr("Magenta"), "#c061cb"), ("orange", tr("Orange"), "#ff7800"), ("pink", tr("Rose"), "#f66151"),
    ("purple", tr("Pourpre"), "#813d9c"), ("red", tr("Rouge"), "#e01b24"), ("violet", tr("Violet"), "#935cd8"),
    ("white", tr("Blanc"), "#f6f5f4"), ("yellow", tr("Jaune"), "#f6d32d"),
]
COLOR_HEX = {k: h for k, _l, h in COLORS}

EMBLEMS = [
    ("emblem-important", tr("Important")), ("emblem-urgent", tr("En cours")),
    ("emblem-favorite", tr("Favori")), ("emblem-default", tr("Terminé")), ("emblem-new", tr("Nouveau")),
    ("emblem-documents", tr("Documents")), ("emblem-photos", tr("Photos")), ("emblem-shared", tr("Partagé")),
]

ATTRS = "metadata::custom-icon,metadata::custom-icon-name,metadata::emblems"
ICON_DIR = os.path.join(CACHE_DIR, "folders")
FALLBACK = os.path.join(CONFIG_DIR, "metadata.json")

_fallback: Optional[dict] = None


def _fb() -> dict:
    global _fallback
    if _fallback is None:
        try:
            with open(FALLBACK, encoding="utf-8") as fh:
                _fallback = json.load(fh)
        except (OSError, ValueError):
            _fallback = {}
    return _fallback


def _fb_save():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(FALLBACK + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(_fb(), fh)
        os.replace(FALLBACK + ".tmp", FALLBACK)
    except OSError:
        pass


def metadata_supported(path: str) -> bool:
    try:
        f = Gio.File.new_for_path(path)
        attrs = f.query_writable_namespaces(None)
        return attrs is not None and attrs.lookup("metadata") is not None
    except GLib.Error:
        return False


def read(path: str) -> dict:
    """{'icon_name': …, 'icon_uri': …, 'emblems': [...]} (clés absentes si rien)."""
    out: dict = {}
    try:
        info = Gio.File.new_for_path(path).query_info(ATTRS, Gio.FileQueryInfoFlags.NONE, None)
        name = info.get_attribute_string("metadata::custom-icon-name")
        uri = info.get_attribute_string("metadata::custom-icon")
        emb = info.get_attribute_stringv("metadata::emblems") if info.has_attribute("metadata::emblems") else None
        if name:
            out["icon_name"] = name
        if uri:
            out["icon_uri"] = uri
        if emb:
            out["emblems"] = [e for e in emb if e]
    except GLib.Error:
        pass
    fb = _fb().get(path)
    if fb:
        for k, v in fb.items():
            out.setdefault(k, v)
    return out


def _write(path: str, values: dict):
    """values : attribut → valeur (None pour effacer)."""
    f = Gio.File.new_for_path(path)
    try:
        info = Gio.FileInfo()
        for attr, v in values.items():
            if v is None:
                info.set_attribute(attr, Gio.FileAttributeType.INVALID, 0)
            elif isinstance(v, list):
                info.set_attribute_stringv(attr, v)
            else:
                info.set_attribute_string(attr, v)
        f.set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, None)
        ok = True
    except GLib.Error:
        ok = False
    if not ok:
        entry = dict(_fb().get(path, {}))
        mapping = {"metadata::custom-icon-name": "icon_name", "metadata::custom-icon": "icon_uri",
                   "metadata::emblems": "emblems"}
        for attr, v in values.items():
            k = mapping[attr]
            if v in (None, []):
                entry.pop(k, None)
            else:
                entry[k] = v
        if entry:
            _fb()[path] = entry
        else:
            _fb().pop(path, None)
        _fb_save()
    try:
        os.utime(path, None)     # Nautilus recharge l'icône
    except OSError:
        pass


FOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
<path d="M14 22h34l10 10h56c4 0 6 2 6 6v66c0 4-2 6-6 6H14c-4 0-6-2-6-6V28c0-4 2-6 6-6z" fill="{dark}"/>
<path d="M8 44c0-4 2-6 6-6h100c4 0 6 2 6 6v60c0 4-2 6-6 6H14c-4 0-6-2-6-6z" fill="{color}"/>
<path d="M8 100v4c0 4 2 6 6 6h100c4 0 6-2 6-6v-4z" fill="{dark}" opacity=".35"/>
</svg>"""


def _shade(hexcolor: str, f: float) -> str:
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c * f))) for c in (r, g, b))


def folder_icon_file(color: str) -> str:
    """SVG de dossier coloré, généré une fois."""
    os.makedirs(ICON_DIR, exist_ok=True)
    path = os.path.join(ICON_DIR, f"folder-{color}.svg")
    if not os.path.exists(path):
        hexc = COLOR_HEX.get(color, "#3584e4")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FOLDER_SVG.format(color=hexc, dark=_shade(hexc, 0.78)))
    return path


def set_color(path: str, color: str, has_icon: Callable[[str], bool]):
    """Colore un dossier : icône du thème si elle existe, sinon dossier dessiné par Odysseus."""
    for candidate in (f"folder-{color}", f"folder_color_{color}", f"folder_{color}"):
        if has_icon(candidate):
            _write(path, {"metadata::custom-icon-name": candidate, "metadata::custom-icon": None})
            return
    uri = Gio.File.new_for_path(folder_icon_file(color)).get_uri()
    _write(path, {"metadata::custom-icon": uri, "metadata::custom-icon-name": None})


def set_custom_icon(path: str, uri: str):
    """Icône personnalisée (image choisie dans Propriétés), comme Nautilus : metadata::custom-icon."""
    _write(path, {"metadata::custom-icon": uri, "metadata::custom-icon-name": None})


def reset_icon(path: str) -> dict:
    """Retire l'icône personnalisée (couleur comprise) ; renvoie l'ancienne pour « Annuler »."""
    old = {k: v for k, v in read(path).items() if k in ("icon_name", "icon_uri")}
    _write(path, {"metadata::custom-icon": None, "metadata::custom-icon-name": None})
    return old


def restore_icon(path: str, old: dict):
    _write(path, {"metadata::custom-icon": old.get("icon_uri"), "metadata::custom-icon-name": old.get("icon_name")})


def toggle_emblem(path: str, emblem: str):
    cur = read(path).get("emblems", [])
    new = [e for e in cur if e != emblem] if emblem in cur else cur + [emblem]
    _write(path, {"metadata::emblems": new or None})


def restore(path: str):
    _write(path, {"metadata::custom-icon": None, "metadata::custom-icon-name": None,
                  "metadata::emblems": None})


def is_modified(path: str) -> bool:
    return bool(read(path))
