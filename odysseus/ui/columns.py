"""Vue en colonnes (colonnes de Miller, à la manière du Finder), reprise de Column Browser.

Chaque colonne a son propre modèle virtuel chargé en arrière-plan. Sélectionner un dossier
ajoute une colonne à droite ; ← et → passent d'une colonne à l'autre. La fenêtre ne change plus
de taille : la chaîne défile horizontalement et la dernière colonne reste visible.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from gi.repository import Gdk, GLib, Graphene, Gtk, Pango

from ..backend.scanner import Rec
from . import clipboard, icons
from .model import DirModel

if TYPE_CHECKING:
    from .pane import Pane

COLUMN_WIDTH = 230
COLUMN_MIN = 120
COLUMN_MAX = 640
ANCESTORS = 3      # colonnes d'ancêtres affichées en ouvrant un emplacement


class MillerColumn(Gtk.Box):
    __gtype_name__ = "OdysseusMillerColumn"

    def __init__(self, view: "MillerView", path: str, width: int = COLUMN_WIDTH):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["miller-column"])
        self.view = view
        self.path = path
        self.width = width
        self.set_size_request(width, -1)
        self.model = DirModel()
        self.model.sort_key = "name"
        self.model.sort_reverse = False
        self.selection = Gtk.SingleSelection(model=self.model, autoselect=False, can_unselect=True)
        self.selection.connect("selection-changed", self._on_selected)
        self._pending_select: Optional[str] = None
        self._quiet = False
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._setup)
        f.connect("bind", self._bind)
        f.connect("unbind", lambda _f, li: self._untrack(li.get_child()))
        self.list = Gtk.ListView(model=self.selection, factory=f, css_classes=["navigation-sidebar", "miller-list"])
        self.list.connect("activate", self._on_activate)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.list.add_controller(keys)
        bg = Gtk.GestureClick(button=3)
        bg.connect("pressed", self._on_bg_menu)
        self.list.add_controller(bg)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: view.set_focused(self))
        self.list.add_controller(focus)
        sw = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(sw)
        self.model.connect("state", self._on_state)
        self.model.load(path)

    def _on_state(self, _m, state):
        if state == "ready" and self._pending_select:
            name, self._pending_select = self._pending_select, None
            self.select_name(name, quiet=True)

    def select_name(self, name: str, quiet: bool = False):
        i = self.model.find_name(name)
        if i < 0:
            self._pending_select = name
            return
        self._quiet = quiet
        self.selection.set_selected(i)
        self._quiet = False
        self.list.scroll_to(i, Gtk.ListScrollFlags.NONE, None)

    def selected_rec(self) -> Optional[Rec]:
        return self.model.rec_at(self.selection.get_selected())

    # -- cellules ----------------------------------------------------------
    def _setup(self, _f, li):
        box = Gtk.Box(spacing=8, margin_start=4, margin_end=2)
        box.append(Gtk.Image(pixel_size=20))
        box.append(Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        chev = Gtk.Image(icon_name="go-next-symbolic", pixel_size=12, css_classes=["miller-chevron"])
        box.append(chev)
        li.set_child(box)
        g = Gtk.GestureClick(button=3)

        def pressed(gesture, n, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            pos = li.get_position()
            if pos != Gtk.INVALID_LIST_POSITION:
                self.selection.set_selected(pos)
            self.view.set_focused(self)
            ok, pt = box.compute_point(self.view.pane, Graphene.Point().init(x, y))
            self.view.pane.emit("context-menu", pt.x if ok else x, pt.y if ok else y, True)
        g.connect("pressed", pressed)
        box.add_controller(g)

    def _bind(self, _f, li):
        entry = li.get_item()
        box = li.get_child()
        box._rec = entry.rec
        old = getattr(box, "_entry", None)
        if old is not None:
            old.disconnect(box._hid)
        box._entry = entry
        box._hid = entry.connect("changed", lambda *_: self._fill(box, entry.rec))
        self._fill(box, entry.rec)

    def _untrack(self, box):
        old = getattr(box, "_entry", None)
        if old is not None:
            old.disconnect(box._hid)
        box._entry = None

    def _fill(self, box, rec: Rec):
        img = box.get_first_child()
        lbl = img.get_next_sibling()
        chev = box.get_last_child()
        img.set_from_gicon(icons.gicon_for(rec))
        icons.request_meta(rec, self.model)
        lbl.set_text(rec.name)
        chev.set_visible(rec.is_dir)
        from .. import features
        from ..settings import settings
        dim_icon = dim_all = 1.0
        if rec.hidden and features.enabled("hidden_dim"):
            if settings["hidden_dim_mode"] == "all":
                dim_all = 0.55
            else:
                dim_icon = 0.35
        if features.enabled("cut_dim") and clipboard.state.is_cut(rec.path):
            dim_all = 0.5
        img.set_opacity(dim_icon)
        box.set_opacity(dim_all)

    # -- événements --------------------------------------------------------
    def _on_selected(self, *_):
        if not self._quiet:
            self.view.column_selected(self, self.selected_rec())

    def _on_activate(self, _lv, pos):
        rec = self.model.rec_at(pos)
        if rec is not None and not rec.is_dir:
            self.view.pane.emit("activated", rec)

    def _on_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            return self.view.focus_next(self)
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            return self.view.focus_previous(self)
        return False

    def _on_bg_menu(self, gesture, n, x, y):
        self.selection.unselect_all()
        self.view.set_focused(self)
        ok, pt = self.list.compute_point(self.view.pane, Graphene.Point().init(x, y))
        self.view.pane.emit("context-menu", pt.x if ok else x, pt.y if ok else y, False)


def _resize_handle(column: MillerColumn) -> Gtk.Widget:
    handle = Gtk.Box(css_classes=["miller-resize-handle"], width_request=5)
    handle.set_cursor(Gdk.Cursor.new_from_name("col-resize", None))
    state = {"w": 0}
    drag = Gtk.GestureDrag()
    drag.connect("drag-begin", lambda *_: state.__setitem__("w", column.width))

    def update(_g, dx, _dy):
        w = max(COLUMN_MIN, min(COLUMN_MAX, int(state["w"] + dx)))
        column.width = w
        column.set_size_request(w, -1)
    drag.connect("drag-update", update)
    handle.add_controller(drag)
    return handle


class MillerView(Gtk.ScrolledWindow):
    __gtype_name__ = "OdysseusMillerView"

    def __init__(self, pane: "Pane"):
        super().__init__(vexpand=True, hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.pane = pane
        self.add_css_class("miller-view")
        self.chain = Gtk.Box()
        self.set_child(self.chain)
        self.columns: list[MillerColumn] = []
        self.focused: Optional[MillerColumn] = None
        self.root = ""

    # -- chaîne --------------------------------------------------------------
    def open(self, path: str):
        """Affiche path et quelques ancêtres pour garder le contexte."""
        if self.columns and self.columns[-1].path == path:
            return
        for i, c in enumerate(self.columns):
            if c.path == path:
                self._truncate_after(c)
                c.selection.unselect_all()
                self.set_focused(c)
                return
        chain = [path]
        p = path
        for _ in range(ANCESTORS):
            parent = os.path.dirname(p.rstrip("/")) or "/"
            if parent == p:
                break
            chain.insert(0, parent)
            p = parent
        while (c := self.chain.get_first_child()) is not None:
            self.chain.remove(c)
        self.columns.clear()
        for i, d in enumerate(chain):
            col = self._append(d)
            if i + 1 < len(chain):
                col.select_name(os.path.basename(chain[i + 1].rstrip("/")), quiet=True)
        self.set_focused(self.columns[-1])
        GLib.idle_add(self._scroll_end)

    def _append(self, path: str) -> MillerColumn:
        col = MillerColumn(self, path)
        if self.columns:
            self.chain.append(_resize_handle(self.columns[-1]))
        self.chain.append(col)
        self.columns.append(col)
        return col

    def _truncate_after(self, column: MillerColumn):
        idx = self.columns.index(column)
        for c in self.columns[idx + 1:]:
            c.model.cancel()
        del self.columns[idx + 1:]
        children = []
        w = self.chain.get_first_child()
        while w is not None:
            children.append(w)
            w = w.get_next_sibling()
        pos = children.index(column)
        for w in children[pos + 1:]:
            self.chain.remove(w)

    def _scroll_end(self):
        adj = self.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    def refresh(self):
        for c in self.columns:
            c.model.load(c.path)

    def set_show_hidden(self, value: bool):
        for c in self.columns:
            c.model.set_show_hidden(value)

    def refresh_rows(self):
        for c in self.columns:
            c.model.refresh_rows()

    # -- sélection ------------------------------------------------------------
    def column_selected(self, column: MillerColumn, rec: Optional[Rec]):
        self._truncate_after(column)
        self.set_focused(column)
        if rec is not None and rec.is_dir and rec.path:
            self._append(rec.path)
            GLib.idle_add(self._scroll_end)
            self.pane.columns_location(rec.path)
        else:
            self.pane.columns_location(column.path)
        self.pane.emit("selection-changed")

    def set_focused(self, column: MillerColumn):
        if self.focused is column:
            return
        if self.focused is not None:
            self.focused.remove_css_class("focused")
        self.focused = column
        column.add_css_class("focused")

    def focus_next(self, column: MillerColumn) -> bool:
        idx = self.columns.index(column)
        if idx + 1 < len(self.columns):
            nxt = self.columns[idx + 1]
            nxt.list.grab_focus()
            if nxt.selection.get_selected() == Gtk.INVALID_LIST_POSITION and nxt.model.get_n_items():
                nxt.selection.set_selected(0)
            return True
        return False

    def focus_previous(self, column: MillerColumn) -> bool:
        idx = self.columns.index(column)
        if idx > 0:
            self.columns[idx - 1].list.grab_focus()
            return True
        return False

    def selected_recs(self) -> list[Rec]:
        """La sélection est celle de la colonne active (celle qui a le focus)."""
        col = self.focused or (self.columns[-1] if self.columns else None)
        if col is None:
            return []
        r = col.selected_rec()
        if r is None and col is not self.columns[0]:
            return []
        return [r] if r is not None else []

    def current_dir(self) -> str:
        col = self.focused or (self.columns[-1] if self.columns else None)
        return col.path if col else ""

    def focus(self):
        col = self.focused or (self.columns[-1] if self.columns else None)
        if col:
            col.list.grab_focus()
