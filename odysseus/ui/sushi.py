"""Aperçu rapide par Sushi (gnome-sushi), comme Nautilus 46 (nautilus-previewer.c) : Espace ouvre ou
ferme l'aperçu du fichier sélectionné ; tant qu'il est ouvert, il suit la sélection ; les flèches
tapées dans Sushi déplacent la sélection d'Odysseus (signal SelectionEvent).

Interface D-Bus org.gnome.NautilusPreviewer2 : ShowFile(uri, poignée de la fenêtre, fermer si déjà
visible), Close(), propriété Visible, signal SelectionEvent(direction GtkDirectionType)."""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gio, GLib, Gtk

NAME = "org.gnome.NautilusPreviewer"
PATH = "/org/gnome/NautilusPreviewer"
IFACE = "org.gnome.NautilusPreviewer2"


class Previewer:
    def __init__(self):
        self.proxy: Optional[Gio.DBusProxy] = None
        self.ready = False
        self._fetching = False
        self._pending = None                     # (uri, handle, close) demandé avant que Sushi réponde
        self.on_selection_event: Optional[Callable[[int], None]] = None
        self.owner = None                        # fenêtre qui a ouvert l'aperçu : elle seule le fait suivre
        self._visible = False                    # d'après les annonces de Sushi (PropertiesChanged)

    # ------------------------------------------------------------------
    def _ensure(self) -> bool:
        if self.ready:
            return True
        if not self._fetching:
            self._fetching = True
            Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION,
                                      None, NAME, PATH, IFACE, None, self._on_proxy)
        return False

    def _on_proxy(self, _src, res):
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(res)
        except GLib.Error:
            self._fetching = False
            return
        # Ping : démarre Sushi (activation D-Bus) et dit s'il est installé
        self.proxy.call("org.freedesktop.DBus.Peer.Ping", None, Gio.DBusCallFlags.NONE, -1, None, self._on_ping)

    def _on_ping(self, proxy, res):
        self._fetching = False
        try:
            proxy.call_finish(res)
        except GLib.Error:
            return
        self.ready = True
        conn = proxy.get_connection()
        conn.signal_subscribe(NAME, IFACE, "SelectionEvent", PATH, None, Gio.DBusSignalFlags.NONE, self._on_signal)
        # Sushi ne répond pas à Properties.Get (son objet D-Bus n'a pas de lecteur pour Visible) : il
        # annonce seulement l'ouverture et la fermeture. On écoute ces annonces nous-mêmes, comme le
        # cache de propriétés de Nautilus ; Sushi parti (bus), l'aperçu est fermé.
        conn.signal_subscribe(NAME, "org.freedesktop.DBus.Properties", "PropertiesChanged", PATH, IFACE,
                              Gio.DBusSignalFlags.NONE, self._on_properties)
        conn.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                              "/org/freedesktop/DBus", NAME, Gio.DBusSignalFlags.NONE, self._on_owner)
        if self._pending:
            self._call_show(*self._pending)
            self._pending = None

    def _on_signal(self, _conn, _sender, _path, _iface, _signal, params):
        self._visible = True                     # une flèche tapée dans Sushi : il est ouvert
        if self.on_selection_event is not None:
            self.on_selection_event(params.unpack()[0])

    def _on_properties(self, _conn, _sender, _path, _iface, _signal, params):
        _name, changed, _gone = params.unpack()
        if "Visible" in changed:
            self._visible = bool(changed["Visible"])

    def _on_owner(self, _conn, _sender, _path, _iface, _signal, params):
        if not params.unpack()[2]:
            self._visible = False

    # ------------------------------------------------------------------
    def visible(self) -> bool:
        """Aperçu ouvert, d'après les annonces de Sushi. Ni Properties.Get (Sushi n'y répond pas : vu chez
        Tof avec dbus-monitor, aucune réponse, l'aperçu ne suivait plus), ni le seul cache du proxy (vide
        quand Sushi démarre après la création du proxy, au premier Espace)."""
        if not self.ready or self.proxy is None:
            return False
        if self._visible:
            return True
        v = self.proxy.get_cached_property("Visible")
        return bool(v.unpack()) if v is not None else False

    def show(self, window: Gtk.Window, uri: str, close_if_visible: bool):
        self.owner = window
        handle = window_handle(window)
        if not self._ensure():
            self._pending = (uri, handle, close_if_visible)
            return
        self._call_show(uri, handle, close_if_visible)

    def _call_show(self, uri, handle, close_if_visible):
        self.proxy.call("ShowFile", GLib.Variant("(ssb)", (uri, handle, close_if_visible)),
                        Gio.DBusCallFlags.NONE, -1, None, self._done)

    def close(self):
        if self.ready and self.proxy is not None:
            # sans démarrer Sushi s'il ne tourne pas
            self.proxy.call("Close", None, Gio.DBusCallFlags.NO_AUTO_START, -1, None, self._done)

    @staticmethod
    def _done(proxy, res):
        try:
            proxy.call_finish(res)
        except GLib.Error:
            pass


def window_handle(window: Gtk.Window) -> str:
    """Poignée de la fenêtre parente, comme Nautilus (« x11:<xid en hexadécimal> ») : Sushi se pose
    au-dessus d'elle. Sous Wayland, sans poignée exportée, la valeur de repli de Nautilus."""
    surface = window.get_surface() if window is not None else None
    try:
        import gi
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
        if isinstance(surface, GdkX11.X11Surface):
            return "x11:%x" % surface.get_xid()
    except (ImportError, ValueError):
        pass
    return "x11:0"


previewer = Previewer()
