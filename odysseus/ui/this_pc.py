"""Page « Ce PC », pour ceux qui viennent de Windows (« Ce PC » / « Poste de travail »).

Dossiers de l'utilisateur en haut, puis les disques avec une barre d'espace utilisé et l'espace
libre, puis les partages réseau connectés. Mêmes règles que la barre latérale : clic pour monter
un volume, jamais d'éjection pour un disque de /etc/fstab ou interne. Les tailles sont lues par
GIO en asynchrone (un téléphone ou un partage lent ne bloque pas l'affichage) ; la page suit les
branchements en direct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gi.repository import Gio, GLib, Graphene, Gtk, Pango

from .. import features
from ..backend import devices
from ..backend.engine import human_size
from ..i18n import tr
from .common import popup_menu

if TYPE_CHECKING:
    from .pane import Pane

COMPUTER_URI = "computer:///"
FULL = 0.9                      # au-delà, la barre passe à la couleur d'alerte du thème


class _Place:
    """Ce qu'il faut à Sidebar.mount_volume / eject : cible, montage, volume."""

    def __init__(self, target: Optional[str], mount=None, volume=None, ejectable=False):
        self.target = target
        self.mount = mount
        self.volume = volume
        self.ejectable = ejectable


class Tile(Gtk.FlowBoxChild):
    def __init__(self, place: _Place, gicon, name: str, drive: bool, tooltip: str = ""):
        super().__init__(css_classes=["thispc-tile"])
        self.place = place
        box = Gtk.Box(spacing=12)
        img = Gtk.Image(pixel_size=48)
        if isinstance(gicon, str):
            img.set_from_icon_name(gicon)
        elif gicon is not None:
            img.set_from_gicon(gicon)
        box.append(img)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        col.append(Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["heading"] if drive else []))
        self.bar = self.info = None
        if drive:
            self.bar = Gtk.LevelBar(visible=False, hexpand=True)
            for name_ in ("low", "high", "full"):
                self.bar.remove_offset_value(name_)
            self.bar.add_offset_value("disk-ok", FULL)
            self.bar.add_offset_value("disk-full", 1.0)
            col.append(self.bar)
            self.info = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
            if place.target is None:
                self.info.set_text(tr("Cliquez pour monter"))
                img.add_css_class("dim-label")
            col.append(self.info)
        box.append(col)
        self.set_child(box)
        if tooltip:
            self.set_tooltip_text(tooltip)

    def set_usage(self, free: int, total: int):
        if not total or self.bar is None:
            return
        self.bar.set_value(min(1.0, max(0.0, (total - free) / total)))
        self.bar.set_visible(True)
        self.info.set_text(tr("{0} libres sur {1}").format(human_size(free), human_size(total)))


