"""Barre de chemin à la manière de Nautilus : fil d'Ariane cliquable, saisie avec Ctrl+L.

Clic sur un segment : y aller ; clic du milieu : nouvel onglet ; déposer des fichiers sur un
segment : les copier ou déplacer dans ce dossier. En saisie, comme Nautilus, les sous-dossiers qui
commencent par le texte tapé sont proposés sous le champ ; Tab complète.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ..i18n import tr
from .common import SpringLoad
from .common import shown as _shown



def mount_roots() -> dict:
    """Racines des disques montés montrés dans la barre latérale (Périphériques) : chemin → (nom,
    icône). Moniteur de volumes de GNOME, plus les disques fstab qu'il masque."""
    from ..backend import devices
    out = {}
    for disk in devices.mounted_disks():
        out[disk.path] = (disk.name, "drive-harddisk-symbolic")
    for mount in Gio.VolumeMonitor.get().get_mounts():
        root = mount.get_root()
        path = root.get_path() if root is not None else None
        if path and path != "/" and not mount.is_shadowed():
            out[path] = (mount.get_name(), mount.get_symbolic_icon())
    return out

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
            self.menu_btn.set_has_frame(False)     # plat, même dans le cadre « linked » du fil d'Ariane
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
        # en capture : avant le texte, qui prendrait sinon les flèches (déplacement du focus)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.timeout_add(150, self._maybe_leave))
        self.entry.add_controller(focus)
        self.stack.add_named(self.entry, "entry")
        self.append(self.stack)
        self._build_suggestions()

    # ------------------------------------------------------------------
    def set_location(self, location: str, display: str, archive_root: str = ""):
        """location : emplacement réel ; display : chemin affiché (archive : …/a.7z/dossier)."""
        self.location = location
        self.display = display
        if self.stack.get_visible_child_name() != "entry":
            self.entry.set_text(_shown(display))
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
                       "computer:///": (tr("Ce PC"), "computer-symbolic"),
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
        # comme Nautilus : un disque monté (/mnt/…, /media/…) commence le chemin, avec son nom et son
        # icône, au lieu de « Zorin OS / mnt / Stockage_Linux » ; le plus profond l'emporte
        starts = {home: (tr("Dossier personnel"), "user-home-symbolic")}
        starts.update(mount_roots())
        inside = [r for r in starts if r != "/" and (display == r or display.startswith(r.rstrip("/") + "/"))]
        if inside:
            base = max(inside, key=len)
            label, icon = starts[base]
            segs.append((label, base, icon))
            rest = display[len(base):].strip("/")
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
            if isinstance(icon, Gio.Icon):
                box.append(Gtk.Image(gicon=icon))
            elif icon:
                box.append(Gtk.Image(icon_name=icon))
            if label:
                # Pas d'abréviation automatique : le fil d'Ariane défile plutôt que de rogner
                # tous les segments ; seuls les noms très longs sont raccourcis.
                limit = 40 if last else 28
                shown = label if len(label) <= limit else label[:limit // 2 - 1] + "…" + label[-(limit // 2 - 1):]
                box.append(Gtk.Label(label=_shown(shown)))
            btn.set_child(box)
            btn.set_tooltip_text(_shown(target if "://" not in target else GLib.uri_unescape_string(target, None)))
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
        # comme Nautilus : tenir un élément au-dessus d'un dossier du chemin y va
        spring = SpringLoad(widget, lambda: self.emit("navigate", target))

        def enter(_t, x, y):
            widget.add_css_class("drop-row")
            spring.enter(x, y)
            return Gdk.DragAction.MOVE

        def motion(_t, x, y):
            spring.motion(x, y)
            return Gdk.DragAction.MOVE
        t.connect("enter", enter)
        t.connect("motion", motion)
        t.connect("leave", lambda *_: (widget.remove_css_class("drop-row"), spring.leave()))

        def drop(_t, value, x, y):
            spring.leave()
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
        self.entry.set_text(_shown(text if text is not None else self.display))
        self.stack.set_visible_child_name("entry")
        self.entry.grab_focus()
        if text is None:
            self.entry.select_region(0, -1)
        else:
            self.entry.set_position(-1)

    def stop_edit(self):
        self.hide_suggestions()
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
        row = self.suggest_list.get_selected_row() if self.suggest.get_visible() else None
        if row is not None:                 # suggestion choisie aux flèches : on y va
            entry.set_text(row.path)
        text = entry.get_text().strip()
        if text:
            self.stop_edit()
            self.emit("navigate", text)

    def _on_entry_key(self, _c, keyval, _code, state):
        shown_list = self.suggest.get_visible()
        if shown_list and keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move_suggestion(1 if keyval == Gdk.KEY_Down else -1)
            return True
        if shown_list and keyval == Gdk.KEY_Escape:     # Échap ferme d'abord la liste
            self.hide_suggestions()
            return True
        if shown_list and keyval == Gdk.KEY_Tab and self.suggest_list.get_selected_row() is not None:
            self._accept(self.suggest_list.get_selected_row())
            return True
        if keyval == Gdk.KEY_Escape:
            self.entry.set_text(_shown(self.display))
            self.stop_edit()
            self.emit("navigate", "")       # rend le focus à la vue
            return True
        if keyval == Gdk.KEY_Tab:
            self._complete()
            return True
        return False

    # -- suggestions (comme Nautilus) ----------------------------------------
    MAX_SUGGESTIONS = 10

    def _build_suggestions(self):
        self.suggest_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["path-suggestions"])
        self.suggest_list.set_activate_on_single_click(True)
        self.suggest_list.connect("row-activated", lambda _l, row: self._accept(row))
        # le champ garde le focus : la liste ne le prend jamais (on continue à taper)
        self.suggest = Gtk.Popover(child=self.suggest_list, autohide=False, has_arrow=False,
                                   position=Gtk.PositionType.BOTTOM, halign=Gtk.Align.START,
                                   can_focus=False, css_classes=["path-suggestions-popover"])
        self.suggest.set_parent(self.entry)
        self.suggest_list.set_can_focus(False)
        self._suggest_timer = 0
        self._suggest_token = 0
        self.entry.connect("changed", self._queue_suggestions)
        self.connect("unrealize", lambda *_: self.hide_suggestions())

    def hide_suggestions(self):
        self._suggest_token += 1
        if self._suggest_timer:
            GLib.source_remove(self._suggest_timer)
            self._suggest_timer = 0
        if self.suggest.get_visible():
            self.suggest.popdown()

    def _queue_suggestions(self, *_):
        if self._suggest_timer:
            GLib.source_remove(self._suggest_timer)
        self._suggest_timer = GLib.timeout_add(120, self._update_suggestions)

    def _update_suggestions(self):
        self._suggest_timer = 0
        self._suggest_token += 1
        token = self._suggest_token
        typed = self.entry.get_text()
        if self.stack.get_visible_child_name() != "entry" or not typed or "://" in typed:
            self.hide_suggestions()
            return False
        text = os.path.expanduser(typed)
        if not text.startswith("/"):
            self.hide_suggestions()
            return False
        folder, prefix = os.path.split(text)
        shown_folder = typed[:len(typed) - len(prefix)]     # tel que tapé (« ~/ » gardé)

        def work():
            # dans un fil : un dossier sur un disque réseau lent ne bloque pas la saisie
            try:
                low = prefix.casefold()
                names = [e.name for e in os.scandir(folder or "/")
                         if e.name.casefold().startswith(low) and (prefix.startswith(".") or not e.name.startswith("."))
                         and e.is_dir()]
            except OSError:
                names = []
            names.sort(key=lambda n: GLib.utf8_collate_key_for_filename(n, -1))
            GLib.idle_add(lambda: (self._show_suggestions(token, shown_folder, folder, prefix, names), False)[1])
        threading.Thread(target=work, daemon=True).start()
        return False

    def _show_suggestions(self, token, shown_folder, folder, prefix, names):
        if token != self._suggest_token or self.stack.get_visible_child_name() != "entry":
            return
        while (row := self.suggest_list.get_first_child()) is not None:
            self.suggest_list.remove(row)
        # rien à proposer de plus que ce qui est tapé : pas de liste
        if not names or names == [prefix]:
            self.hide_suggestions()
            return
        for name in names[:self.MAX_SUGGESTIONS]:
            row = Gtk.ListBoxRow()
            row.path = os.path.join(folder, name) + "/"
            row.text = shown_folder + name + "/"
            lbl = Gtk.Label(xalign=0, use_markup=True)
            lbl.set_markup('<span alpha="55%">{0}</span>{1}/'.format(
                GLib.markup_escape_text(_shown(shown_folder)), GLib.markup_escape_text(_shown(name))))
            row.set_child(lbl)
            self.suggest_list.append(row)
        self.suggest.set_size_request(max(self.entry.get_width(), 1), -1)
        if not self.suggest.get_visible():
            self.suggest.popup()

    def _move_suggestion(self, step: int):
        rows = []
        r = self.suggest_list.get_first_child()
        while r is not None:
            rows.append(r)
            r = r.get_next_sibling()
        if not rows:
            return
        cur = self.suggest_list.get_selected_row()
        i = rows.index(cur) + step if cur in rows else (0 if step > 0 else len(rows) - 1)
        if 0 <= i < len(rows):
            self.suggest_list.select_row(rows[i])
        else:
            self.suggest_list.unselect_all()        # au-delà des bouts : retour au texte tapé

    def _accept(self, row):
        """Suggestion retenue (Tab, clic) : le champ la reprend, et on continue dans ce dossier."""
        self.entry.set_text(row.text)
        self.entry.set_position(-1)
        self.entry.grab_focus()

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
