"""Page « Autres emplacements », comme dans Nautilus.

Sur cet appareil : la racine (nom du système) et les disques, avec l'espace libre et le
périphérique. Réseaux : partages connectés et emplacements découverts par GVfs (Freebox, smb,
dns-sd…), mis à jour en direct. En bas : se connecter à un serveur par son adresse.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Optional

from gi.repository import Gio, GLib, Gtk, Pango

from ..backend import devices, scanner
from ..backend.engine import human_size
from ..i18n import tr

if TYPE_CHECKING:
    from .pane import Pane

OTHER_URI = "other-locations:///"
NETWORK_URI = "network:///"


def _usage(path: str) -> str:
    try:
        st = os.statvfs(path)
    except OSError:
        return ""
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if not total:
        return ""
    return tr("{0} / {1} de disponibles").format(human_size(free), human_size(total))


class LocationRow(Gtk.ListBoxRow):
    def __init__(self, gicon, name: str, usage: str = "", where: str = "", activate=None, tooltip: str = ""):
        super().__init__(activatable=True, css_classes=["other-row"])
        self.activate_cb = activate
        box = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        img = Gtk.Image(pixel_size=32)
        if isinstance(gicon, str):
            img.set_from_icon_name(gicon)
        elif gicon is not None:
            img.set_from_gicon(gicon)
        box.append(img)
        box.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        if usage:
            box.append(Gtk.Label(label=usage, xalign=1, css_classes=["dim-label"]))
        box.append(Gtk.Label(label=where, xalign=1, width_chars=16, ellipsize=Pango.EllipsizeMode.MIDDLE,
                             css_classes=["dim-label"]))
        self.set_child(box)
        if tooltip:
            self.set_tooltip_text(tooltip)


def connect_bar(owner, pane: "Pane") -> Gtk.Box:
    """Barre « Se connecter à un serveur » (bas d'« Autres emplacements » et de « Ce PC ») ;
    pose owner.address et owner.connect_btn."""
    bar = Gtk.Box(spacing=6, css_classes=["other-connect"])
    owner.address = Gtk.Entry(hexpand=True, placeholder_text=tr("Saisir l'adresse du serveur…"),
                              secondary_icon_name="dialog-question-symbolic",
                              secondary_icon_tooltip_text=tr("Adresses prises en charge : smb://, sftp://, "
                                                             "ftp://, dav://, davs://, nfs://, afp://…"))
    owner.connect_btn = Gtk.Button(label=tr("Se connecter"), sensitive=False)

    def go(*_):
        text = owner.address.get_text().strip()
        if not text:
            return
        if "://" not in text:
            text = "smb://" + text.lstrip("/")
        pane.open(text)
    owner.address.connect("activate", go)
    owner.address.connect("changed", lambda e: owner.connect_btn.set_sensitive(bool(e.get_text().strip())))
    owner.connect_btn.connect("clicked", go)
    bar.append(owner.address)
    bar.append(owner.connect_btn)
    return bar


def discover_network(callback) -> None:
    """Emplacements réseau découverts par GVfs (network:///), lus dans un fil ; callback(recs)
    appelé dans la boucle GTK."""
    def work():
        try:
            recs = scanner.scan_gio(Gio.File.new_for_uri(NETWORK_URI), threading.Event())
        except Exception:  # noqa: BLE001 — réseau indisponible (GVfs absent…)
            recs = []
        GLib.idle_add(lambda: (callback(recs), False)[1])
    threading.Thread(target=work, daemon=True).start()


class OtherLocations(Gtk.Box):
    __gtype_name__ = "OdysseusOtherLocations"

    def __init__(self, pane: "Pane"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True,
                         css_classes=["other-locations"])
        self.pane = pane
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(Gtk.Label(label=tr("Sur cet appareil"), xalign=0, css_classes=["other-heading"]))
        self.dev_list = self._list()
        content.append(self.dev_list)
        content.append(Gtk.Label(label=tr("Réseaux"), xalign=0, css_classes=["other-heading"]))
        self.net_list = self._list()
        content.append(self.net_list)
        self.net_status = Gtk.Label(xalign=0, css_classes=["dim-label", "other-status"])
        content.append(self.net_status)
        self.append(Gtk.ScrolledWindow(child=content, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.append(connect_bar(self, pane))

        self.monitor = Gio.VolumeMonitor.get()
        self._ids = [self.monitor.connect(sig, lambda *_: self._queue_devices())
                     for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added",
                                 "volume-removed", "volume-changed")]
        self._dev_pending = False
        self._net_monitor: Optional[Gio.FileMonitor] = None
        self._net_pending = 0
        self._net_token = 0
        self._net_items: list = []

    def _list(self) -> Gtk.ListBox:
        lb = Gtk.ListBox(css_classes=["other-list"], selection_mode=Gtk.SelectionMode.NONE)
        lb.set_activate_on_single_click(True)
        lb.connect("row-activated", lambda _lb, row: row.activate_cb and row.activate_cb())
        return lb

    # ------------------------------------------------------------------
    def refresh(self):
        self._fill_devices()
        self._watch_network()
        self._load_network()

    def stop(self):
        if self._net_monitor is not None:
            self._net_monitor.cancel()
            self._net_monitor = None
        self._net_token += 1

    # -- Sur cet appareil ------------------------------------------------
    def _queue_devices(self):
        if not self._dev_pending:
            self._dev_pending = True
            GLib.timeout_add(300, self._fill_devices)

    def _fill_devices(self):
        self._dev_pending = False
        while (c := self.dev_list.get_first_child()) is not None:
            self.dev_list.remove(c)
        self.dev_list.append(LocationRow(Gio.ThemedIcon.new("drive-harddisk"), GLib.get_os_info("NAME") or "/",
                                         _usage("/"), "/", lambda: self.pane.open("/")))
        seen = {"/"}
        for mount in self.monitor.get_mounts():
            root = mount.get_root()
            path = root.get_path() if root else None
            if mount.is_shadowed() or not path or path in seen:
                continue
            seen.add(path)
            vol = mount.get_volume()
            dev = vol.get_identifier("unix-device") if vol is not None else None
            self.dev_list.append(LocationRow(mount.get_icon(), mount.get_name(), _usage(path), dev or path,
                                             lambda p=path: self.pane.open(p), tooltip=path))
        for disk in devices.mounted_disks():
            if disk.path not in seen:
                seen.add(disk.path)
                self.dev_list.append(LocationRow(Gio.ThemedIcon.new("drive-harddisk"), disk.name,
                                                 _usage(disk.path), disk.path,
                                                 lambda p=disk.path: self.pane.open(p)))
        for vol in self.monitor.get_volumes():
            if vol.get_mount() is None and vol.can_mount():
                self.dev_list.append(LocationRow(vol.get_icon(), vol.get_name(), "",
                                                 vol.get_identifier("unix-device") or "",
                                                 lambda v=vol: self._mount_volume(v),
                                                 tooltip=tr("Cliquez pour monter")))
        return False

    def _mount_volume(self, vol: Gio.Volume):
        op = Gtk.MountOperation.new(self.get_root())

        def done(v, res):
            try:
                v.mount_finish(res)
            except GLib.Error as e:
                self.pane.emit("message", e.message)
                return
            m = v.get_mount()
            if m is not None and m.get_root().get_path():
                self.pane.open(m.get_root().get_path())
        vol.mount(Gio.MountMountFlags.NONE, op, None, done)

    # -- Réseaux -----------------------------------------------------------
    def _watch_network(self):
        if self._net_monitor is not None:
            return
        try:
            self._net_monitor = Gio.File.new_for_uri(NETWORK_URI).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        self._net_monitor.set_rate_limit(500)
        self._net_monitor.connect("changed", lambda *_: self._queue_network())

    def _queue_network(self):
        if not self._net_pending:
            self._net_pending = GLib.timeout_add(400, self._load_network)

    def _load_network(self):
        self._net_pending = 0
        self._net_token += 1
        token = self._net_token
        if not self._net_items:
            self.net_status.set_text(tr("Recherche des emplacements réseau…"))

        discover_network(lambda recs: self._network_loaded(token, recs))
        return False

    def _network_loaded(self, token, recs):
        if token != self._net_token:
            return False
        self._net_items = recs
        while (c := self.net_list.get_first_child()) is not None:
            self.net_list.remove(c)
        # partages déjà connectés (smb, sftp…) puis emplacements découverts
        n = 0
        for mount in self.monitor.get_mounts():
            root = mount.get_root()
            if root is None or root.get_path() or mount.is_shadowed():
                continue
            uri = root.get_uri()
            self.net_list.append(LocationRow(mount.get_icon(), mount.get_name(), "", GLib.uri_unescape_string(uri, None),
                                             lambda u=uri: self.pane.open(u)))
            n += 1
        for rec in scanner.sort_recs(recs, "name"):
            icon = rec.get("gicon") or "network-server"
            target = rec.get("target_uri") or rec.uri
            if rec.get("mountable"):
                cb = (lambda r=rec: self.pane._mount_and_open(r))
            else:
                cb = (lambda t=target: self.pane.open(t))
            self.net_list.append(LocationRow(icon, rec.name, "", "", cb,
                                             tooltip=GLib.uri_unescape_string(target, None) if target else ""))
            n += 1
        self.net_list.set_visible(n > 0)
        self.net_status.set_visible(n == 0)
        self.net_status.set_text(tr("Aucun emplacement réseau trouvé"))
        return False
