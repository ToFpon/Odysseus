"""Barre latérale à la manière de Nautilus : emplacements, signets, périphériques.

Les signets sont ceux de GTK (partagés avec Nautilus). Les périphériques reprennent la logique
de Pressoir : clés USB et disques amovibles (montage en un clic, bouton d'éjection) et disques
montés en permanence ; jamais de bouton d'éjection pour un disque de /etc/fstab ni un disque interne.
"""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk

from .. import features
from ..backend import bookmarks, devices
from ..backend.engine import human_size
from ..i18n import tr
from ..settings import settings
from .common import alert, ask_text, popup_menu, toast

TRASH = "trash:///"

PRIVACY_SCHEMA = "org.gnome.desktop.privacy"


COMPUTER = "computer:///"


def places(desktop: bool = True, trash: bool = True) -> list[tuple[str, str, str]]:
    home = GLib.get_home_dir()
    out = [("user-home-symbolic", tr("Dossier personnel"), home)]
    for d, icon, label in (
            (GLib.UserDirectory.DIRECTORY_DESKTOP, "user-desktop-symbolic", tr("Bureau")),
            (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "folder-documents-symbolic", tr("Documents")),
            (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "folder-download-symbolic", tr("Téléchargements")),
            (GLib.UserDirectory.DIRECTORY_MUSIC, "folder-music-symbolic", tr("Musique")),
            (GLib.UserDirectory.DIRECTORY_PICTURES, "folder-pictures-symbolic", tr("Images")),
            (GLib.UserDirectory.DIRECTORY_VIDEOS, "folder-videos-symbolic", tr("Vidéos"))):
        if d == GLib.UserDirectory.DIRECTORY_DESKTOP and not desktop:
            continue
        p = GLib.get_user_special_dir(d)
        if p and p != home and os.path.isdir(p):
            out.append((icon, label, p))
    if trash:
        out.append(("user-trash-symbolic", tr("Corbeille"), TRASH))
    return out


def _gvfs(scheme: str) -> bool:
    return scheme in Gio.Vfs.get_default().get_supported_uri_schemes()


class SidebarRow(Gtk.ListBoxRow):
    def __init__(self, icon, label: str, target: Optional[str], kind: str = "place", tooltip: str = ""):
        super().__init__(css_classes=["sidebar-row"])
        self.target = target
        self.kind = kind
        self.mount = None
        self.volume = None
        box = Gtk.Box(spacing=12, margin_start=6, margin_end=4, margin_top=1, margin_bottom=1)
        self.icon = Gtk.Image()
        if isinstance(icon, str):
            self.icon.set_from_icon_name(icon)
        elif icon is not None:
            self.icon.set_from_gicon(icon)
        box.append(self.icon)
        self.label = Gtk.Label(label=label, xalign=0, ellipsize=3, hexpand=True)
        box.append(self.label)
        self.box = box
        self.set_child(box)
        self.set_tooltip_text(tooltip or (target if target and "://" not in target else None))


