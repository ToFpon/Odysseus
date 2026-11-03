"""Odysseus gestionnaire de fichiers par défaut (Préférences → Général), un seul interrupteur :

- `inode/directory` → io.github.odysseus.Odysseus.desktop dans ~/.config/mimeapps.list, ce que fait
  `xdg-mime default io.github.odysseus.Odysseus.desktop inode/directory` (dossiers ouverts depuis le
  bureau, le dock, les autres applications) ;
- et réponse à org.freedesktop.FileManager1 (« Afficher dans le dossier » de Firefox, des
  téléchargements…, et la Corbeille de dash-to-dock, qui en a besoin : voir dbus.py).

Coupé : Nautilus redevient l'application par défaut (sinon les associations du système) et le nom
FileManager1 est rendu. L'état affiché est celui du système, relu à l'ouverture des Préférences.
Idée reprise de « Mira » (fork d'Odysseus d'un utilisateur)."""

from __future__ import annotations

from gi.repository import Gio, GLib

from . import APP_ID
from .settings import settings

DESKTOP = APP_ID + ".desktop"
NAUTILUS = "org.gnome.Nautilus.desktop"
MIME = "inode/directory"
COMMAND = f"xdg-mime default {DESKTOP} {MIME}"


def installed() -> bool:
    """Le fichier .desktop d'Odysseus est-il installé ? (pas lancé depuis les sources seulement)"""
    try:
        return Gio.DesktopAppInfo.new(DESKTOP) is not None
    except (TypeError, GLib.Error):
        return False


def is_default() -> bool:
    app = Gio.AppInfo.get_default_for_type(MIME, False)
    return app is not None and app.get_id() == DESKTOP


def set_default(on: bool) -> bool:
    """Fait (ou défait) d'Odysseus le gestionnaire de fichiers par défaut. True si c'est fait."""
    try:
        if on:
            app = Gio.DesktopAppInfo.new(DESKTOP)
            if app is None:
                return False
            app.set_as_default_for_type(MIME)
        else:
            nautilus = Gio.DesktopAppInfo.new(NAUTILUS)
            if nautilus is not None:
                nautilus.set_as_default_for_type(MIME)
            else:
                Gio.AppInfo.reset_type_associations(MIME)
    except GLib.Error as e:
        print("odysseus: gestionnaire par défaut :", e.message)
        return False
    settings["dbus_file_manager"] = on          # « Afficher dans le dossier » et Corbeille du dock
    return True


def reconcile():
    """Au démarrage : Odysseus gestionnaire par défaut (mimeapps.list) mais « Afficher dans le
    dossier » coupé — un vieux settings.json remis en place (vu chez Tof après un Timeshift) : la
    Corbeille du dock et Firefox repartaient chez Nautilus alors que l'interrupteur paraissait actif.
    On remet les deux d'accord. Seulement dans ce sens : un FileManager1 activé à part (ancien
    interrupteur, avant 1.7.6) n'est jamais coupé d'office."""
    if not settings["dbus_file_manager"] and installed() and is_default():
        settings["dbus_file_manager"] = True
