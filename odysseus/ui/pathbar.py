"""Barre de chemin à la manière de Nautilus : fil d'Ariane cliquable, saisie avec Ctrl+L.

Clic sur un segment : y aller ; clic du milieu : nouvel onglet ; déposer des fichiers sur un
segment : les copier ou déplacer dans ce dossier. Tab complète le nom de dossier saisi.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ..i18n import tr


class PathBar(Gtk.Box):
    __gtype_name__ = "OdysseusPathBar"
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "navigate-tab": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "drop": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
    }

    def __init__(self, compact: bool = False, menu_factory: Optional[Callable[[], Gio.MenuModel]] = None):
        super().__init__(css_classes=["path-bar"] + (["compact"] if compact else []), hexpand=True)
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
        # menu du dossier : bouton « ⋮ » au bout de la barre, comme Nautilus (menu reconstruit à
        # chaque ouverture : il dépend des fonctionnalités activées)
        if menu_factory is not None:
            self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat", "path-menu"],
                                           tooltip_text=tr("Menu du dossier"), valign=Gtk.Align.CENTER)
            self.menu_btn.set_create_popup_func(lambda b: b.set_menu_model(menu_factory()))
            crumbs_frame.append(self.menu_btn)
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
            special = {"trash:///": (tr("Corbeille"), "user-trash-symbolic"),
                       "recent:///": (tr("Récents"), "document-open-recent-symbolic"),
                       "network:///": (tr("Réseau"), "network-workgroup-symbolic"),
                       "other-locations:///": (tr("Autres emplacements"), "list-add-symbolic"),
                       "admin:///": (tr("Administrateur"), "dialog-password-symbolic")}
            while f is not None:
                name = f.get_basename() or f.get_uri()
                if f.get_uri() in special:
                    label, icon = special[f.get_uri()]
                    segs.insert(0, (label, f.get_uri(), icon))
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
            # comme Nautilus : la racine porte le nom du système (« Zorin OS »)
            segs.append((GLib.get_os_info("NAME") or "", "/", "drive-harddisk-symbolic"))
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
            btn = Gtk.Button(css_classes=["flat", "path-button"] + (["current"] if last else []))
            box = Gtk.Box(spacing=6)
            if icon:
                box.append(Gtk.Image(icon_name=icon))
            if label:
                # Pas d'abréviation automatique : le fil d'Ariane défile plutôt que de rogner
                # tous les segments ; seuls les noms très longs sont raccourcis.
                limit = 40 if last else 28
                shown = label if len(label) <= limit else label[:limit // 2 - 1] + "…" + label[-(limit // 2 - 1):]
                box.append(Gtk.Label(label=shown))
            btn.set_child(box)
            btn.set_tooltip_text(target if "://" not in target else GLib.uri_unescape_string(target, None))
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
