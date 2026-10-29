"""Ordre de tri propre à chaque dossier, comme Nautilus 46 : mêmes métadonnées GVfs
(metadata::nautilus-icon-view-sort-by / -sort-reversed, nautilus-files-view.c), donc un dossier trié
dans Odysseus l'est aussi dans Nautilus, et inversement. Un dossier sans tri enregistré prend le tri
par défaut (Préférences).

Nautilus connaît name, size, type, date_modified (avec le même sens du drapeau « reversed » que nous).
Les autres tris d'Odysseus (durée, droits, propriétaire) ne vont jamais dans l'attribut de Nautilus :
sa vue en liste, sans colonne de ce nom, resterait sans tri. Ils vont dans sort.json avec la valeur
de Nautilus du moment : si Nautilus change le tri entre-temps, c'est lui qui gagne. Même fichier
quand GVfs n'écrit pas (pas de démon de métadonnées).

Emplacements qui ne sont pas des chemins locaux (réseau, corbeille, archives) : mémorisés pour la
session seulement (lire des métadonnées sur un serveur lent bloquerait l'interface)."""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from gi.repository import Gio, GLib

from ..settings import CONFIG_DIR

SORT_BY = "metadata::nautilus-icon-view-sort-by"
REVERSED = "metadata::nautilus-icon-view-sort-reversed"
TO_NAUTILUS = {"name": "name", "size": "size", "type": "type", "date": "date_modified"}
FROM_NAUTILUS = {v: k for k, v in TO_NAUTILUS.items()}
FALLBACK = os.path.join(CONFIG_DIR, "sort.json")

_session: dict[str, tuple[str, bool]] = {}
_fallback: Optional[dict] = None
# rappelés avec (emplacement, clé, inverse) quand un tri change : les panneaux qui l'affichent suivent
listeners: list[Callable[[str, str, bool], None]] = []


def _local(place: str) -> bool:
    return place.startswith("/")


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


def _read_gvfs(path: str) -> tuple[Optional[str], Optional[str]]:
    try:
        info = Gio.File.new_for_path(path).query_info(f"{SORT_BY},{REVERSED}", Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return None, None
    by = info.get_attribute_string(SORT_BY) if info.has_attribute(SORT_BY) else None
    rev = info.get_attribute_string(REVERSED) if info.has_attribute(REVERSED) else None
    return by, rev


def _write_gvfs(path: str, by: str, reverse: bool) -> bool:
    info = Gio.FileInfo()
    info.set_attribute_string(SORT_BY, by)
    info.set_attribute_string(REVERSED, "true" if reverse else "false")     # comme nautilus_file_set_boolean_metadata
    try:
        Gio.File.new_for_path(path).set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, None)
        return True
    except GLib.Error:
        return False


def get(place: str) -> Optional[tuple[str, bool]]:
    """Tri enregistré pour cet emplacement (clé, inverse), ou None : tri par défaut."""
    if place in _session:
        return _session[place]
    if not _local(place):
        return None
    by, rev = _read_gvfs(place)
    own = _fb().get(place)
    if own and own.get("base") == by:
        return own["key"], bool(own["reverse"])
    key = FROM_NAUTILUS.get(by or "")
    if key is None:
        return None          # rien, ou un tri de Nautilus inconnu ici (étoiles, date d'accès…)
    return key, rev == "true"


def save(place: str, key: str, reverse: bool):
    if not _local(place):
        _session[place] = (key, reverse)
    else:
        fb = _fb()
        if key in TO_NAUTILUS and _write_gvfs(place, TO_NAUTILUS[key], reverse):
            if fb.pop(place, None) is not None:
                _fb_save()
        else:
            fb[place] = {"key": key, "reverse": reverse, "base": _read_gvfs(place)[0]}
            _fb_save()
    for cb in list(listeners):
        cb(place, key, reverse)