class ThisPC(Gtk.Box):
    __gtype_name__ = "OdysseusThisPC"

    def __init__(self, pane: "Pane"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True,
                         css_classes=["this-pc"])
        self.pane = pane
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["thispc-content"])
        self.sections = {}
        for key, title in (("folders", tr("Dossiers")), ("drives", tr("Périphériques et lecteurs")),
                           ("network", tr("Emplacements réseau"))):
            label = Gtk.Label(label=title, xalign=0, css_classes=["other-heading"])
            box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
                              max_children_per_line=6, column_spacing=6, row_spacing=6,
                              css_classes=["thispc-flow"], valign=Gtk.Align.START)
            box.set_activate_on_single_click(True)
            box.connect("child-activated", lambda _b, tile: self._activate(tile))
            menu = Gtk.GestureClick(button=3)
            menu.connect("pressed", self._on_menu, box)
            box.add_controller(menu)
            mid = Gtk.GestureClick(button=2)
            mid.connect("pressed", self._on_middle, box)
            box.add_controller(mid)
            content.append(label)
            content.append(box)
            self.sections[key] = (label, box)
        self.append(Gtk.ScrolledWindow(child=content, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.monitor = Gio.VolumeMonitor.get()
        self._ids = []
        self._pending = 0
        self._cancel: Optional[Gio.Cancellable] = None

    # ------------------------------------------------------------------
    def refresh(self):
        if not self._ids:
            self._ids = [self.monitor.connect(sig, lambda *_: self._queue())
                         for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added",
                                     "volume-removed", "volume-changed")]
        self._fill()

    def stop(self):
        for i in self._ids:
            self.monitor.disconnect(i)
        self._ids = []
        if self._cancel is not None:
            self._cancel.cancel()
            self._cancel = None

    def _queue(self):
        if not self._pending:
            self._pending = GLib.timeout_add(300, self._fill)

    def _clear(self, key: str) -> Gtk.FlowBox:
        box = self.sections[key][1]
        while (c := box.get_first_child()) is not None:
            box.remove(c)
        return box

    def _fill(self):
        self._pending = 0
        if self._cancel is not None:
            self._cancel.cancel()
        self._cancel = Gio.Cancellable()
        from .sidebar import Sidebar, places
        folders = self._clear("folders")
        for icon, label, path in places(desktop=True, trash=False):
            folders.append(Tile(_Place(path), icon.replace("-symbolic", ""), label, False, path))

        drives = self._clear("drives")
        network = self._clear("network")
        fstab = devices.fstab_entries()
        root = _Place("/")
        self._add_drive(drives, root, Gio.ThemedIcon.new("drive-harddisk"), GLib.get_os_info("NAME") or "/",
                        Gio.File.new_for_path("/"), "/")
        seen = {"/"}
        for mount in self.monitor.get_mounts():
            if mount.is_shadowed():
                continue
            f = mount.get_root()
            path = f.get_path() if f else None
            target = path or (f.get_uri() if f else None)
            if not target or target in seen:
                continue
            seen.add(target)
            vol = mount.get_volume()
            place = _Place(target, mount, vol,
                           Sidebar._is_ejectable(path, Sidebar._unix_device(vol), mount.get_drive(), fstab))
            # GVfs : téléphones et appareils photo avec les disques, partages (smb, sftp…) à part
            gvfs = path is None or path.startswith(GLib.get_user_runtime_dir() + "/gvfs/")
            box = network if gvfs and "mtp" not in target and "gphoto2" not in target else drives
            self._add_drive(box, place, mount.get_icon(), mount.get_name(), f,
                            path or GLib.uri_unescape_string(target, None))
        for vol in self.monitor.get_volumes():
            if vol.get_mount() is None and vol.can_mount():
                place = _Place(None, None, vol,
                               Sidebar._is_ejectable(None, Sidebar._unix_device(vol), vol.get_drive(), fstab))
                drives.append(Tile(place, vol.get_icon(), vol.get_name(), True,
                                   vol.get_identifier("unix-device") or ""))
        for disk in devices.mounted_disks():
            if disk.path not in seen:
                seen.add(disk.path)
                self._add_drive(drives, _Place(disk.path), Gio.ThemedIcon.new("drive-harddisk"), disk.name,
                                Gio.File.new_for_path(disk.path), disk.path)
        has_net = network.get_first_child() is not None
        for w in self.sections["network"]:
            w.set_visible(has_net)
        return False

    def _add_drive(self, box: Gtk.FlowBox, place: _Place, gicon, name: str, f: Gio.File, tooltip: str):
        tile = Tile(place, gicon, name, True, tooltip)
        box.append(tile)

        def done(src, res):
            try:
                info = src.query_filesystem_info_finish(res)
            except GLib.Error:
                return
            tile.set_usage(info.get_attribute_uint64("filesystem::free"),
                           info.get_attribute_uint64("filesystem::size"))
        f.query_filesystem_info_async("filesystem::size,filesystem::free", GLib.PRIORITY_DEFAULT,
                                      self._cancel, done)

    # -- ouvrir, menus ----------------------------------------------------
    def _sidebar(self):
        return getattr(self.get_root(), "sidebar", None)

    def _activate(self, tile: Tile, where: str = "here"):
        place = tile.place
        if place.target is None:
            sb = self._sidebar()
            if sb is not None and place.volume is not None:
                sb.mount_volume(place, where)
            return
        if where == "here":
            self.pane.open(place.target)
        else:
            win = self.get_root()
            if hasattr(win, "open_location"):
                win.open_location(place.target, where)

    def _tile_at(self, box: Gtk.FlowBox, x: float, y: float) -> Optional[Tile]:
        child = box.get_child_at_pos(int(x), int(y))
        return child if isinstance(child, Tile) else None

    def _on_middle(self, gesture, _n, x, y, box):
        tile = self._tile_at(box, x, y)
        if tile is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._activate(tile, "tab")

    def _on_menu(self, gesture, _n, x, y, box):
        tile = self._tile_at(box, x, y)
        if tile is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        place = tile.place
        group = Gio.SimpleActionGroup()
        menu = Gio.Menu()
        s = Gio.Menu()

        def add(name, label, cb, section):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            group.add_action(a)
            section.append(label, f"pc.{name}")
        if place.target is None:
            add("mount", tr("Monter"), lambda: self._activate(tile), s)
        else:
            add("open", tr("Ouvrir"), lambda: self._activate(tile), s)
            add("tab", tr("Ouvrir dans un nouvel onglet"), lambda: self._activate(tile, "tab"), s)
            add("window", tr("Ouvrir dans une nouvelle fenêtre"), lambda: self._activate(tile, "window"), s)
            if features.enabled("dual_panel"):
                add("other", tr("Ouvrir dans l'autre panneau"), lambda: self._activate(tile, "other"), s)
        menu.append_section(None, s)
        sb = self._sidebar()
        m = place.mount
        if place.ejectable and sb is not None:
            s2 = Gio.Menu()
            if m is not None and (m.can_eject() or m.can_unmount()):
                add("eject", tr("Éjecter") if m.can_eject() else tr("Démonter"), lambda: sb.eject(place), s2)
            elif m is None and place.volume is not None and place.volume.can_eject():
                add("eject", tr("Éjecter"), lambda: sb.eject(place), s2)
            if s2.get_n_items():
                menu.append_section(None, s2)
        if place.target and "://" not in place.target:
            s3 = Gio.Menu()
            s3.append(tr("Propriétés"), "win.properties-path::" + place.target)
            menu.append_section(None, s3)
        self.insert_action_group("pc", group)
        ok, pt = box.compute_point(self, Graphene.Point().init(x, y))
        popup_menu(self, menu, pt.x if ok else x, pt.y if ok else y)
