"""Barre de chemin à la manière de Nautilus : fil d'Ariane cliquable, saisie avec Ctrl+L.

Clic sur un segment : y aller ; clic du milieu : nouvel onglet ; déposer des fichiers sur un
segment : les copier ou déplacer dans ce dossier. Tab complète le nom de dossier saisi.
"""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ..i18n import tr


class PathBar(Gtk.Box):
    __gtype_name__ = "OdysseusPathBar"
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "navigate-tab": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "drop": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
    }

    def __init__(self, compact: bool = False, menu_model: Optional[Gio.MenuModel] = None):
        super().__init__(css_classes=["path-bar"] + (["compact"] if compact else []), hexpand=True)
        self.menu_model = menu_model
        self.location = ""
        self.display = ""
        self.stack = Gtk.Stack(hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=100)
        # fil d'Ariane
        crumbs_frame = Gtk.Box(css_classes=["path-crumbs-frame", "linked"], hexpand=True)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                           vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True,
                                           propagate_natural_width=True)
        self.crumbs = Gtk.Box(css_classes=["path-crumbs"])
        self.scroller.set_child(self.crumbs)
        crumbs_frame.append(self.scroller)
        click = Gtk.GestureClick()
        click.connect("released", self._on_frame_click)
        self.scroller.add_controller(click)
        self.stack.add_named(crumbs_frame, "crumbs")
        # saisie
        self.entry = Gtk.Entry(hexpand=True, css_classes=["path-entry"],
                               placeholder_text=tr("Saisir un emplacement"))
        self.entry.connect("activate", self._on_activate)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.timeout_add(150, self._maybe_leave))
        self.entry.add_controller(focus)
        self.stack.add_named(self.entry, "entry")
        self.append(self.stack)

    # ------------------------------------------------------------------
    def set_location(self, location: str, display: str, archive_root: str = ""):
        """location : emplacement réel ; display : chemin affiché (archive : …/a.7z/dossier)."""
        self.location = location
        self.display = display
        if self.stack.get_visible_child_name() != "entry":
            self.entry.set_text(display)
        self._build(display, archive_root)

    def _segments(self, display: str, archive_root: str) -> list[tuple[str, str, Optional[str]]]:
        """(libellé, cible, icône)."""
        if "://" in display and not display.startswith("/"):
            f = Gio.File.new_for_uri(display)
            segs = []
            while f is not None:
                name = f.get_basename() or f.get_uri()
                if f.get_uri() in ("trash:///", "trash:"):
                    segs.insert(0, (tr("Corbeille"), "trash:///", "user-trash-symbolic"))
                    break
                segs.insert(0, (name if name != "/" else f.get_uri(), f.get_uri(), None))
                f = f.get_parent()
            return segs
        home = os.path.expanduser("~")
        segs = []
        if display == home or display.startswith(home + "/"):
            segs.append((tr("Dossier personnel"), home, "user-home-symbolic"))
            rest = display[len(home):].strip("/")
            base = home
        else:
            segs.append(("", "/", "drive-harddisk-symbolic"))
            rest = display.strip("/")
            base = ""
        for part in [p for p in rest.split("/") if p]:
            base = base + "/" + part
            icon = None
            if archive_root and base == archive_root:
                icon = "package-x-generic-symbolic"
            segs.append((part, base, icon))
        return segs

    def _build(self, display: str, archive_root: str):
        while (c := self.crumbs.get_first_child()) is not None:
            self.crumbs.remove(c)
        segs = self._segments(display, archive_root)
        for i, (label, target, icon) in enumerate(segs):
            last = i == len(segs) - 1
            if i > 0:
                self.crumbs.append(Gtk.Label(label="/", css_classes=["path-sep", "dim-label"]))
            btn = Gtk.MenuButton(css_classes=["flat", "path-button", "current"]) if (last and self.menu_model) \
                else Gtk.Button(css_classes=["flat", "path-button"] + (["current"] if last else []))
            box = Gtk.Box(spacing=6)
            if icon:
                box.append(Gtk.Image(icon_name=icon))
            if label:
                # Pas d'abréviation automatique : le fil d'Ariane défile plutôt que de rogner
                # tous les segments ; seuls les noms très longs sont raccourcis.
                limit = 40 if last else 28
                shown = label if len(label) <= limit else label[:limit // 2 - 1] + "…" + label[-(limit // 2 - 1):]
                box.append(Gtk.Label(label=shown))
            if last and self.menu_model:
                box.append(Gtk.Image(icon_name="pan-down-symbolic", css_classes=["dim-label"]))
                btn.set_menu_model(self.menu_model)
            btn.set_child(box)
            btn.set_tooltip_text(target if "://" not in target else GLib.uri_unescape_string(target, None))
            if not (last and self.menu_model):
                btn.connect("clicked", lambda _b, t=target: self.emit("navigate", t))
            mid = Gtk.GestureClick(button=2)
            mid.connect("pressed", lambda *_a, t=target: self.emit("navigate-tab", t))
            btn.add_controller(mid)
            if not target.startswith("trash"):
                self._add_drop(btn, target)
            self.crumbs.append(btn)
        GLib.idle_add(self._scroll_end)

    def _add_drop(self, widget, target):
        t = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        t.connect("enter", lambda *_: (widget.add_css_class("drop-row"), Gdk.DragAction.MOVE)[1])
        t.connect("leave", lambda *_: widget.remove_css_class("drop-row"))

        def drop(_t, value, x, y):
            widget.remove_css_class("drop-row")
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if paths:
                self.emit("drop", paths, target)
            return bool(paths)
        t.connect("drop", drop)
        widget.add_controller(t)

    def _scroll_end(self):
        adj = self.scroller.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    # -- saisie ------------------------------------------------------------
    def edit(self, text: Optional[str] = None):
        self.entry.set_text(text if text is not None else self.display)
        self.stack.set_visible_child_name("entry")
        self.entry.grab_focus()
        if text is None:
            self.entry.select_region(0, -1)
        else:
            self.entry.set_position(-1)

    def stop_edit(self):
        self.stack.set_visible_child_name("crumbs")

    def _maybe_leave(self):
        if not self.entry.has_focus() and not self.entry.is_focus():
            self.stop_edit()
        return False

    def _on_frame_click(self, gesture, n, x, y):
        # clic dans la zone vide du fil d'Ariane : saisir un chemin, comme Nautilus
        w = self.scroller.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not self.scroller:
            if isinstance(w, Gtk.Button):
                return
            w = w.get_parent()
        self.edit()

    def _on_activate(self, entry):
        text = entry.get_text().strip()
        if text:
            self.stop_edit()
            self.emit("navigate", text)

    def _on_entry_key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.entry.set_text(self.display)
            self.stop_edit()
            self.emit("navigate", "")       # rend le focus à la vue
            return True
        if keyval == Gdk.KEY_Tab:
            self._complete()
            return True
        return False

    def _complete(self):
        text = os.path.expanduser(self.entry.get_text())
        folder, prefix = os.path.split(text)
        if not os.path.isdir(folder or "/"):
            return
        try:
            names = sorted(e.name for e in os.scandir(folder or "/") if e.is_dir() and e.name.startswith(prefix)
                           and (prefix.startswith(".") or not e.name.startswith(".")))
        except OSError:
            return
        if not names:
            return
        common = os.path.commonprefix(names)
        new = os.path.join(folder, common)
        if len(names) == 1:
            new += "/"
        self.entry.set_text(new)
        self.entry.set_position(-1)


def make_folder_menu() -> Gio.Menu:
    """Menu du dossier courant (dernier segment), comme dans Nautilus."""
    m = Gio.Menu()
    s1 = Gio.Menu()
    s1.append(tr("Nouveau dossier…"), "win.new-folder")
    s1.append(tr("Nouveau document"), "win.new-file")
    m.append_section(None, s1)
    s2 = Gio.Menu()
    s2.append(tr("Ajouter aux signets"), "win.bookmark")
    s2.append(tr("Ouvrir dans un terminal"), "win.terminal")
    s2.append(tr("Copier l'emplacement"), "win.copy-location")
    m.append_section(None, s2)
    s3 = Gio.Menu()
    s3.append(tr("Propriétés"), "win.properties-folder")
    m.append_section(None, s3)
    return m

