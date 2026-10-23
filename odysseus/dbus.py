"""Interface D-Bus org.freedesktop.FileManager1 : « Afficher dans le dossier » depuis Firefox,
Thunderbird, le gestionnaire de téléchargements… Activée seulement si l'utilisateur le demande
(Préférences), car elle prend la place de Nautilus pour ces demandes.

Odysseus fermé, le bus lance le programme du fichier de service de ce nom : celui de Nautilus
(/usr/share/dbus-1/services). Option active, on écrit le nôtre dans ~/.local/share/dbus-1/services,
qui passe avant ; on le retire quand l'option est coupée.
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

from . import APP_ID

NAME = "org.freedesktop.FileManager1"
PATH = "/org/freedesktop/FileManager1"
XML = """<node>
  <interface name="org.freedesktop.FileManager1">
    <method name="ShowFolders"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
    <method name="ShowItems"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
    <method name="ShowItemProperties"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  </interface>
</node>"""


SERVICE_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                           "dbus-1", "services")
SERVICE_FILE = os.path.join(SERVICE_DIR, NAME + ".service")
MARK = "# Odysseus"


def _launcher() -> str:
    """Commande qui lance Odysseus : celle de son service D-Bus (écrit par install.sh dans
    ~/.local/share, ou installé par le paquet dans /usr/share), sinon bin/odysseus du dépôt."""
    dirs = [SERVICE_DIR] + [os.path.join(d, "dbus-1", "services")
                            for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
                            if d]
    for folder in dirs:
        try:
            with open(os.path.join(folder, APP_ID + ".service"), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Exec="):
                        return line[5:].strip()
        except OSError:
            pass
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "odysseus")
    return f"{here} --gapplication-service" if os.access(here, os.X_OK) else ""


def _ours() -> bool:
    try:
        with open(SERVICE_FILE, encoding="utf-8") as fh:
            return fh.readline().strip() == MARK
    except OSError:
        return False


def sync_service(enabled: bool):
    """Fichier de service FileManager1 présent si et seulement si l'option est active. Un fichier
    qui n'est pas le nôtre n'est jamais touché."""
    exists = os.path.lexists(SERVICE_FILE)
    if not enabled:
        if exists and _ours():
            try:
                os.unlink(SERVICE_FILE)
            except OSError:
                pass
        return
    if exists and not _ours():
        return
    command = _launcher()
    if not command:
        return
    text = f"{MARK}\n[D-BUS Service]\nName={NAME}\nExec={command}\n"
    try:
        os.makedirs(SERVICE_DIR, exist_ok=True)
        tmp = SERVICE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, SERVICE_FILE)
    except OSError:
        pass


class FileManager1:
    def __init__(self, app):
        self.app = app
        self.owner_id = 0
        self.reg_id = 0

    def start(self):
        self.owner_id = Gio.bus_own_name(Gio.BusType.SESSION, NAME,
                                         Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
                                         self._acquired, None, None)

    def stop(self):
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
            self.owner_id = 0
        if self.reg_id:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            conn.unregister_object(self.reg_id)
            self.reg_id = 0

    def _acquired(self, conn, _name):
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        # register_object() de PyGObject correspond à g_dbus_connection_register_object_with_closures()
        self.reg_id = conn.register_object(PATH, info, self._call, None, None)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        uris = params.unpack()[0]
        paths = [Gio.File.new_for_uri(u).get_path() or u for u in uris]
        GLib.idle_add(self._handle, method, paths)
        invocation.return_value(None)

    def _handle(self, method, paths):
        win = self.app._window() or self.app._new_window()
        if method == "ShowFolders":
            for i, p in enumerate(paths):
                win.open_location(p) if i == 0 else win.new_tab(p)
        elif method == "ShowItems":
            # un onglet par dossier, avec tous ses éléments sélectionnés (avant : un onglet par
            # fichier, et la sélection seulement dans le premier)
            groups: dict = {}
            for p in paths:
                groups.setdefault(os.path.dirname(p.rstrip("/")) or "/", []).append(os.path.basename(p.rstrip("/")))
            for i, (folder, names) in enumerate(groups.items()):
                if i == 0:
                    win.pane.open(folder, select=names)
                else:
                    ws = win.new_tab(folder)
                    ws.active.open(folder, push=False, select=names)
        elif method == "ShowItemProperties":
            existing = [p for p in paths if os.path.lexists(p)]
            if existing:                 # chemin inexistant : pas de fenêtre de propriétés vide
                win._show_properties(existing)
        win.present()
        return False
