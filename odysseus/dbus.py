"""Interface D-Bus org.freedesktop.FileManager1 : « Afficher dans le dossier » depuis Firefox,
Thunderbird, le gestionnaire de téléchargements… Activée seulement si l'utilisateur le demande
(Préférences), car elle prend la place de Nautilus pour ces demandes.
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

NAME = "org.freedesktop.FileManager1"
PATH = "/org/freedesktop/FileManager1"
XML = """<node>
  <interface name="org.freedesktop.FileManager1">
    <method name="ShowFolders"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
    <method name="ShowItems"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
    <method name="ShowItemProperties"><arg type="as" name="URIs" direction="in"/><arg type="s" name="StartupId" direction="in"/></method>
  </interface>
</node>"""


class FileManager1:
    def __init__(self, app):
        self.app = app
        self.owner_id = 0
        self.reg_id = 0

    def start(self):
        self.owner_id = Gio.bus_own_name(Gio.BusType.SESSION, NAME,
                                         Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
                                         self._acquired, None, None)

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
