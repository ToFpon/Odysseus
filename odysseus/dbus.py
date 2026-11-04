"""Interface D-Bus org.freedesktop.FileManager1 : « Afficher dans le dossier » depuis Firefox,
Thunderbird, le gestionnaire de téléchargements… Activée seulement si l'utilisateur le demande
(Préférences), car elle prend la place de Nautilus pour ces demandes.

Odysseus fermé, le bus lance le programme du fichier de service de ce nom : celui de Nautilus
(/usr/share/dbus-1/services). Option active, on écrit le nôtre dans ~/.local/share/dbus-1/services,
qui passe avant ; on le retire quand l'option est coupée.

Propriété OpenWindowsWithLocations (a{sas}), publiée par Nautilus hors de la norme : chemin D-Bus de
chaque fenêtre (celui que GTK donne à la fenêtre : _GTK_WINDOW_OBJECT_PATH) → dossiers de ses onglets.
dash-to-dock s'en sert pour savoir si la « Corbeille » du dock a une fenêtre : sans elle, après un
premier clic, son icône restait « en cours de lancement » et les clics suivants ne faisaient plus rien.
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
    <property name="OpenWindowsWithLocations" type="a{sas}" access="read"/>
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


def _location_uri(pane) -> str:
    """URI de l'emplacement d'un panneau, comme g_file_get_uri (corbeille : « trash:/// »)."""
    if pane.mode == "archive":
        loc = pane.archive_path
    elif pane.mode == "search":
        loc = pane.search_root
    else:
        loc = pane.location
    if not loc:
        return ""
    if "://" in loc and not loc.startswith("/"):
        return Gio.File.new_for_uri(loc).get_uri()
    return Gio.File.new_for_path(loc).get_uri()


class FileManager1:
    def __init__(self, app):
        self.app = app
        self.owner_id = 0
        self.reg_id = 0
        self.conn = None
        self._last = None
        self._pending = 0
        self._app_ids = []

    def start(self):
        self.owner_id = Gio.bus_own_name(Gio.BusType.SESSION, NAME,
                                         Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
                                         self._acquired, None, None)
        self._app_ids = [self.app.connect(sig, lambda *_: self.queue_update())
                         for sig in ("window-added", "window-removed")]

    # -- OpenWindowsWithLocations ------------------------------------------------------------
    def locations(self) -> dict:
        """Chemin D-Bus de chaque fenêtre → URI des emplacements de ses onglets (comme Nautilus)."""
        from .ui.window import MainWindow
        base = self.app.get_dbus_object_path()
        out = {}
        if not base:
            return out
        for w in self.app.get_windows():
            if not isinstance(w, MainWindow) or not w.get_visible():
                continue
            uris = []
            for p in w.all_panes():
                uri = _location_uri(p)
                if uri and uri not in uris:
                    uris.append(uri)
            out[f"{base}/window/{w.get_id()}"] = uris
        return out

    def queue_update(self):
        """Fenêtre ouverte / fermée, onglet fermé, dossier changé : propriété republiée si elle change."""
        if self._pending or self.conn is None:
            return
        def run():
            self._pending = 0
            now = self.locations()
            if now != self._last:
                self._last = now
                try:
                    self.conn.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                          GLib.Variant("(sa{sv}as)", (NAME, {
                                              "OpenWindowsWithLocations": GLib.Variant("a{sas}", now)}, [])))
                except GLib.Error:
                    pass
            return False
        self._pending = GLib.timeout_add(100, run)

    def _get_property(self, _conn, _sender, _path, _iface, name):
        if name == "OpenWindowsWithLocations":
            self._last = self.locations()
            return GLib.Variant("a{sas}", self._last)
        return None

    def stop(self):
        for hid in self._app_ids:
            self.app.disconnect(hid)
        self._app_ids = []
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
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
        self.reg_id = conn.register_object(PATH, info, self._call, self._get_property, None)
        self.conn = conn
        self.queue_update()

    def _call(self, conn, sender, path, iface, method, params, invocation):
        uris = params.unpack()[0]
        paths = [Gio.File.new_for_uri(u).get_path() or u for u in uris]
        GLib.idle_add(self._handle, method, paths)
        invocation.return_value(None)

    def _handle(self, method, paths):
        if method == "ShowItemProperties":
            existing = [p for p in paths if os.path.lexists(p)]
            if existing:                 # chemin inexistant : pas de fenêtre de propriétés vide
                self.show_properties(existing)
            return False
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
        win.present()
        return False

    def show_properties(self, paths):
        """« Propriétés » du bureau (DING…) ou d'une autre application : seule la fenêtre des propriétés
        apparaît, comme chez Nautilus (avant : la fenêtre principale venait aussi au premier plan).
        Odysseus fermé : une fenêtre principale cachée sert d'hôte (permissions des fichiers inclus,
        « Ouvrir le dossier parent » qui l'affiche) et disparaît avec les propriétés si on ne l'a pas montrée."""
        from .ui.properties import PropertiesDialog
        from .ui.window import MainWindow
        host = self.app._window()
        hidden = None
        if host is None:
            hidden = host = MainWindow(self.app)
        dlg = PropertiesDialog(host, paths)
        dlg.set_application(self.app)            # garde Odysseus ouvert tant qu'elle l'est
        if hidden is not None:
            def closed(*_):
                def drop():
                    if not hidden.get_visible():
                        hidden.destroy()
                    return False
                GLib.idle_add(drop)              # « Ouvrir le dossier parent » ferme puis l'affiche
            dlg.connect("closed", closed)
        dlg.present()                            # sans parent : la fenêtre principale ne bouge pas
        return dlg
