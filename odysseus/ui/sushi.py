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
        proxy.get_connection().signal_subscribe(NAME, IFACE, "SelectionEvent", PATH, None,
                                                Gio.DBusSignalFlags.NONE, self._on_signal)
        if self._pending:
            self._call_show(*self._pending)
            self._pending = None

    def _on_signal(self, _conn, _sender, _path, _iface, _signal, params):
        if self.on_selection_event is not None:
            self.on_selection_event(params.unpack()[0])

    # ------------------------------------------------------------------
    def visible(self) -> bool:
        """État mémorisé par le proxy (peut manquer : voir query_visible)."""
        if not self.ready or self.proxy is None:
            return False
        v = self.proxy.get_cached_property("Visible")
        return bool(v.unpack()) if v is not None else False

    def query_visible(self, callback: Callable[[bool], None]):
        """Demande à Sushi s'il est ouvert. Le cache du proxy ne suffit pas : Sushi lancé par le Ping
        après la création du proxy (cas normal : il démarre au premier Espace), la propriété Visible
        n'y était pas toujours, et l'aperçu ne suivait plus la sélection."""
        if not self.ready or self.proxy is None:
            return

        def done(conn, res):
            try:
                v = conn.call_finish(res).unpack()[0]
            except GLib.Error:
                return
            callback(bool(v))
        self.proxy.get_connection().call(NAME, PATH, "org.freedesktop.DBus.Properties", "Get",
                                         GLib.Variant("(ss)", (IFACE, "Visible")), GLib.VariantType("(v)"),
                                         Gio.DBusCallFlags.NO_AUTO_START, -1, None, done)

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
