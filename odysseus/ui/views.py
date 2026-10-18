"""Vues de fichiers : liste (comme la vue liste de Nautilus) et grille.

Les deux vues partagent le modèle virtuel et la sélection du panneau. Les cellules affichent
d'abord ce qui est connu tout de suite (nom, icône du type), puis se complètent quand les
données paresseuses arrivent (miniature, couleur de dossier, nombre d'éléments, durée).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Gdk, Gio, GLib, Graphene, Gtk, Pango

from .. import features
from ..backend import media, scanner
from ..backend.engine import human_size
from ..backend.scanner import Rec
from ..backend.workers import pool
from ..i18n import ntr, tr
from ..settings import settings
from . import clipboard, icons
from .common import date_label, perms_label

if TYPE_CHECKING:
    from .pane import Pane

# Niveaux de zoom repris de Nautilus.
ZOOM_LEVELS = ["small", "medium", "large"]
ZOOM = {"small": (16, True), "medium": (32, False), "large": (48, False)}
GRID_ZOOM_LEVELS = ["small", "medium", "large", "larger"]
GRID_ZOOM = {"small": 64, "medium": 96, "large": 128, "larger": 256}

# clé, titre, largeur, alignement, clé de tri
COLUMNS = [
    ("name", tr("Nom"), 0, 0.0, "name"),
    ("size", tr("Taille"), 125, 0.0, "size"),
    ("packed", tr("Compressé"), 100, 1.0, "packed"),
    ("ratio", tr("Ratio"), 70, 1.0, None),
    ("type", tr("Type"), 170, 0.0, "type"),
    ("date", tr("Dernière modification"), 175, 1.0, "date"),
    ("perms", tr("Droits"), 110, 0.0, "perms"),
    ("owner", tr("Propriétaire"), 110, 0.0, "owner"),
    ("duration", tr("Durée"), 90, 1.0, "duration"),
    ("location", tr("Emplacement"), 240, 0.0, "location"),
]
OPTIONAL_COLUMNS = [("size", tr("Taille")), ("type", tr("Type")), ("date", tr("Modifié")),
                    ("perms", tr("Droits")), ("owner", tr("Propriétaire")), ("duration", tr("Durée"))]
SORT_TO_COLUMN = {c[4]: c[0] for c in COLUMNS if c[4]}

# Chemins en cours de glisser depuis Odysseus (pour choisir copier ou déplacer à l'arrivée)
DRAG_PATHS: list[str] = []


def cell_text(key: str, rec: Rec, pane: "Pane") -> str:
    if key == "size":
        if rec.is_dir:
            n = rec.get("count")
            if n is None:
                if rec.path and pane.mode != "archive":
                    request_count(rec, pane)
                n = rec.get("children")
            return "" if n is None or n < 0 else ntr("{0} élément", "{0} éléments", n).format(n)
        return human_size(rec.size) if rec.stat_ok else ""
    if key == "packed":
        p = rec.get("packed")
        return human_size(p) if p is not None and not rec.is_dir else ""
    if key == "ratio":
        p = rec.get("packed")
        if rec.is_dir or p is None or not rec.size:
            return ""
        return f"{100 * p / rec.size:.0f} %"
    if key == "type":
        return tr("Dossier") if rec.is_dir else scanner.type_description(rec.content_type)
    if key == "date":
        return date_label(rec.mtime) if rec.stat_ok else ""
    if key == "perms":
        return perms_label(rec.mode)
    if key == "owner":
        return scanner.owner_name(rec.uid)
    if key == "duration":
        if rec.is_dir or not media.is_media(rec.name):
            return ""
        d = rec.get("duration", None)
        if d is None and rec.path:
            request_duration(rec, pane)
            return ""
        return media.fmt_duration(d)
    if key == "location":
        return rec.get("location", "")
    return ""


def request_count(rec: Rec, pane: "Pane"):
    if rec.get("count_pending"):
        return
    rec.set("count_pending", True)
    path, hidden = rec.path, settings["show_hidden"]
    model = pane.model

    def done(n):
        rec.set("count", n if n is not None else -1)
        model.notify_rec(rec)
    def work():
        with scanner.SCAN_LOCK:     # pas de lecture disque concurrente (voir scanner.SCAN_LOCK)
            return scanner.count_children(path, hidden)
    pool.submit(("count", path, hidden), work, done)


def request_duration(rec: Rec, pane: "Pane"):
    if not features.available("duration_column") or rec.get("duration_pending"):
        return
    rec.set("duration_pending", True)
    path, mtime, model = rec.path, rec.mtime, pane.model

    def done(d):
        rec.set("duration", d if d is not None else -1.0)
        model.notify_rec(rec)
    pool.submit(("dur", path), lambda: media.durations.duration(path, mtime), done)


class _CellMixin:
    """Liaison commune des cellules nom (liste) et des cases de la grille."""
    pane: "Pane"

    def _track(self, widget, entry, refill):
        old = getattr(widget, "_entry", None)
        if old is not None and getattr(widget, "_hid", 0):
            try:
                old.disconnect(widget._hid)
            except TypeError:
                pass
        widget._entry = entry
        widget._hid = entry.connect("changed", lambda *_: refill()) if entry is not None else 0

    def _untrack(self, widget):
        self._track(widget, None, None)
        widget._rec = None

    def _fill_icon(self, image: Gtk.Image, emblem: Gtk.Image, rec: Rec, size: int, allow_thumb: bool):
        image.set_pixel_size(size)
        tex = icons.request_thumbnail(rec, size, self.pane.model) if (allow_thumb and icons.thumbnails_wanted(rec)) else None
        if tex is not None:
            image.set_from_paintable(tex)
            image.add_css_class("thumbnail")
        else:
            image.set_from_gicon(icons.gicon_for(rec))
            image.remove_css_class("thumbnail")
        icons.request_meta(rec, self.pane.model)
        ems = icons.emblems_for(rec)
        if ems:
            emblem.set_from_icon_name(ems[-1])
            emblem.set_pixel_size(max(10, min(24, size // 3)))
            emblem.set_visible(True)
        else:
            emblem.set_visible(False)

    @staticmethod
    def _apply_copy_mark(mark: Gtk.Widget, rec: Rec):
        """Barre colorée devant un élément copié ; l'emplacement n'existe que si la fonctionnalité
        est activée, pour ne pas décaler les noms sinon."""
        on = features.enabled("copy_mark")
        mark.set_visible(on)
        if on and clipboard.state.is_copied(rec.path):
            mark.add_css_class("copied")
        else:
            mark.remove_css_class("copied")

    def _apply_dim(self, box: Gtk.Widget, image: Gtk.Image, rec: Rec):
        dim_icon = dim_all = 1.0
        if rec.hidden and features.enabled("hidden_dim"):
            if settings["hidden_dim_mode"] == "all":
                dim_all = 0.55
            else:
                dim_icon = 0.35
        if features.enabled("cut_dim") and clipboard.state.is_cut(rec.path):
            dim_all = min(dim_all, 0.5)
        image.set_opacity(dim_icon)
        box.set_opacity(dim_all)

    def _attach_menu_gesture(self, widget, list_item):
        g = Gtk.GestureClick(button=3)

        def pressed(gesture, n, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            pos = list_item.get_position()
            sel = self.pane.selection
            if pos != Gtk.INVALID_LIST_POSITION and not sel.is_selected(pos):
                sel.select_item(pos, True)
            ok, pt = widget.compute_point(self.pane, Graphene.Point().init(x, y))
            self.pane.emit("context-menu", pt.x if ok else x, pt.y if ok else y, True)
        g.connect("pressed", pressed)
        widget.add_controller(g)

        # clic du milieu : ouvrir un dossier dans un nouvel onglet, comme Nautilus
        m = Gtk.GestureClick(button=2)

        def middle(gesture, n, x, y):
            rec = getattr(widget, "_rec", None)
            if rec is not None and rec.is_dir:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.pane.emit("open-in-tab", rec)
        m.connect("pressed", middle)
        widget.add_controller(m)

    def _attach_drag(self, widget, list_item):
        """Glisser des fichiers. La source est posée sur chaque cellule, comme dans Nautilus : sur la
        vue entière, la sélection par rectangle (rubberband) prend le geste et rien ne se glisse."""
        pane = self.pane
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK,
                              propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def prepare(source, x, y):
            pos = list_item.get_position()
            if pos != Gtk.INVALID_LIST_POSITION and not pane.selection.is_selected(pos):
                pane.selection.select_item(pos, True)     # glisser un élément non sélectionné : lui seul
            paths = pane.drag_paths()
            if not paths:
                return None
            source._paths = paths
            DRAG_PATHS[:] = paths
            return Gdk.ContentProvider.new_for_value(
                Gdk.FileList.new_from_list([Gio.File.new_for_path(p) for p in paths]))

        def begin(source, drag_obj):
            paths = getattr(source, "_paths", [])
            icon = Gtk.DragIcon.get_for_drag(drag_obj)
            box = Gtk.Box(spacing=6, css_classes=["drag-icon"])
            img = Gtk.Image(pixel_size=32)
            recs = pane.selected_recs()
            img.set_from_gicon(icons.gicon_for(recs[0]) if recs else Gio.ThemedIcon.new("text-x-generic"))
            box.append(img)
            if len(paths) > 1:
                box.append(Gtk.Label(label=str(len(paths)), css_classes=["drag-count"]))
            icon.set_child(box)
        drag.connect("prepare", prepare)
        drag.connect("drag-begin", begin)
        drag.connect("drag-end", lambda *_: (DRAG_PATHS.clear(), pane.clear_drop_highlight()))
        widget.add_controller(drag)

    def _attach_drop(self, widget):
        """Déposer sur un dossier : copie ou déplacement dans ce dossier."""
        t = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)

        def accept(_t, drop):
            rec = getattr(widget, "_rec", None)
            return bool(rec is not None and rec.is_dir and rec.path and self.pane.mode == "fs")

        def enter(_t, x, y):
            widget.add_css_class("drop-row")
            return self.pane.preferred_action(_t.get_current_drop(), getattr(widget, "_rec", None))

        def drop(_t, value, x, y):
            widget.remove_css_class("drop-row")
            rec = getattr(widget, "_rec", None)
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if rec is None or not paths or rec.path in paths:
                return False
            self.pane.handle_drop(paths, rec.path, _t.get_current_drop())
            return True
        t.connect("accept", accept)
        t.connect("enter", enter)
        t.connect("motion", enter)
        t.connect("leave", lambda *_: widget.remove_css_class("drop-row"))
        t.connect("drop", drop)
        widget.add_controller(t)


class ListView(Gtk.ScrolledWindow, _CellMixin):
    """Vue liste (Gtk.ColumnView) ; même classe CSS que Nautilus pour hériter des règles des thèmes."""
    __gtype_name__ = "OdysseusListView"

    def __init__(self, pane: "Pane"):
        super().__init__(vexpand=True, hexpand=True)
        self.pane = pane
        self.add_css_class("nautilus-list-view")
        self.view = Gtk.ColumnView(model=None, show_row_separators=False, show_column_separators=False,
                                   reorderable=True, enable_rubberband=True, css_classes=["file-view"])
        if hasattr(self.view, "set_tab_behavior"):
            self.view.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
        self.view.connect("activate", lambda _v, pos: pane.activate_position(pos))
        self.view.set_single_click_activate(settings["single_click"])
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}
        self._syncing = False
        self._narrow = 0
        for key, title, width, xalign, sort_key in COLUMNS:
            f = Gtk.SignalListItemFactory()
            if key == "name":
                f.connect("setup", self._setup_name)
                f.connect("bind", self._bind_name)
                f.connect("unbind", lambda _f, li: self._untrack(li.get_child()))
            else:
                f.connect("setup", self._setup_text, xalign)
                f.connect("bind", self._bind_text, key)
                f.connect("unbind", lambda _f, li: self._untrack(li.get_child()))
            col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, expand=(key == "name"))
            if width:
                col.set_fixed_width(width)
            if sort_key:
                col.set_sorter(Gtk.CustomSorter.new(lambda *_: 0))
                col._sort_key = sort_key
            self.view.append_column(col)
            self.columns[key] = col
        self.view.get_sorter().connect("changed", self._on_header_sort)
        self.set_child(self.view)
        bg = Gtk.GestureClick(button=3)
        bg.connect("pressed", self._on_bg_click)
        self.view.add_controller(bg)
        lp = Gtk.GestureLongPress(touch_only=True)
        lp.connect("pressed", lambda g, x, y: pane.emit("context-menu", x, y, True))
        self.view.add_controller(lp)
        self.apply_zoom()
        self.update_columns()
        self.sync_sort()

    # -- colonnes ----------------------------------------------------------
    def do_size_allocate(self, width, height, baseline):
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)
        # Panneau étroit (double panneau) : les colonnes facultatives s'effacent pour laisser
        # la place au nom, comme Nautilus.
        level = 0 if width >= 760 else (1 if width >= 560 else (2 if width >= 420 else 3))
        if level != self._narrow:
            self._narrow = level
            GLib.idle_add(self._idle_columns)

    def _idle_columns(self):
        self.update_columns()
        return False

    def update_columns(self):
        mode = self.pane.mode
        wanted = set(settings["list_columns"])
        drop = {0: set(), 1: {"perms", "owner"}, 2: {"perms", "owner", "type", "packed", "ratio"},
                3: {"perms", "owner", "type", "date", "duration", "packed", "ratio", "location"}}[self._narrow]
        wanted -= drop
        for key, col in self.columns.items():
            if key == "name":
                vis = True
            elif key in ("packed", "ratio"):
                vis = mode == "archive"
            elif key == "location":
                vis = mode in ("search", "trash")
            elif key == "duration":
                vis = key in wanted and features.available("duration_column") and mode != "archive"
            elif key in ("perms", "owner"):
                vis = key in wanted and mode != "archive"
            else:
                vis = key in wanted
            col.set_visible(vis and key not in drop)
        loc = self.columns["location"]
        loc.set_title(tr("Emplacement d'origine") if mode == "trash" else tr("Emplacement"))

    def sync_sort(self):
        key = SORT_TO_COLUMN.get(self.pane.model.sort_key, "name")
        col = self.columns.get(key)
        self._syncing = True
        self.view.sort_by_column(col, Gtk.SortType.DESCENDING if self.pane.model.sort_reverse
                                 else Gtk.SortType.ASCENDING)
        self._syncing = False

    def _on_header_sort(self, sorter, *_):
        if self._syncing:
            return
        col = sorter.get_primary_sort_column()
        if col is None:
            return
        key = getattr(col, "_sort_key", "name")
        reverse = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        self.pane.set_sort(key, reverse)

    def apply_zoom(self):
        _size, compact = self.pane.list_zoom
        if compact:
            self.view.add_css_class("compact")
        else:
            self.view.remove_css_class("compact")

    # -- cellules ----------------------------------------------------------
    def _setup_name(self, _f, li):
        box = Gtk.Box(spacing=10, css_classes=["name-cell"])
        box.append(Gtk.Box(css_classes=["copy-mark"], visible=False))
        ov = Gtk.Overlay()
        img = Gtk.Image()
        ov.set_child(img)
        emb = Gtk.Image(halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False, css_classes=["emblem"])
        ov.add_overlay(emb)
        box.append(ov)
        box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True))
        li.set_child(box)
        self._attach_menu_gesture(box, li)
        self._attach_drag(box, li)
        self._attach_drop(box)

    def _bind_name(self, _f, li):
        entry = li.get_item()
        box = li.get_child()
        box._rec = entry.rec
        self._track(box, entry, lambda: self._fill_name(box, entry.rec))
        self._fill_name(box, entry.rec)

    def _fill_name(self, box, rec: Rec):
        mark = box.get_first_child()
        ov = mark.get_next_sibling()
        img, emb = ov.get_child(), ov.get_last_child()
        label = box.get_last_child()
        size, _compact = self.pane.list_zoom
        self._fill_icon(img, emb, rec, size, size >= 32)
        label.set_text(rec.name)
        label.set_tooltip_text(rec.name if len(rec.name) > 50 else None)
        self._apply_copy_mark(mark, rec)
        self._apply_dim(box, img, rec)

    def _setup_text(self, _f, li, xalign):
        lbl = Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "numeric"])
        li.set_child(lbl)
        self._attach_menu_gesture(lbl, li)
        self._attach_drag(lbl, li)

    def _bind_text(self, _f, li, key):
        entry = li.get_item()
        lbl = li.get_child()
        lbl._rec = entry.rec

        def fill():
            lbl.set_text(cell_text(key, entry.rec, self.pane))
            lbl.set_opacity(0.5 if (features.enabled("cut_dim") and clipboard.state.is_cut(entry.rec.path)) else 1.0)
        self._track(lbl, entry, fill)
        fill()

    def _on_bg_click(self, gesture, n, x, y):
        self.pane.selection.unselect_all()
        ok, pt = self.view.compute_point(self.pane, Graphene.Point().init(x, y))
        self.pane.emit("context-menu", pt.x if ok else x, pt.y if ok else y, False)

    def scroll_to(self, pos: int):
        self.view.scroll_to(pos, None, Gtk.ListScrollFlags.FOCUS, None)

    def focus(self):
        self.view.grab_focus()


class GridView(Gtk.ScrolledWindow, _CellMixin):
    __gtype_name__ = "OdysseusGridView"

    def __init__(self, pane: "Pane"):
        super().__init__(vexpand=True, hexpand=True)
        self.pane = pane
        self.add_css_class("nautilus-grid-view")
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._setup)
        f.connect("bind", self._bind)
        f.connect("unbind", lambda _f, li: self._untrack(li.get_child()))
        self.view = Gtk.GridView(model=None, factory=f, max_columns=64, min_columns=1,
                                 enable_rubberband=True, css_classes=["file-grid"])
        self.view.connect("activate", lambda _v, pos: pane.activate_position(pos))
        self.view.set_single_click_activate(settings["single_click"])
        self.set_child(self.view)
        bg = Gtk.GestureClick(button=3)
        bg.connect("pressed", self._on_bg_click)
        self.view.add_controller(bg)

    def _setup(self, _f, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["grid-cell"],
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        ov = Gtk.Overlay(halign=Gtk.Align.CENTER)
        img = Gtk.Image()
        ov.set_child(img)
        ov.add_overlay(Gtk.Image(halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False,
                                 css_classes=["emblem"]))
        ov.add_overlay(Gtk.Box(halign=Gtk.Align.START, css_classes=["copy-mark"], visible=False))
        box.append(ov)
        box.append(Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=3,
                             ellipsize=Pango.EllipsizeMode.END, justify=Gtk.Justification.CENTER))
        li.set_child(box)
        self._attach_menu_gesture(box, li)
        self._attach_drag(box, li)
        self._attach_drop(box)

    def _bind(self, _f, li):
        entry = li.get_item()
        box = li.get_child()
        box._rec = entry.rec
        self._track(box, entry, lambda: self._fill(box, entry.rec))
        self._fill(box, entry.rec)

    def _fill(self, box, rec: Rec):
        ov = box.get_first_child()
        img = ov.get_child()
        emb = img.get_next_sibling()
        mark = ov.get_last_child()
        label = box.get_last_child()
        size = self.pane.grid_icon_size
        ov.set_size_request(size, size)
        self._fill_icon(img, emb, rec, size, True)
        chars = max(10, size // 7)
        label.set_width_chars(chars)
        label.set_max_width_chars(chars)
        label.set_text(rec.name)
        label.set_tooltip_text(rec.name)
        self._apply_copy_mark(mark, rec)
        self._apply_dim(box, img, rec)

    def _on_bg_click(self, gesture, n, x, y):
        self.pane.selection.unselect_all()
        ok, pt = self.view.compute_point(self.pane, Graphene.Point().init(x, y))
        self.pane.emit("context-menu", pt.x if ok else x, pt.y if ok else y, False)

    def apply_zoom(self):
        pass

    def update_columns(self):
        pass

    def sync_sort(self):
        pass

    def scroll_to(self, pos: int):
        self.view.scroll_to(pos, Gtk.ListScrollFlags.FOCUS, None)

    def focus(self):
        self.view.grab_focus()

