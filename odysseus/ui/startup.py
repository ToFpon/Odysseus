"""Où s'ouvre Odysseus quand on ne lui demande pas de dossier précis (dock, menu des applications,
« odysseus » sans argument) : réglage « Ouvrir Odysseus sur » (`startup_location`).

- home : le dossier personnel, comme Nautilus (par défaut) ;
- last : le dernier emplacement — dossier du panneau actif, et celui de l'autre panneau en double
  panneau —, retenu à la fermeture de la dernière fenêtre et, par sécurité, peu après chaque
  changement de dossier (un plantage ou une déconnexion brutale ne le fait pas perdre) ;
- folder : un dossier choisi dans les Préférences (`startup_folder`).

Un emplacement qui n'est plus là (clé débranchée, dossier supprimé, partage démonté ou muet) :
dossier personnel. Jamais d'attente : un partage réseau a 0,3 s pour répondre (fsinfo.bounded).
"""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Gio, GLib

from ..backend import fsinfo, starred
from ..settings import settings

# pages d'Odysseus qu'on peut rouvrir telles quelles
SPECIAL = {"trash:///", starred.URI, "other-locations:///", "computer:///", "recent:///"}


def place_of(pane) -> str:
    """Ce qu'on retient d'un panneau : son dossier ; pour une archive ouverte, le dossier qui la
    contient ; pour une recherche, le dossier où elle a été lancée ; sinon la page elle-même."""
    if pane.mode in ("fs", "archive", "search"):
        return pane.current_dir() or ""
    return pane.location or ""


def _mounted_uri(uri: str) -> bool:
    """Emplacement distant (smb://, ftp://…) encore monté : d'après la liste de GIO, en mémoire,
    sans rien demander au serveur ni rien monter (une demande de mot de passe au démarrage)."""
    for m in Gio.VolumeMonitor.get().get_mounts():
        root = m.get_root()
        r = root.get_uri() if root is not None else ""
        if r and (uri == r or uri.startswith(r.rstrip("/") + "/")):
            return True
    return False


def usable(loc: str) -> bool:
    if not loc:
        return False
    if loc in SPECIAL:
        return True
    if "://" in loc:
        return _mounted_uri(loc)
    if not os.path.isabs(loc):
        return False
    if fsinfo.is_network(loc):
        return bool(fsinfo.bounded(os.path.isdir, loc, default=False))
    return os.path.isdir(loc)


def locations() -> tuple[Optional[str], Optional[str], int]:
    """(panneau de gauche, panneau de droite ou None, panneau actif 0 / 1) pour une fenêtre ouverte
    sans dossier précis ; (None, None, 0) : dossier personnel."""
    mode = settings["startup_location"]
    if mode == "folder":
        loc = settings["startup_folder"]
        return (loc, None, 0) if usable(loc) else (None, None, 0)
    if mode != "last":
        return None, None, 0
    locs = [x for x in settings["last_locations"] if isinstance(x, str)][:2]
    active = settings["last_active"] if settings["last_active"] in (0, 1) else 0
    ok = [loc if usable(loc) else None for loc in locs]
    if not ok or ok[0] is None and (len(ok) < 2 or ok[1] is None):
        return None, None, 0
    if ok[0] is None:                    # le panneau de gauche n'est plus là : celui de droite le remplace
        return ok[1], None, 0
    return ok[0], ok[1] if len(ok) > 1 else None, active if len(ok) > 1 and ok[1] else 0


def remember(window) -> None:
    """Retient les emplacements de l'onglet affiché de cette fenêtre (sans prévenir les écouteurs
    des réglages : rien à redessiner)."""
    if settings["startup_location"] != "last" or getattr(window, "_torn_down", False):
        return
    ws = getattr(window, "workspace", None)
    if ws is None or not ws.panes:
        return
    panes = list(ws.panes) if ws.dual else [ws.active]
    locs = [place_of(p) for p in panes]
    if not locs[0]:
        return
    active = panes.index(ws.active) if ws.active in panes else 0
    if settings.data.get("last_locations") != locs or settings.data.get("last_active") != active:
        settings.data["last_locations"], settings.data["last_active"] = locs, active
        settings.save()


_pending = {"id": 0, "window": None}


def remember_soon(window, delay: int = 3) -> None:
    """Changement de dossier : retenu quelques secondes plus tard (une écriture, pas une par clic)."""
    if settings["startup_location"] != "last":
        return
    _pending["window"] = window
    if _pending["id"]:
        return

    def fire():
        _pending["id"] = 0
        w, _pending["window"] = _pending["window"], None
        if w is not None:
            remember(w)
        return False
    _pending["id"] = GLib.timeout_add_seconds(delay, fire)