class Sidebar(Gtk.Box):
    __gtype_name__ = "OdysseusSidebar"
    __gsignals__ = {
        "open": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),      # cible, où : here / tab / other
        "drop": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.current = ""
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_bottom=12, margin_top=4)
        scroll.set_child(self.box)
        self.append(scroll)
        self.lists: list[Gtk.ListBox] = []
        self.places_list = self._section(None)
        self.recent_row = None
        if _gvfs("recent"):
            self.recent_row = SidebarRow("document-open-recent-symbolic", tr("Récents"), "recent:///")
            self.places_list.append(self.recent_row)
            # comme Nautilus : masqué quand « Historique des fichiers » est désactivé dans les
            # Paramètres de GNOME (Vie privée), et suivi en direct
            src = Gio.SettingsSchemaSource.get_default()
            if src is not None and src.lookup(PRIVACY_SCHEMA, True) is not None:
                self._privacy = Gio.Settings.new(PRIVACY_SCHEMA)     # gardé : sinon plus de signal
                self._privacy.connect("changed::remember-recent-files", lambda *_: self._sync_recent())
                self._sync_recent()
        self.fill_places()
        # comme Nautilus : un simple séparateur avant les signets (masqué s'il n'y en a pas)
        self.bm_list = self._section(None)
        self.bm_label = self.bm_list.get_prev_sibling()
        self.dev_label, self.dev_list = self._section(tr("Périphériques"), with_label=True)
        self.other_list = self._section(None)
        # disques, réseau et connexion à un serveur : page « Autres emplacements », comme Nautilus
        self.other_list.append(SidebarRow("list-add-symbolic", tr("Autres emplacements"), "other-locations:///"))
        self._fill_bookmarks()
        self._watch_bookmarks()
        self._watch_devices()

    def fill_places(self):
        """Emplacements : « Ce PC » (option, comme sous Windows), Récents, dossier personnel et
        dossiers de l'utilisateur (Bureau en option), Corbeille. Suit les Préférences en direct."""
        row = self.places_list.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            if row is not self.recent_row:
                self.places_list.remove(row)
            row = nxt
        if settings["sidebar_this_pc"]:
            self.places_list.prepend(SidebarRow("computer-symbolic", tr("Ce PC"), COMPUTER))
        for icon, label, target in places(desktop=settings["sidebar_desktop"]):
            self.places_list.append(SidebarRow(icon, label, target))
        self.select(self.current)

    def _section(self, title, with_label=False):
        label = None
        if title:
            label = Gtk.Label(label=title, xalign=0, css_classes=["sidebar-heading"])
            self.box.append(label)
        else:
            self.box.append(Gtk.Separator(css_classes=["sidebar-separator"]) if self.lists else Gtk.Box())
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        lb.connect("row-activated", self._on_row)
        menu = Gtk.GestureClick(button=3)
        menu.connect("pressed", self._on_menu, lb)
        lb.add_controller(menu)
        mid = Gtk.GestureClick(button=2)
        mid.connect("pressed", self._on_middle, lb)
        lb.add_controller(mid)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        drop.connect("motion", self._drop_motion, lb)
        drop.connect("leave", lambda *_a, lb=lb: self._drop_clear(lb))
        drop.connect("drop", self._on_drop, lb)
        lb.add_controller(drop)
        self.box.append(lb)
        self.lists.append(lb)
        return (label, lb) if with_label else lb

    # -- signets ---------------------------------------------------------------
    def _watch_bookmarks(self):
        try:
            f = Gio.File.new_for_path(bookmarks.BOOKMARKS_FILE)
            self._bm_monitor = f.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._bm_monitor.connect("changed", lambda *_: GLib.timeout_add(100, self._fill_bookmarks))
        except GLib.Error:
            self._bm_monitor = None

    def _fill_bookmarks(self):
        while (c := self.bm_list.get_first_child()) is not None:
            self.bm_list.remove(c)
        items = bookmarks.load()
        for b in items:
            p = b.path
            if p is not None and not os.path.exists(p):
                continue
            icon = "folder-symbolic" if p else "folder-remote-symbolic"
            self.bm_list.append(SidebarRow(icon, b.title, p or b.uri, kind="bookmark",
                                           tooltip=p or GLib.uri_unescape_string(b.uri, None)))
        has = self.bm_list.get_first_child() is not None
        self.bm_label.set_visible(has)
        self.bm_list.set_visible(has)
        self.select(self.current)
        return False

    # -- périphériques (Pressoir) ---------------------------------------------------
    def _watch_devices(self):
        self.monitor = Gio.VolumeMonitor.get()
        for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added", "volume-removed",
                    "volume-changed", "drive-connected", "drive-disconnected"):
            self.monitor.connect(sig, lambda *_: self._queue_devices())
        self._mounts_sig = devices.mounts_signature()
        GLib.timeout_add_seconds(3, self._poll_mounts)
        self._fill_devices()

    def _poll_mounts(self):
        sig = devices.mounts_signature()
        if sig != self._mounts_sig:
            self._mounts_sig = sig
            self._fill_devices()
        return True

    def _queue_devices(self):
        if not getattr(self, "_dev_pending", False):
            self._dev_pending = True
            GLib.timeout_add(250, self._fill_devices)

    @staticmethod
    def _unix_device(volume) -> str:
        dev = volume.get_identifier("unix-device") if volume is not None else None
        return os.path.realpath(dev) if dev else ""

    @staticmethod
    def _is_ejectable(path, device: str, drive, fstab: tuple) -> bool:
        points, devs = fstab
        if (path and path.rstrip("/") in points) or (device and device in devs):
            return False
        if drive is not None and not (drive.is_removable() or drive.can_eject() or drive.is_media_removable()):
            return False
        return True

    @staticmethod
    def _space(path: str) -> str:
        try:
            s = os.statvfs(path)
            return tr("{0} libres sur {1}").format(human_size(s.f_bavail * s.f_frsize),
                                                    human_size(s.f_blocks * s.f_frsize))
        except OSError:
            return path

    def _device_row(self, label, icon, target, mount=None, volume=None, tooltip="", ejectable=False):
        row = SidebarRow(icon, label, target, kind="device", tooltip=tooltip)
        row.mount, row.volume = mount, volume
        can = ejectable and ((mount is not None and (mount.can_eject() or mount.can_unmount())) or
                             (mount is None and volume is not None and volume.can_eject()))
        if can:
            btn = Gtk.Button(icon_name="media-eject-symbolic", css_classes=["flat", "circular", "row-eject"],
                             valign=Gtk.Align.CENTER,
                             tooltip_text=tr("Éjecter") if (mount and mount.can_eject()) or (not mount and volume)
                             else tr("Démonter"))
            btn.connect("clicked", lambda *_: self.eject(row))
            row.box.append(btn)
        if target is None:
            row.label.add_css_class("dim-label")
        return row

    def _fill_devices(self):
        self._dev_pending = False
        while (c := self.dev_list.get_first_child()) is not None:
            self.dev_list.remove(c)
        rows, seen = [], set()
        fstab = devices.fstab_entries()
        for mount in self.monitor.get_mounts():
            if mount.is_shadowed():
                continue
            root = mount.get_root()
            path = root.get_path() if root else None
            target = path or (root.get_uri() if root else None)
            if not target or target in seen:
                continue
            seen.add(target)
            rows.append(self._device_row(mount.get_name(), mount.get_symbolic_icon(), target, mount,
                                         mount.get_volume(), self._space(path) if path else target,
                                         self._is_ejectable(path, self._unix_device(mount.get_volume()),
                                                            mount.get_drive(), fstab)))
        for volume in self.monitor.get_volumes():
            if volume.get_mount() is None and volume.can_mount():
                rows.append(self._device_row(volume.get_name(), volume.get_symbolic_icon(), None, None, volume,
                                             tr("Cliquez pour monter"),
                                             self._is_ejectable(None, self._unix_device(volume),
                                                                volume.get_drive(), fstab)))
        for disk in devices.mounted_disks():
            if disk.path not in seen:
                seen.add(disk.path)
                rows.append(self._device_row(disk.name, "drive-harddisk-symbolic", disk.path,
                                             tooltip=disk.path + "\n" + self._space(disk.path)))
        for r in rows:
            self.dev_list.append(r)
        self.dev_label.set_visible(bool(rows))
        self.dev_list.set_visible(bool(rows))
        self.select(self.current)
        return False

    def mount_volume(self, row: SidebarRow, where: str = "here"):
        op = Gtk.MountOperation.new(self.get_root())

        def done(vol, res):
            try:
                vol.mount_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
                    alert(self, tr("Impossible de monter {0}").format(vol.get_name()), e.message)
                return
            mount = vol.get_mount()
            root = mount.get_root() if mount else None
            self._fill_devices()
            if root:
                self.emit("open", root.get_path() or root.get_uri(), where)
        row.volume.mount(Gio.MountMountFlags.NONE, op, None, done)

    def eject(self, row: SidebarRow):
        mount, volume = row.mount, row.volume
        name = (mount or volume).get_name()
        win = self.get_root()
        if row.target and hasattr(win, "leave_location"):
            win.leave_location(row.target)
        op = Gtk.MountOperation.new(win)

        def done(obj, res, finish):
            try:
                finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
                    alert(self, tr("Impossible d'éjecter {0}").format(name), e.message)
                return
            toast(self, tr("{0} peut être retiré en toute sécurité").format(name))
            self._fill_devices()
        if mount is not None and mount.can_eject():
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None,
                                       lambda o, r: done(o, r, o.eject_with_operation_finish))
        elif mount is not None:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None,
                                         lambda o, r: done(o, r, o.unmount_with_operation_finish))
        elif volume is not None:
            volume.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None,
                                        lambda o, r: done(o, r, o.eject_with_operation_finish))

    # -- sélection / activation -----------------------------------------------
    def select(self, target: str):
        """Sélectionne la ligne de l'emplacement affiché, comme Nautilus."""
        self.current = target
        found = False
        for lb in self.lists:
            match = None
            if not found:
                row = lb.get_first_child()
                while row is not None:
                    if isinstance(row, SidebarRow) and row.target == target:
                        match = row
                        break
                    row = row.get_next_sibling()
            if match is not None:
                lb.select_row(match)
                found = True
            else:
                lb.unselect_all()

    def _sync_recent(self):
        self.recent_row.set_visible(self._privacy.get_boolean("remember-recent-files"))

    def _on_row(self, _lb, row: SidebarRow, where: str = "here"):
        if row.target is None and row.volume is not None:
            self.mount_volume(row, where)
            return
        if row.target:
            self.emit("open", row.target, where)

    def _on_middle(self, gesture, n, x, y, lb):
        row = lb.get_row_at_y(int(y))
        if isinstance(row, SidebarRow):
            self._on_row(lb, row, "tab")

    def _on_menu(self, gesture, n, x, y, lb):
        row = lb.get_row_at_y(int(y))
        if not isinstance(row, SidebarRow):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        group = Gio.SimpleActionGroup()
        menu = Gio.Menu()
        s = Gio.Menu()

        def add(name, label, cb, section):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            group.add_action(a)
            section.append(label, f"side.{name}")
        add("open", tr("Ouvrir"), lambda: self._on_row(lb, row), s)
        add("tab", tr("Ouvrir dans un nouvel onglet"), lambda: self._on_row(lb, row, "tab"), s)
        add("window", tr("Ouvrir dans une nouvelle fenêtre"), lambda: self._on_row(lb, row, "window"), s)
        if features.enabled("dual_panel"):
            add("other", tr("Ouvrir dans l'autre panneau"), lambda: self._on_row(lb, row, "other"), s)
        menu.append_section(None, s)
        s2 = Gio.Menu()
        if row.kind == "bookmark":
            add("rename", tr("Renommer…"), lambda: self._rename_bookmark(row), s2)
            add("remove", tr("Retirer des signets"), lambda: bookmarks.toggle(row.target), s2)
        elif row.target and row.kind == "place" and row.target != TRASH:
            pass
        if row.kind == "device" and (row.mount is not None or row.volume is not None):
            if row.target is None:
                add("mount", tr("Monter"), lambda: self.mount_volume(row), s2)
            elif row.mount is not None and (row.mount.can_eject() or row.mount.can_unmount()):
                add("eject", tr("Éjecter") if row.mount.can_eject() else tr("Démonter"), lambda: self.eject(row), s2)
        if row.target == TRASH:
            s2.append(tr("Vider la corbeille…"), "win.empty-trash")
        if s2.get_n_items():
            menu.append_section(None, s2)
        if row.target and "://" not in row.target:
            s3 = Gio.Menu()
            s3.append(tr("Propriétés"), "win.properties-path::" + row.target)
            menu.append_section(None, s3)
        # sur la barre latérale elle-même : c'est à elle que le menu est attaché, et un menu ne cherche
        # ses actions que chez ses parents (posées sur la liste, toutes les entrées étaient grisées)
        self.insert_action_group("side", group)
        ok, pt = lb.compute_point(self, Graphene.Point().init(x, y))
        popup_menu(self, menu, pt.x if ok else x, pt.y if ok else y)

    def _rename_bookmark(self, row: SidebarRow):
        ask_text(self, tr("Renommer le signet"), tr("Nom"), row.label.get_text(),
                 lambda name: bookmarks.rename(row.target, name), tr("Renommer"))

    # -- déposer ----------------------------------------------------------------
    def _drop_motion(self, target, x, y, lb):
        self._drop_clear(lb)
        row = lb.get_row_at_y(int(y))
        if isinstance(row, SidebarRow) and row.target and (row.target == TRASH or "://" not in row.target):
            row.add_css_class("drop-row")
            return Gdk.DragAction.MOVE if row.target == TRASH else Gdk.DragAction.COPY
        if lb is self.bm_list or lb is self.places_list:
            lb.add_css_class("drop-bookmark")
            return Gdk.DragAction.LINK
        return 0

    def _drop_clear(self, lb):
        lb.remove_css_class("drop-bookmark")
        row = lb.get_first_child()
        while row is not None:
            row.remove_css_class("drop-row")
            row = row.get_next_sibling()

    def _on_drop(self, target, value, x, y, lb):
        self._drop_clear(lb)
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        row = lb.get_row_at_y(int(y))
        if isinstance(row, SidebarRow) and row.target and (row.target == TRASH or "://" not in row.target):
            self.emit("drop", paths, row.target)
            return True
        # déposer un dossier entre les lignes : nouveau signet
        added = 0
        for p in paths:
            if os.path.isdir(p) and not bookmarks.contains(p):
                bookmarks.toggle(p)
                added += 1
        return bool(added)
