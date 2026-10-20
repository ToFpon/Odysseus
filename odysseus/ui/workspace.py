"""Contenu d'un onglet : un panneau, ou deux côte à côte (double panneau, touche F3)."""

from __future__ import annotations

from typing import Optional

from gi.repository import GLib, GObject, Gtk

from .pane import Pane
from .pathbar import PathBar


class Workspace(Gtk.Box):
    __gtype_name__ = "OdysseusWorkspace"
    __gsignals__ = {
        "active-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, window, location: str, dual: bool = False, view_mode: Optional[str] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.window = window
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True, vexpand=True,
                               shrink_start_child=False, shrink_end_child=False)
        self.append(self.paned)
        self._ratio = 0.5
        self._placing = False
        self.paned.connect("notify::max-position", lambda *_: self._apply_ratio())
        self.paned.connect("notify::position", self._on_position)
        # Seul un déplacement de la poignée par l'utilisateur change la proportion des panneaux.
        self._dragging = False
        g = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        g.connect("pressed", lambda *_: setattr(self, "_dragging", True))
        g.connect("released", lambda *_: setattr(self, "_dragging", False))
        g.connect("stopped", lambda *_: setattr(self, "_dragging", False))
        self.paned.add_controller(g)
        self.panes: list[Pane] = []
        first = self._make_pane(location, view_mode)
        self.paned.set_start_child(first)
        self.active: Pane = first
        self.dual = False
        if dual:
            self.set_dual(True)

    def _make_pane(self, location: str, view_mode: Optional[str] = None) -> Pane:
        pane = Pane(location, view_mode)
        self.window.connect_pane(pane, self)
        pane.connect("focused", lambda p: self.set_active(p))
        self.panes.append(pane)
        return pane

    def other(self, pane: Optional[Pane] = None) -> Optional[Pane]:
        pane = pane or self.active
        if not self.dual:
            return None
        for p in self.panes:
            if p is not pane:
                return p
        return None

    def set_active(self, pane: Pane):
        if pane is self.active:
            return
        self.active = pane
        for p in self.panes:
            if p is pane:
                p.add_css_class("active-pane")
            else:
                p.remove_css_class("active-pane")
        self.emit("active-changed")

    def set_dual(self, dual: bool):
        if dual == self.dual:
            return
        self.dual = dual
        if dual:
            src = self.active
            second = self._make_pane(src.location if src.mode in ("fs", "trash", "remote") else src.current_dir()
                                     or src.location, src.view_mode)
            self.paned.set_end_child(second)
            for p in self.panes:
                self._add_pathbar(p)
            self.active.add_css_class("active-pane")
            GLib.idle_add(self._center)
        else:
            keep = self.active
            # Défaire le séparateur fait chercher à GtkPaned le widget qui a le focus ; s'il est
            # ailleurs (champ de recherche…) alors que le séparateur garde la trace d'un ancien focus
            # dans un panneau : « Error finding last focus widget ». Focus vidé le temps de la
            # reconstruction, puis rendu là où il était (au panneau gardé s'il était dans un panneau).
            root = self.get_root()
            focus = root.get_focus() if root is not None else None
            if focus is not None:
                root.set_focus(None)
            for p in list(self.panes):
                if p is not keep:
                    p.retire()
                    self.panes.remove(p)
            self.paned.set_start_child(None)
            self.paned.set_end_child(None)
            self.paned.set_start_child(keep)
            if focus is not None:
                if focus.is_ancestor(self.paned) or focus.get_root() is not root:
                    GLib.idle_add(keep.focus_view)
                elif isinstance(focus, Gtk.Text):
                    focus.grab_focus_without_selecting()      # le texte tapé reste tel quel
                else:
                    focus.grab_focus()
            for p in self.panes:
                p.remove_css_class("active-pane")
                if getattr(p, "pathbar", None) is not None:
                    p.top.remove(p.pathbar)
                    p.pathbar = None
            self.active = keep
        self.emit("active-changed")

    def _center(self):
        self._ratio = 0.5
        self._apply_ratio()
        return False

    def _apply_ratio(self):
        if not self.dual:
            return
        w = self.paned.get_width()
        if w > 0:
            self._placing = True
            self.paned.set_position(int(w * self._ratio))
            self._placing = False

    def _on_position(self, *_):
        if self._placing or not self.dual or not self._dragging:
            return
        w = self.paned.get_width()
        if w > 100:
            self._ratio = max(0.1, min(0.9, self.paned.get_position() / w))

    def _add_pathbar(self, pane: Pane):
        if getattr(pane, "pathbar", None) is not None:
            return
        bar = PathBar(compact=True)
        bar.connect("navigate", lambda _b, loc: (pane.open(loc) if loc else pane.focus_view(), self.set_active(pane)))
        bar.connect("navigate-tab", lambda _b, loc: self.window.new_tab(loc))
        bar.connect("drop", lambda _b, paths, target: self.window.drop_files(paths, target, "auto"))
        pane.pathbar = bar
        pane.top.append(bar)
        self.window.update_pane_pathbar(pane)
