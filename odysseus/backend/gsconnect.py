"""Envoi de fichiers vers un téléphone avec GSConnect (KDE Connect pour GNOME).

Même mécanisme que nautilus-gsconnect.py, fourni par l'extension GSConnect, mais sans passer par
Nautilus : on suit les appareils publiés par le service D-Bus de GSConnect (ObjectManager), et
on envoie chaque fichier par l'action « shareFile » de l'appareil. Les appareils qui se
connectent ou se déconnectent sont suivis en direct ; sans GSConnect, rien n'apparaît.
"""

from __future__ import annotations

from typing import Optional

from gi.repository import Gio, GLib

SERVICE_NAME = "org.gnome.Shell.Extensions.GSConnect"
SERVICE_PATH = "/org/gnome/Shell/Extensions/GSConnect"
DEVICE_IFACE = "org.gnome.Shell.Extensions.GSConnect.Device"


class Devices:
    """Appareils GSConnect connus : chemin D-Bus → (nom, groupe d'actions de l'appareil)."""

    def __init__(self):
        self.devices: dict = {}
        self._proxy: Optional[Gio.DBusProxy] = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
                                  SERVICE_NAME, SERVICE_PATH, "org.freedesktop.DBus.ObjectManager",
                                  None, self._ready, None)

    def _ready(self, _src, res, _data):
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_finish(res)
        except GLib.Error:
            return                       # pas de bus de session : rien à proposer
        self._proxy.connect("notify::g-name-owner", lambda *_: self._refresh())
        self._proxy.connect("g-signal", self._on_signal)
        self._refresh()

    def _add(self, path: str, props: dict):
        props = props.get(DEVICE_IFACE)
        if not props or "Name" not in props:
            return
        group = Gio.DBusActionGroup.get(self._proxy.get_connection(), SERVICE_NAME, path)
        group.list_actions()             # charge les actions (et leur état) en arrière-plan
        self.devices[path] = (props["Name"], group)

    def _refresh(self):
        self.devices = {}
        if self._proxy is None or self._proxy.get_name_owner() is None:
            return
        self._proxy.call("GetManagedObjects", None, Gio.DBusCallFlags.NO_AUTO_START, -1, None,
                         self._got_objects, None)

    def _got_objects(self, proxy, res, _data):
        try:
            objects = proxy.call_finish(res)[0]
        except GLib.Error:
            return
        for path, props in objects.items():
            self._add(path, props)

    def _on_signal(self, proxy, _sender, signal, params):
        if proxy.get_name_owner() is None:
            return
        if signal == "InterfacesAdded":
            path, props = params.unpack()
            self._add(path, props)
        elif signal == "InterfacesRemoved":
            path, _ifaces = params.unpack()
            self.devices.pop(path, None)

    def capable(self) -> list[tuple[str, str]]:
        """[(chemin, nom)] des appareils qui acceptent des fichiers maintenant (connectés)."""
        out = []
        for path, (name, group) in self.devices.items():
            if group.has_action("shareFile") and group.get_action_enabled("shareFile"):
                out.append((path, name))
        return sorted(out, key=lambda d: d[1].casefold())

    def send(self, path: str, uris: list[str]) -> bool:
        dev = self.devices.get(path)
        if dev is None:
            return False
        for uri in uris:
            dev[1].activate_action("shareFile", GLib.Variant("(sb)", (uri, False)))
        return True


devices = Devices()
