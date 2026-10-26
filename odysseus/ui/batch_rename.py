"""Dialogue « Renommer » pour plusieurs éléments, celui de Nautilus 46 (nautilus-batch-rename-dialog.ui) :
« Renommer en utilisant un modèle » (texte + balises, menu « Ajouter », ordre de numérotation) ou
« Rechercher et remplacer un texte », aperçu « ancien nom → nouveau nom » en direct, conflits
signalés (bouton Renommer grisé tant qu'il en reste, flèches pour passer de l'un à l'autre)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..backend import batchrename as br
from ..i18n import ntr, tr
from .common import shown

SORT_LABELS = {
    "name_asc": lambda: tr("Nom original (croissant)"), "name_desc": lambda: tr("Nom original (décroissant)"),
    "first_modified": lambda: tr("Premier modifié"), "last_modified": lambda: tr("Dernier modifié"),
    "first_created": lambda: tr("Premier créé"), "last_created": lambda: tr("Dernier créé"),
}
# libellés du menu « Ajouter » (ceux de Nautilus, avec majuscules) ; la balise insérée est br.tag_text
MENU_LABELS = {
    "num1": lambda: "1, 2, 3, 4", "num2": lambda: "01, 02, 03, 04", "num3": lambda: "001, 002, 003, 004",
    "creation_date": lambda: tr("Date de création"), "camera": lambda: tr("Modèle de l'appareil photo"),
    "season": lambda: tr("Numéro de saison"), "episode": lambda: tr("Numéro d'épisode"),
    "track": lambda: tr("Numéro de piste"), "artist": lambda: tr("Nom de l'artiste"),
    "title": lambda: tr("Titre"), "album": lambda: tr("Nom de l'album"),
    br.ORIGINAL: lambda: tr("Nom du fichier original"),
}


def dialog_title(items: list[br.Item]) -> str:
    dirs = sum(1 for it in items if it.is_dir)
    n = len(items)
    if dirs == n:
        return ntr("Renommer {0} dossier", "Renommer {0} dossiers", n).format(n)
    if dirs == 0:
        return ntr("Renommer {0} fichier", "Renommer {0} fichiers", n).format(n)
    return ntr("Renommer {0} fichier et dossier", "Renommer {0} fichiers et dossiers", n).format(n)


class BatchRenameDialog(Adw.Window):
    def __init__(self, parent: Gtk.Window, items: list[br.Item], on_apply: Callable[[list], None]):
        super().__init__(title=dialog_title(items), modal=True, transient_for=parent, destroy_with_parent=True,
                         default_width=640, default_height=563)
        self.items = items
        self.on_apply = on_apply
        self.sort = "name_asc"
        self.order = br.ordered(items, self.sort)
        self.new_names: dict[str, str] = {}
        self.problems: dict[str, str] = {}
        self.conflict_pos = 0
        self._closed = False
        self._meta_ready = False
        self._update_id = 0
        # contenu des dossiers lu une fois (comme Nautilus) : les conflits se calculent sans disque
        self._listing: dict[str, list] = {}
        for it in items:
            if it.folder not in self._listing:
                try:
                    self._listing[it.folder] = os.listdir(it.folder or ".")
                except OSError:
                    self._listing[it.folder] = []

        self._build()
        self._update()
        self.connect("close-request", self._on_close)
        threading.Thread(target=self._read_metadata, daemon=True).start()

    # ------------------------------------------------------------------
    def _build(self):
        actions = Gio.SimpleActionGroup()
        for tag in br.ALL_TAGS:
            a = Gio.SimpleAction.new("tag-" + tag, None)
            a.connect("activate", lambda _a, _v, t=tag: self._insert_tag(t))
            actions.add_action(a)
        order = Gio.SimpleAction.new_stateful("numbering-order", GLib.VariantType.new("s"),
                                              GLib.Variant.new_string(self.sort))
        order.connect("activate", self._on_order)
        actions.add_action(order)
        self.insert_action_group("rename", actions)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Gtk.HeaderBar(show_title_buttons=False)
        cancel = Gtk.Button(label=tr("A_nnuler"), use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.rename_button = Gtk.Button(label=tr("_Renommer"), use_underline=True, css_classes=["suggested-action"])
        self.rename_button.connect("clicked", lambda *_: self._apply())
        header.pack_end(self.rename_button)
        box.append(header)

        modes = Gtk.Box(spacing=15, halign=Gtk.Align.CENTER, margin_top=20, margin_bottom=20,
                        margin_start=20, margin_end=20)
        self.format_mode = Gtk.CheckButton(label=tr("Renommer en _utilisant un modèle"), use_underline=True,
                                           active=True)
        self.replace_mode = Gtk.CheckButton(label=tr("Rechercher et remplacer un _texte"), use_underline=True,
                                            group=self.format_mode)
        self.format_mode.connect("toggled", self._on_mode)
        modes.append(self.format_mode)
        modes.append(self.replace_mode)
        box.append(modes)

        self.stack = Gtk.Stack(vhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=100)
        # --- modèle
        fmt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15, margin_start=40, margin_end=40,
                      margin_bottom=10)
        line = Gtk.Box(halign=Gtk.Align.CENTER, css_classes=["linked"])
        self.name_entry = Gtk.Entry(width_request=400, hexpand=True, text=br.tag_text(br.ORIGINAL))
        self.name_entry.connect("changed", lambda *_: self._queue_update())
        self.name_entry.connect("activate", lambda *_: self._apply())
        self._delete_id = self.name_entry.get_delegate().connect("delete-text", self._on_delete_text)
        line.append(self.name_entry)
        self.add_menu = Gio.Menu()
        add_button = Gtk.MenuButton(menu_model=self.add_menu,
                                    child=Adw.ButtonContent(icon_name="list-add-symbolic", label=tr("Ajouter")))
        line.append(add_button)
        fmt.append(line)
        self.numbering = Gtk.Revealer(halign=Gtk.Align.CENTER)
        nb = Gtk.Box(spacing=10)
        nb.append(Gtk.Label(label=tr("Ordre de numérotation automatique")))
        self.order_menu = Gio.Menu()
        self.order_button = Gtk.MenuButton(menu_model=self.order_menu, always_show_arrow=True,
                                           label=SORT_LABELS[self.sort]())
        nb.append(self.order_button)
        self.numbering.set_child(nb)
        fmt.append(self.numbering)
        self.stack.add_named(fmt, "format")
        # --- rechercher / remplacer
        rep = Gtk.Grid(margin_start=40, margin_end=40, margin_bottom=10, halign=Gtk.Align.CENTER,
                       row_spacing=15, column_spacing=6)
        rep.attach(Gtk.Label(label=tr("Texte existant"), sensitive=False, xalign=1), 0, 0, 1, 1)
        self.find_entry = Gtk.Entry(width_request=375)
        rep.attach(self.find_entry, 1, 0, 3, 1)
        rep.attach(Gtk.Label(label=tr("Remplacer par"), sensitive=False, xalign=1), 0, 1, 1, 1)
        self.replace_entry = Gtk.Entry(width_request=375)
        rep.attach(self.replace_entry, 1, 1, 3, 1)
        for e in (self.find_entry, self.replace_entry):
            e.connect("changed", lambda *_: self._queue_update())
            e.connect("activate", lambda *_: self._apply())
        self.stack.add_named(rep, "replace")
        box.append(self.stack)

        # --- aperçu : une ligne par position, dans l'ordre de numérotation
        self.preview = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        self.rows = []
        for _it in self.items:
            row = Gtk.ListBoxRow(activatable=False)
            r = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
            old = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE, width_chars=20)
            arrow = Gtk.Label(label="←" if self.get_direction() == Gtk.TextDirection.RTL else "→")
            new = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE, width_chars=20)
            r.append(old)
            r.append(arrow)
            r.append(new)
            row.set_child(r)
            row._old, row._new = old, new
            self.preview.append(row)
            self.rows.append(row)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=250, min_content_width=600,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.preview,
                                           css_classes=["batch-rename-preview"])
        box.append(self.scroller)

        self.conflict_box = Gtk.Box(spacing=6, visible=False, margin_start=18, margin_end=12, margin_top=6,
                                    margin_bottom=6)
        self.conflict_label = Gtk.Label(hexpand=True, xalign=0, wrap=True)
        self.conflict_box.append(self.conflict_label)
        nav = Gtk.Box()
        for icon, step in (("go-down-symbolic", 1), ("go-up-symbolic", -1)):
            b = Gtk.Button(icon_name=icon, css_classes=["flat"])
            b.connect("clicked", lambda *_, s=step: self._next_conflict(s))
            nav.append(b)
        self.conflict_nav = nav
        self.conflict_box.append(nav)
        box.append(self.conflict_box)
        self.set_content(box)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(keys)
        self._fill_menus()
        self.name_entry.grab_focus()
        self.name_entry.set_position(-1)

    def _fill_menus(self):
        self.add_menu.remove_all()
        numbers = Gio.Menu()
        for tag, _w in br.NUMBERING:
            numbers.append(MENU_LABELS[tag](), "rename.tag-" + tag)
        self.add_menu.append_section(tr("Nombres automatiques"), numbers)
        meta = Gio.Menu()
        for tag in (br.available_tags(self.items) if self._meta_ready else []):
            meta.append(MENU_LABELS[tag](), "rename.tag-" + tag)
        meta.append(MENU_LABELS[br.ORIGINAL](), "rename.tag-" + br.ORIGINAL)
        self.add_menu.append_section(tr("Métadonnées"), meta)
        self.order_menu.remove_all()
        sorts = ["name_asc", "name_desc", "first_modified", "last_modified"]
        if self._meta_ready and all(it.meta.get("created") for it in self.items):
            sorts += ["first_created", "last_created"]
        for s in sorts:
            item = Gio.MenuItem.new(SORT_LABELS[s](), None)
            item.set_action_and_target_value("rename.numbering-order", GLib.Variant.new_string(s))
            self.order_menu.append_item(item)

    # ------------------------------------------------------------------
    def _read_metadata(self):
        # ffprobe par fichier audio / vidéo : quatre à la fois (un album entier en quelques secondes)
        def one(it):
            if self._closed or it.is_dir:
                return
            try:
                it.meta = br.read_metadata(it.path)
            except Exception:  # noqa: BLE001 — un fichier illisible n'empêche pas le reste
                it.meta = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, self.items))

        def ready():
            if not self._closed:
                self._meta_ready = True
                self._fill_menus()
                self._update()
            return False
        GLib.idle_add(ready)

    def _on_close(self, *_):
        self._closed = True
        if self._update_id:
            GLib.source_remove(self._update_id)
            self._update_id = 0
        return False

    # ------------------------------------------------------------------
    def _on_mode(self, *_):
        # « toggled » du premier bouton arrive avant que l'autre soit actif : on lit celui-ci
        replace = not self.format_mode.get_active()
        self.stack.set_visible_child_name("replace" if replace else "format")
        (self.find_entry if replace else self.name_entry).grab_focus()
        self._update()

    def _on_order(self, action, value):
        action.set_state(value)
        self.sort = value.get_string()
        self.order_button.set_label(SORT_LABELS[self.sort]())
        self._update()

    def _insert_tag(self, tag: str):
        """Balise insérée à la place du curseur (une numérotation à la fois, comme Nautilus)."""
        pos = self.name_entry.get_position()
        if tag.startswith("num"):
            text = self.name_entry.get_text()
            for t, _w in br.NUMBERING:
                while br.tag_text(t) in text:
                    i = text.index(br.tag_text(t))
                    text = text[:i] + text[i + len(br.tag_text(t)):]
                    if pos > i:
                        pos = max(i, pos - len(br.tag_text(t)))
            if text != self.name_entry.get_text():
                self.name_entry.set_text(text)          # (remet le curseur au début : pos gardé à part)
        pos = min(pos, len(self.name_entry.get_text()))
        if any(s < pos < e for s, e in br.tag_spans(self.name_entry.get_text())):
            pos = next(e for s, e in br.tag_spans(self.name_entry.get_text()) if s < pos < e)
        p = self.name_entry.get_delegate()
        p.insert_text(br.tag_text(tag), pos)
        self.name_entry.grab_focus()
        self.name_entry.set_position(pos + len(br.tag_text(tag)))

    def _on_delete_text(self, text, start, end):
        """Une balise s'efface d'un bloc : on étend l'effacement à toute balise entamée."""
        spans = br.tag_spans(self.name_entry.get_text())
        ns, ne = start, end if end >= 0 else len(self.name_entry.get_text())
        for s, e in spans:
            if s < ne and e > ns:
                ns, ne = min(ns, s), max(ne, e)
        if (ns, ne) != (start, end):
            GObject.signal_stop_emission_by_name(text, "delete-text")
            text.handler_block(self._delete_id)
            try:
                text.delete_text(ns, ne)
            finally:
                text.handler_unblock(self._delete_id)

    # ------------------------------------------------------------------
    def _queue_update(self):
        if not self._update_id:
            self._update_id = GLib.timeout_add(60, self._update_timeout)

    def _update_timeout(self):
        self._update_id = 0
        self._update()
        return False

    def _update(self):
        replace = not self.format_mode.get_active()
        if replace:
            find = self.find_entry.get_text()
            self.order = br.ordered(self.items, "name_asc")
            self.new_names = br.replace_names(self.items, find, self.replace_entry.get_text())
        else:
            find = ""
            template = self.name_entry.get_text()
            self.numbering.set_reveal_child(br.uses_numbering(template))
            self.order = br.ordered(self.items, self.sort)
            self.new_names = br.template_names(self.items, template, self.sort)
        self.problems = br.conflicts(self.items, self.new_names, lambda f: self._listing.get(f, []))
        for row, it in zip(self.rows, self.order):
            old = shown(it.name)
            if replace and find:
                parts = [GLib.markup_escape_text(p) for p in old.split(shown(find))]
                mark = "<span underline='single' weight='bold'>%s</span>" % GLib.markup_escape_text(shown(find))
                row._old.set_markup(mark.join(parts))
            else:
                row._old.set_text(old)
            row._new.set_text(shown(self.new_names[it.path]))
            (row.add_css_class if it.path in self.problems else row.remove_css_class)("conflict-row")
            row._path = it.path
        changed = any(self.new_names[it.path] != it.name for it in self.items)
        self.rename_button.set_sensitive(changed and not self.problems)
        if self.problems:
            self.conflict_pos = min(self.conflict_pos, len(self.problems) - 1)
            self._show_conflict()
        else:
            self.conflict_box.set_visible(False)

    def _conflict_rows(self):
        return [row for row in self.rows if getattr(row, "_path", None) in self.problems]

    def _show_conflict(self):
        rows = self._conflict_rows()
        if not rows:
            return
        row = rows[self.conflict_pos % len(rows)]
        self.conflict_label.set_text(self.problems[row._path])
        self.conflict_nav.set_visible(len(rows) > 1)
        self.conflict_box.set_visible(True)

    def _next_conflict(self, step: int):
        rows = self._conflict_rows()
        if not rows:
            return
        self.conflict_pos = (self.conflict_pos + step) % len(rows)
        self._show_conflict()
        row = rows[self.conflict_pos]
        ok, rect = row.compute_bounds(self.preview)
        if ok:
            adj = self.scroller.get_vadjustment()
            adj.clamp_page(rect.get_y(), rect.get_y() + rect.get_height())

    def _apply(self):
        if not self.rename_button.get_sensitive():
            return
        pairs = [(it.path, self.new_names[it.path]) for it in self.order if self.new_names[it.path] != it.name]
        self.close()
        self.on_apply(pairs)
