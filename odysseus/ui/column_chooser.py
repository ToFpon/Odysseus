"""« Colonnes visibles », le sélecteur de colonnes de Nautilus 46 (nautilus-column-chooser.ui) : Nom en
tête (toujours affiché), puis chaque colonne avec sa poignée (glisser pour la déplacer), un interrupteur
et un menu Monter / Descendre ; en bas, « N'appliquer qu'au dossier actuel » (métadonnées du dossier,
partagées avec Nautilus : backend/folder_columns). Les changements s'appliquent tout de suite."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import features
from ..i18n import tr
from .common import DialogWindow, shown
from .views import COLUMN_INFO, OPTIONAL_KEYS, full_order


def available_keys() -> list:
    """Colonnes proposées (Durée seulement si la fonctionnalité est disponible)."""
    return [k for k in OPTIONAL_KEYS if k != "duration" or features.available("duration_column")]


class ColumnChooser(DialogWindow):
    def __init__(self, window, pane):
        super().__init__(title=tr("Colonnes visibles"), content_width=420, content_height=-1)
        self.window, self.pane = window, pane
        self.set_resizable(False)
        visible, order, own = pane.column_prefs()
        self.visible = set(visible)
        self.order = [k for k in full_order(order) if k in available_keys()]

        tv = Adw.ToolbarView()
        place = pane.column_place()
        title = Adw.WindowTitle(title=tr("Colonnes visibles"),
                                subtitle=shown(os.path.basename(place.rstrip("/")) or place) if place else "")
        tv.add_top_bar(Adw.HeaderBar(title_widget=title))
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                margin_top=12, margin_bottom=18, margin_start=12, margin_end=12,
                                valign=Gtk.Align.START)
        tv.set_content(self.list)
        # « N'appliquer qu'au dossier actuel » : comme Nautilus, en bas, séparé de la liste
        bottom = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=18, margin_end=18)
        bottom.append(Gtk.Label(label=tr("N'appliquer qu'au dossier actuel"), xalign=0, hexpand=True))
        self.folder_switch = Gtk.Switch(active=own, valign=Gtk.Align.CENTER, sensitive=place is not None)
        self.folder_switch.connect("notify::active", self._on_folder_only)
        bottom.append(self.folder_switch)
        tv.add_bottom_bar(bottom)
        self.set_child(tv)
        self._fill()

    # ------------------------------------------------------------------
    def _fill(self):
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)
        name = Adw.ActionRow(title=COLUMN_INFO["name"][1], activatable=False)
        name.add_prefix(Gtk.Box(width_request=16))         # aligné sous les poignées des autres lignes
        self.list.append(name)
        for i, key in enumerate(self.order):
            self.list.append(self._row(key, i))

    def _row(self, key: str, index: int) -> Adw.ActionRow:
        row = Adw.ActionRow(title=COLUMN_INFO[key][1])
        row._key = key
        handle = Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["dim-label"])
        row.add_prefix(handle)
        sw = Gtk.Switch(active=key in self.visible, valign=Gtk.Align.CENTER)
        sw.connect("notify::active", lambda s, _p, k=key: self._set_visible(k, s.get_active()))
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        menu = Gio.Menu()
        # « Monter » est déjà pris (monter un disque) : un seul texte, une seule traduction
        menu.append(tr("Déplacer vers le haut"), "row.up")
        menu.append(tr("Déplacer vers le bas"), "row.down")
        row.add_suffix(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                      css_classes=["flat"], tooltip_text=tr("Déplacer")))
        group = Gio.SimpleActionGroup()
        for name, delta in (("up", -1), ("down", 1)):
            a = Gio.SimpleAction.new(name, None)
            a.set_enabled(0 <= index + delta < len(self.order))
            a.connect("activate", lambda _a, _p, k=key, d=delta: self._move(k, d))
            group.add_action(a)
        row.insert_action_group("row", group)
        # glisser la ligne (par la poignée ou ailleurs) pour la déplacer, comme dans Nautilus
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda _s, _x, _y, k=key: Gdk.ContentProvider.new_for_value(k))
        drag.connect("drag-begin", lambda s, _d, r=row: s.set_icon(Gtk.WidgetPaintable.new(r), 0, 0))
        row.add_controller(drag)
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", lambda _t, value, _x, y, k=key, r=row: self._dropped(value, k, y, r))
        row.add_controller(drop)
        return row

    # ------------------------------------------------------------------
    def _apply(self):
        hidden_unavailable = [k for k in self.pane.column_prefs()[0] if k not in available_keys()]
        visible = [k for k in self.order if k in self.visible] + hidden_unavailable
        self.pane.set_column_prefs(visible, self.order + [k for k in OPTIONAL_KEYS if k not in self.order],
                                   folder_only=self.folder_switch.get_active())

    def _set_visible(self, key: str, on: bool):
        (self.visible.add if on else self.visible.discard)(key)
        self._apply()

    def _move(self, key: str, delta: int):
        i = self.order.index(key)
        j = i + delta
        if not 0 <= j < len(self.order):
            return
        self.order[i], self.order[j] = self.order[j], self.order[i]
        self._apply()
        self._fill()

    def _dropped(self, value, target: str, y: float, row) -> bool:
        key = value if isinstance(value, str) else None
        if key is None or key == target or key not in self.order:
            return False
        self.order.remove(key)
        i = self.order.index(target) + (1 if y > row.get_height() / 2 else 0)
        self.order.insert(i, key)
        self._apply()
        GLib.idle_add(lambda: (self._fill(), False)[1])     # pas pendant le dépôt sur cette ligne
        return True

    def _on_folder_only(self, sw, _p):
        place = self.pane.column_place()
        if place is None:
            return
        if sw.get_active():
            self._apply()                           # le réglage du moment devient celui du dossier
        else:
            from ..backend import folder_columns
            folder_columns.clear(place)             # le dossier reprend les colonnes par défaut
            visible, order, _own = self.pane.column_prefs()
            self.visible = set(visible)
            self.order = [k for k in full_order(order) if k in available_keys()]
            self._fill()


def show(window, pane):
    ColumnChooser(window, pane).present(window)
