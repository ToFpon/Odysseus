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
from .common import SpringLoad, date_label, perms_label, shown

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
        with scanner.scan_slot():     # pas de lecture disque concurrente (voir scanner.scan_slot)
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

    def _attach_empty_click(self, view):
        """Clic gauche dans une zone vide de la vue : plus rien de sélectionné, comme Nautilus
        (Ctrl / Maj gardent la sélection, pour commencer un lasso qui s'y ajoute)."""
        click = Gtk.GestureClick(button=1)

        def pressed(gesture, _n, x, y):
            if gesture.get_current_event_state() & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                return
            w = view.pick(x, y, Gtk.PickFlags.DEFAULT)
            while w is not None and w is not view:
                # un élément (case ou ligne), ou l'en-tête des colonnes : pas une zone vide
                if getattr(w, "_rec", None) is not None or w.get_css_name() in ("header", "row", "child"):
                    return
                w = w.get_parent()
            self.pane.selection.unselect_all()
        click.connect("pressed", pressed)
        view.add_controller(click)

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

    def _fill_icon(self, image: Gtk.Image, emblems: Gtk.Box, rec: Rec, size: int, allow_thumb: bool,
                   emblem_size: int = 0):
        image.set_pixel_size(size)
        tex = icons.request_thumbnail(rec, size, self.pane.model) if (allow_thumb and icons.thumbnails_wanted(rec)) else None
        if tex is not None:
            image.set_from_paintable(tex)
            image.add_css_class("thumbnail")
        else:
            image.set_from_gicon(icons.gicon_for(rec))
            image.remove_css_class("thumbnail")
        icons.request_meta(rec, self.pane.model)
        self._fill_emblems(emblems, icons.emblems_for(rec), emblem_size or max(10, min(24, size // 3)))

    @staticmethod
    def _fill_emblems(box: Gtk.Box, names: list[str], size: int):
        """Emblèmes à la suite (lien, cadenas, croix…), comme Nautilus ; images réutilisées."""
        child = box.get_first_child()
        for name in names:
            if child is None:
                child = Gtk.Image(css_classes=["emblem"])
                box.append(child)
            child.set_from_icon_name(name)
            child.set_pixel_size(size)
            child.set_visible(True)
            child = child.get_next_sibling()
        while child is not None:
            child.set_visible(False)
            child = child.get_next_sibling()
        box.set_visible(bool(names))

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

    # Les gestionnaires des cellules ne capturent ni la cellule ni son Gtk.ListItem : ils les
    # retrouvent par controller.get_widget() et l'élément affiché (widget._rec). Capturés dans des
    # fermetures Python, ils formaient des cycles (cellule → contrôleur → fermeture → cellule) et
    # GTK les libérait dans le désordre en recréant les cellules : segfault en changeant la
    # visibilité des colonnes après deux glisser-déposer (sortie du double panneau).
    def _cell_pos(self, widget) -> int:
        rec = getattr(widget, "_rec", None)
        i = self.pane.model.index_of(rec) if rec is not None else -1
        return i if i >= 0 else Gtk.INVALID_LIST_POSITION

    def _attach_menu_gesture(self, widget, list_item=None):
        g = Gtk.GestureClick(button=3)

        def pressed(gesture, n, x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            widget = gesture.get_widget()
            pos = self._cell_pos(widget)
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
            rec = getattr(gesture.get_widget(), "_rec", None)
            if rec is not None and rec.is_dir:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.pane.emit("open-in-tab", rec)
        m.connect("pressed", middle)
        widget.add_controller(m)

    def _attach_drag(self, widget, list_item=None):
        """Glisser des fichiers. La source est posée sur chaque cellule, comme dans Nautilus : sur la
        vue entière, la sélection par rectangle (rubberband) prend le geste et rien ne se glisse."""
        pane = self.pane
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK,
                              propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def prepare(source, x, y):
            pane.selection.cancel_pending()         # glisser toute la sélection, pas le seul élément
            pos = self._cell_pos(source.get_widget())
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
            rec = getattr(_t.get_widget(), "_rec", None)
            return bool(rec is not None and rec.is_dir and rec.path and self.pane.mode == "fs")

        def spring():
            # au moment d'ouvrir : la cellule montre-t-elle toujours ce dossier, et y déposer
            # a-t-il un sens (pas un des dossiers qu'on fait glisser, ni leur propre dossier) ?
            rec = getattr(t.get_widget(), "_rec", None)
            if rec is None or rec.path != t._spring_path or not rec.is_dir or self.pane.mode != "fs":
                return
            if rec.path in DRAG_PATHS or not self.pane.preferred_action(t.get_current_drop(), rec):
                return
            # la cellule va être recyclée pour le nouveau dossier sans recevoir « leave » : sans ça,
            # la surbrillance de dépôt restait sur une ligne du nouveau dossier
            t.get_widget().remove_css_class("drop-row")
            t._spring_path = None
            self.pane.open(rec.path)
        t._spring = SpringLoad(widget, spring)
        t._spring_path = None

        def enter(_t, x, y):
            w = _t.get_widget()
            w.add_css_class("drop-row")
            rec = getattr(w, "_rec", None)
            if rec is not None and rec.path != _t._spring_path:
                _t._spring_path = rec.path
                _t._spring.enter(x, y)
            return self.pane.preferred_action(_t.get_current_drop(), rec)

        def motion(_t, x, y):
            _t._spring.motion(x, y)
            return enter(_t, x, y)

        def leave(_t):
            _t.get_widget().remove_css_class("drop-row")
            _t._spring.leave()
            _t._spring_path = None

        def drop(_t, value, x, y):
            w = _t.get_widget()
            w.remove_css_class("drop-row")
            rec = getattr(w, "_rec", None)
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if rec is None or not paths or rec.path in paths:
                return False
            self.pane.handle_drop(paths, rec.path, _t.get_current_drop())
            return True
        t.connect("accept", accept)
        t.connect("enter", enter)
        t.connect("motion", motion)
        t.connect("leave", leave)
        t.connect("drop", lambda *a: (t._spring.leave(), drop(*a))[1])
        widget.add_controller(t)


class ListView(Gtk.ScrolledWindow, _CellMixin):
    """Vue liste (Gtk.ColumnView) ; même classe CSS que Nautilus pour hériter des règles des thèmes."""
    __gtype_name__ = "OdysseusListView"

    def __init__(self, pane: "Pane"):
        super().__init__(vexpand=True, hexpand=True)
        self.pane = pane
        self.add_css_class("nautilus-list-view")
        self.view = None
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}
        self._widths: dict[str, int] = {}
        self._syncing = False
        self._narrow = 0
        self.apply_zoom()
        self.update_columns()
        self.sync_sort()

    # -- colonnes ----------------------------------------------------------
    def do_size_allocate(self, width, height, baseline):
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)
        # Panneau étroit (double panneau) : les colonnes facultatives s'effacent pour laisser
        # la place au nom, comme Nautilus.
        level = 0 if width >= 900 else (1 if width >= 520 else (2 if width >= 400 else 3))
        if level != self._narrow:
            self._narrow = level
            GLib.idle_add(self._idle_columns)

    def _idle_columns(self):
        # panneau fermé entre-temps (sortie du double panneau, onglet fermé) : ses colonnes sont
        # détruites, y toucher fait planter GTK (segfault vu dans tests/ui_mouse.py)
        if self.get_root() is not None:
            self.update_columns()
        return False

    def update_columns(self):
        """Colonnes affichées selon le mode et la largeur. GTK 4.14 tient mal la liste des cellules
        d'une colonne qu'on montre ou cache dans une vue déjà remplie : une cellule libérée y reste
        et la suppression suivante d'une ligne plante (gtk_column_view_column_queue_resize, vu avec
        les symboles de libgtk-4 en sortant du double panneau après des glisser-déposer ; 0 plantage
        sur 5 sans ces changements, 2 à 4 sur 5 avec). On ne touche donc JAMAIS à la visibilité
        d'une colonne : quand l'ensemble change, la vue est reconstruite avec les bonnes colonnes,
        modèle débranché (pas de lignes, pas de cellules). C'est rare : passage étroit ↔ large,
        corbeille, archive, recherche, réglage des colonnes."""
        vis = self._wanted_columns()
        wanted = [key for key, *_ in COLUMNS if vis[key]]
        if self.view is None or wanted != list(self.columns):
            self._rebuild(wanted)
        loc = self.columns.get("location")
        if loc is not None:
            loc.set_title(tr("Emplacement d'origine") if self.pane.mode == "trash" else tr("Emplacement"))

    def _rebuild(self, keys: list):
        old = self.view
        model = old.get_model() if old is not None else None
        adj = self.get_vadjustment()
        top = adj.get_value() if (old is not None and adj is not None) else 0
        for key, col in self.columns.items():       # largeurs réglées à la main : gardées
            self._widths[key] = col.get_fixed_width()
        if old is not None:
            old.set_model(None)
        pane = self.pane
        view = Gtk.ColumnView(model=None, show_row_separators=False, show_column_separators=False,
                              reorderable=True, enable_rubberband=True, css_classes=["file-view"])
        if hasattr(view, "set_tab_behavior"):
            view.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
        view.connect("activate", lambda _v, pos: pane.activate_position(pos))
        view.set_single_click_activate(settings["single_click"])
        self.columns = {}
        for key, title, width, xalign, sort_key in COLUMNS:
            if key not in keys:
                continue
            f = Gtk.SignalListItemFactory()
            if key == "name":
                f.connect("setup", self._setup_name)
                f.connect("bind", self._bind_name)
            else:
                f.connect("setup", self._setup_text, xalign)
                f.connect("bind", self._bind_text, key)
            f.connect("unbind", lambda _f, li: self._untrack(li.get_child()))
            col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, expand=(key == "name"))
            w = self._widths.get(key, width or -1)
            if w and w > 0:
                col.set_fixed_width(w)
            if sort_key:
                col.set_sorter(Gtk.CustomSorter.new(lambda *_: 0))
                col._sort_key = sort_key
            view.append_column(col)
            self.columns[key] = col
        view.get_sorter().connect("changed", self._on_header_sort)
        bg = Gtk.GestureClick(button=3)
        bg.connect("pressed", self._on_bg_click)
        view.add_controller(bg)
        self._attach_empty_click(view)
        lp = Gtk.GestureLongPress(touch_only=True)
        lp.connect("pressed", lambda g, x, y: pane.emit("context-menu", x, y, True))
        view.add_controller(lp)
        self.view = view
        self.set_child(view)
        self.apply_zoom()
        self.sync_sort()
        if model is not None:
            view.set_model(model)
            if top:
                GLib.idle_add(lambda: (self.get_root() is not None and adj.set_value(top), False)[1])

    def _wanted_columns(self) -> dict:
        mode = self.pane.mode
        wanted = set(settings["list_columns"])
        drop = {0: set(), 1: {"perms", "owner", "type"}, 2: {"perms", "owner", "type", "packed", "ratio", "date"},
                3: {"perms", "owner", "type", "date", "duration", "packed", "ratio", "location"}}[self._narrow]
        wanted -= drop
        out = {}
        for key, *_ in COLUMNS:
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
            out[key] = vis and key not in drop
        return out

    def sync_sort(self):
        if self.view is None:
            return
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
        if self.view is None:
            return
        _size, compact = self.pane.list_zoom
        if compact:
            self.view.add_css_class("compact")
        else:
            self.view.remove_css_class("compact")

    # -- cellules ----------------------------------------------------------
    def _setup_name(self, _f, li):
        box = Gtk.Box(spacing=10, css_classes=["name-cell"])
        box.append(Gtk.Box(css_classes=["copy-mark"], visible=False))
        box.append(Gtk.Image())
        box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
        # emblèmes juste après le nom, comme Nautilus
        box.append(Gtk.Box(spacing=4, hexpand=True, halign=Gtk.Align.START, visible=False,
                           css_classes=["emblems"]))
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
        img = mark.get_next_sibling()
        label = img.get_next_sibling()
        emb = box.get_last_child()
        size, _compact = self.pane.list_zoom
        self._fill_icon(img, emb, rec, size, size >= 32, emblem_size=16)
        label.set_text(shown(rec.name))
        label.set_tooltip_text(shown(rec.name) if len(rec.name) > 50 else None)
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
        self.view = Gtk.GridView(model=None, factory=f, max_columns=8, min_columns=1,
                                 enable_rubberband=True, css_classes=["file-grid"])
        self.view.connect("activate", lambda _v, pos: pane.activate_position(pos))
        self.view.set_single_click_activate(settings["single_click"])
        self.set_child(self.view)
        bg = Gtk.GestureClick(button=3)
        bg.connect("pressed", self._on_bg_click)
        self.view.add_controller(bg)
        self._attach_empty_click(self.view)
        self._pending = None
        self._pending_id = 0
        self._cols_id = 0

    def do_size_allocate(self, width, height, baseline):
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)
        # Gtk.GridView remplit d'abord « max_columns » colonnes, sans connaître la largeur des cases :
        # avec 64, il créait ~2 000 cases (32 rangées × 64). On lui donne le vrai nombre de colonnes.
        size = self.pane.grid_icon_size
        cell = max(size, max(10, size // 7) * 8) + 20
        cols = max(1, min(64, (width - 24) // cell))
        if cols != self.view.get_max_columns() and not self._cols_id:
            self._cols_id = GLib.idle_add(self._apply_cols, cols)

    def _apply_cols(self, cols):
        self._cols_id = 0
        if self.get_root() is not None:
            self.view.set_max_columns(cols)
        return False

    def attach(self, model):
        """Brancher le modèle seulement quand la grille a une taille : branchée encore invisible (avant
        le changement de page), Gtk.GridView, sans hauteur connue, créait ~2 000 cases d'un coup
        (1,3 à 2,6 s figées sur 20 000 fichiers, contre une quarantaine visibles)."""
        if model is None or (self.get_mapped() and self.get_width() > 0 and self.get_height() > 0):
            self._pending = None
            if self.view.get_model() is not model:
                self.view.set_model(model)
            return
        self._pending = model
        if not self._pending_id:
            self._pending_id = GLib.timeout_add(30, self._attach_pending)

    def _attach_pending(self):
        if self._pending is None or self.get_root() is None:
            self._pending_id = 0
            return False
        if not (self.get_mapped() and self.get_width() > 0 and self.get_height() > 0):
            return True                     # pas encore dimensionnée : on réessaie
        self._pending_id = 0
        model, self._pending = self._pending, None
        if self.view.get_model() is not model:
            self.view.set_model(model)
        return False

    def _setup(self, _f, li):
        # toute la largeur de la case : centrée, la boîte ne recevait que sa largeur naturelle, plus
        # étroite que celle où GTK avait mesuré le nom ; le nom repassait à la ligne plus tôt, prenait
        # une ligne de plus et débordait sous la sélection (selon la police et sa taille)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["grid-cell"],
                      halign=Gtk.Align.FILL, valign=Gtk.Align.START)
        ov = Gtk.Overlay(halign=Gtk.Align.CENTER)
        img = Gtk.Image()
        ov.set_child(img)
        ov.add_overlay(Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False, spacing=2,
                               css_classes=["emblems"]))
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
        label.set_text(shown(rec.name))
        label.set_tooltip_text(shown(rec.name))
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

