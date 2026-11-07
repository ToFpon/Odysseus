"""Colonnes de la vue en liste propres à un dossier, comme Nautilus 46 (« N'appliquer qu'au dossier
actuel » du sélecteur de colonnes) : mêmes métadonnées GVfs (metadata::nautilus-list-view-visible-columns
et -column-order, nautilus-list-view.c), donc un dossier réglé dans Odysseus l'est aussi dans Nautilus,
et inversement. Un dossier sans réglage prend les colonnes par défaut (settings list_columns /
column_order).

Noms de Nautilus : name, size, type, owner, permissions, date_modified, starred… Les colonnes
d'Odysseus qu'il ne connaît pas (durée) vont dans columns.json avec la valeur de Nautilus du moment :
s'il la change entre-temps, c'est lui qui gagne (comme folder_sort). Même fichier quand GVfs n'écrit pas.

Emplacements qui ne sont pas des chemins locaux : rien de mémorisé (réglage par défaut)."""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from gi.repository import Gio, GLib

from ..settings import CONFIG_DIR

VISIBLE = "metadata::nautilus-list-view-visible-columns"
ORDER = "metadata::nautilus-list-view-column-order"
TO_NAUTILUS = {"name": "name", "size": "size", "type": "type", "date": "date_modified",
               "perms": "permissions", "owner": "owner"}
FROM_NAUTILUS = {v: k for k, v in TO_NAUTILUS.items()}
FALLBACK = os.path.join(CONFIG_DIR, "columns.json")

_fallback: Optional[dict] = None
# rappelés avec l'emplacement quand ses colonnes changent : les panneaux qui l'affichent suivent
listeners: list[Callable[[str], None]] = []


def _local(place: Optional[str]) -> bool:
    return bool(place) and place.startswith("/")


def _fb() -> dict:
    global _fallback
    if _fallback is None:
        try:
            with open(FALLBACK, encoding="utf-8") as fh:
                _fallback = json.load(fh)
        except (OSError, ValueError):
            _fallback = {}
        if not isinstance(_fallback, dict):
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


def _read_gvfs(path: str) -> tuple[list, list]:
    try:
        info = Gio.File.new_for_path(path).query_info(f"{VISIBLE},{ORDER}", Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return [], []
    vis = info.get_attribute_stringv(VISIBLE) if info.has_attribute(VISIBLE) else []
    order = info.get_attribute_stringv(ORDER) if info.has_attribute(ORDER) else []
    return list(vis or []), list(order or [])


def _write_gvfs(path: str, visible: list, order: list) -> bool:
    info = Gio.FileInfo()
    info.set_attribute_stringv(VISIBLE, visible)
    info.set_attribute_stringv(ORDER, order)
    try:
        Gio.File.new_for_path(path).set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, None)
        return True
    except GLib.Error:
        return False


def _unset_gvfs(path: str):
    f = Gio.File.new_for_path(path)
    for key in (VISIBLE, ORDER):
        try:
            f.set_attribute(key, Gio.FileAttributeType.INVALID, None, Gio.FileQueryInfoFlags.NONE, None)
        except (GLib.Error, TypeError):
            pass


def _ours(names: list) -> list:
    return [FROM_NAUTILUS[n] for n in names if n in FROM_NAUTILUS and n != "name"]


def get(place: Optional[str]) -> Optional[tuple[list, list]]:
    """(colonnes visibles, ordre) propres à ce dossier, sans « name » ; None : réglage par défaut."""
    if not _local(place):
        return None
    vis, order = _read_gvfs(place)
    own = _fb().get(place)
    if isinstance(own, dict) and own.get("base") == vis and isinstance(own.get("visible"), list) \
            and isinstance(own.get("order"), list):
        # fichier retouché : seuls les noms comptent (un élément d'un autre type bloquait le dossier)
        return [k for k in own["visible"] if isinstance(k, str)], [k for k in own["order"] if isinstance(k, str)]
    if not vis:
        return None
    return _ours(vis), _ours(order)


def save(place: str, visible: list, order: list):
    """Colonnes propres à ce dossier (clés d'Odysseus, sans « name »)."""
    if not _local(place):
        return
    n_vis = ["name"] + [TO_NAUTILUS[k] for k in visible if k in TO_NAUTILUS]
    n_order = ["name"] + [TO_NAUTILUS[k] for k in order if k in TO_NAUTILUS]
    fb = _fb()
    written = _write_gvfs(place, n_vis, n_order)
    if written and all(k in TO_NAUTILUS for k in visible):
        if fb.pop(place, None) is not None:
            _fb_save()
    else:
        fb[place] = {"visible": list(visible), "order": list(order), "base": n_vis if written else _read_gvfs(place)[0]}
        _fb_save()
    _notify(place)


def clear(place: str):
    """Retour aux colonnes par défaut pour ce dossier."""
    if not _local(place):
        return
    _unset_gvfs(place)
    if _fb().pop(place, None) is not None:
        _fb_save()
    _notify(place)


def _notify(place: str):
    for cb in list(listeners):
        cb(place)
