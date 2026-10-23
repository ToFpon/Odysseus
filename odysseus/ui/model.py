"""Modèle de liste « virtuel » des vues de fichiers.

Implémente Gio.ListModel en Python au-dessus d'une simple liste d'enregistrements (Rec).
GTK ne demande que les éléments affichés : un objet GObject n'est fabriqué que pour ces
lignes-là (environ 200 sur 100 000), ce qui rend l'ouverture des dossiers très peuplés
quasi instantanée. Tri, filtre et fichiers cachés sont gérés ici, à vitesse C (list.sort).
"""

from __future__ import annotations

import bisect
import contextlib
import os
import threading
import traceback
import unicodedata
from typing import Optional

from gi.repository import Gio, GLib, GObject, Gtk

from ..backend import scanner
from ..backend.scanner import Rec
from ..settings import settings

# En dessous de ce nombre d'éléments, tailles et dates sont lues avant le premier affichage.
STAT_INLINE = 4000


class Entry(GObject.Object):
    """Objet présenté à GTK pour une ligne affichée ; « changed » rafraîchit ses cellules."""
    __gtype_name__ = "OdysseusEntry"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, rec: Rec):
        super().__init__()
        self.rec = rec


class _Rev:
    """Inverse l'ordre d'une clé (insertion par dichotomie en ordre décroissant)."""
    __slots__ = ("k",)

    def __init__(self, k):
        self.k = k

    def __lt__(self, other):
        return other.k < self.k


def fold(text: str) -> str:
    """Minuscules sans accents : « Été » trouve « ete »."""
    t = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


class DirModel(GObject.Object, Gio.ListModel):
    __gtype_name__ = "OdysseusDirModel"
    __gsignals__ = {
        # state : "loading", "ready", "error"
        "state": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "reset": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "gone": (GObject.SignalFlags.RUN_FIRST, None, ()),     # le dossier affiché a disparu
    }

    def __init__(self):
        super().__init__()
        self.all: list[Rec] = []         # trié, cachés compris
        self.items: list[Rec] = []       # visible, dans l'ordre affiché
        self.location: Optional[str] = None   # chemin local ou URI
        self.static = False              # archive, recherche : pas de surveillance
        self.filter_text = ""
        self.show_hidden = settings["show_hidden"]
        self.sort_key = settings["sort_key"]
        self.sort_reverse = settings["sort_reverse"]
        self.folders_first = settings["folders_first"]
        self.selection: Optional[Gtk.MultiSelection] = None
        self.error: Optional[str] = None
        self.error_code = None
        self.loading = False
        self._gen = 0
        self._cancel = threading.Event()
        self._monitor = None
        self._pending: dict = {}
        self._pending_id = 0
        self._index: Optional[dict] = None

    # -- Gio.ListModel -----------------------------------------------------
    def do_get_item_type(self):
        return Entry.__gtype__

    def do_get_n_items(self):
        return len(self.items)

    def do_get_item(self, i):
        if 0 <= i < len(self.items):
            r = self.items[i]
            if r.obj is None:
                r.obj = Entry(r)
            return r.obj
        return None

    # -- accès -------------------------------------------------------------
    def rec_at(self, i: int) -> Optional[Rec]:
        return self.items[i] if 0 <= i < len(self.items) else None

    def index_of(self, rec: Rec) -> int:
        if self._index is None:
            self._index = {id(r): i for i, r in enumerate(self.items)}
        return self._index.get(id(rec), -1)

    def find_name(self, name: str) -> int:
        for i, r in enumerate(self.items):
            if r.name == name:
                return i
        return -1

    def notify_rec(self, rec: Rec):
        """Une donnée paresseuse est arrivée (miniature, durée…) : rafraîchir la ligne."""
        if rec.obj is not None:
            rec.obj.emit("changed")

    def refresh_rows(self):
        for r in self.items:
            if r.obj is not None:
                r.obj.emit("changed")

    # -- chargement --------------------------------------------------------
    def cancel(self):
        self._gen += 1
        self._cancel.set()
        self._cancel = threading.Event()
        self._stop_monitor()

    def load(self, location: str):
        """Charge un dossier (chemin local ou URI GIO) dans un fil d'exécution."""
        self.cancel()
        self.static = False
        self.location = location
        self.error = None
        self.error_code = None
        self.loading = True
        gen, cancel = self._gen, self._cancel
        local = not _is_uri(location)

        def work():
            # GIO (réseau, corbeille, admin://) se lit en C sans le verrou de l'interpréteur : pas de
            # convoi possible, et un serveur lent ne doit pas faire attendre les dossiers locaux
            with (scanner.scan_slot() if local else contextlib.nullcontext()):
                if cancel.is_set():
                    return
                try:
                    if local:
                        recs = scanner.scan_local(location, cancel, with_stat=False)
                        if len(recs) <= STAT_INLINE or self.sort_key not in ("name", "type"):
                            scanner.stat_all(recs, cancel)
                            GLib.idle_add(self._loaded, gen, recs, None, True)
                        else:
                            # très gros dossier : les noms d'abord, tailles et dates ensuite
                            GLib.idle_add(self._loaded, gen, recs, None, False)
                            scanner.stat_all(recs, cancel)
                            GLib.idle_add(self._stats_done, gen)
                    else:
                        recs = scanner.scan_gio(Gio.File.new_for_uri(location), cancel)
                        GLib.idle_add(self._loaded, gen, recs, None, True)
                except OSError as e:
                    GLib.idle_add(self._loaded, gen, [], (e.strerror or str(e), e.errno), True)
                except GLib.Error as e:
                    GLib.idle_add(self._loaded, gen, [], (e.message, e.code), True)
                except Exception as e:     # jamais de chargement sans fin : toute erreur s'affiche
                    traceback.print_exc()
                    GLib.idle_add(self._loaded, gen, [], (str(e), None), True)
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add(150, self._maybe_spinner, gen)
        if local:
            self._start_monitor(location)
        else:
            self._start_uri_monitor(location)

    def _maybe_spinner(self, gen):
        if gen == self._gen and self.loading:
            self.emit("state", "loading")
        return False

    def _loaded(self, gen, recs, error, complete):
        if gen != self._gen:
            return False
        self.loading = False
        if error:
            self.error, self.error_code = error
            self.all = []
            self._publish(keep_selection=False)
            self.emit("state", "error")
            return False
        self.all = scanner.sort_recs(recs, self.sort_key, self.sort_reverse, self.folders_first)
        self._publish(keep_selection=False)
        self.emit("state", "ready")
        return False

    def _stats_done(self, gen):
        if gen == self._gen:
            self.refresh_rows()
        return False

    def set_static(self, location: str, recs: list[Rec]):
        """Contenu fourni de l'extérieur : archive, résultats de recherche."""
        self.cancel()
        self.static = True
        self.location = location
        self.error = None
        self.loading = False
        self.all = scanner.sort_recs(recs, self.sort_key, self.sort_reverse, self.folders_first)
        self._publish(keep_selection=False)
        self.emit("state", "ready")

    def append_static(self, recs: list[Rec]):
        """Ajout au fil de l'eau (recherche récursive) ; trié à la fin par finish_static()."""
        if not recs:
            return
        self.all.extend(recs)
        vis = [r for r in recs if self._visible(r)]
        if vis:
            pos = len(self.items)
            self.items.extend(vis)
            self._index = None
            self.items_changed(pos, 0, len(vis))

    def finish_static(self):
        self.resort()

    def set_error(self, message: str):
        self.cancel()
        self.error = message
        self.all = []
        self._publish(keep_selection=False)
        self.emit("state", "error")

    def clear(self):
        self.cancel()
        self.all = []
        self._publish(keep_selection=False)

    # -- tri / filtre ------------------------------------------------------
    def _visible(self, r: Rec) -> bool:
        if r.hidden and not self.show_hidden:
            return False
        if self.filter_text:
            f = r.get("fold")
            if f is None:
                f = fold(r.name)
                r.set("fold", f)
            return self.filter_text in f
        return True

    def set_sort(self, key: str, reverse: bool, folders_first: Optional[bool] = None):
        self.sort_key, self.sort_reverse = key, reverse
        if folders_first is not None:
            self.folders_first = folders_first
        self.resort()

    def resort(self):
        self.all = scanner.sort_recs(self.all, self.sort_key, self.sort_reverse, self.folders_first)
        self._publish(keep_selection=True)

    def set_filter(self, text: str):
        text = fold(text.strip())
        if text == self.filter_text:
            return
        self.filter_text = text
        self._publish(keep_selection=True)

    def set_show_hidden(self, value: bool):
        if value != self.show_hidden:
            self.show_hidden = value
            self._publish(keep_selection=True)

    def _publish(self, keep_selection: bool):
        selected = self.selected_recs() if keep_selection and self.selection else []
        old = len(self.items)
        items = [r for r in self.all if self._visible(r)]
        self._index = None
        if old and len(items) > 2000:
            # Remplacer en une fois ferait chercher à GTK l'élément qui avait le focus parmi
            # tous les nouveaux éléments (100 000 objets créés pour rien) : vider, puis remplir.
            # Le contenu doit rester cohérent avec chaque signal.
            self.items = []
            self.items_changed(0, old, 0)
            self.items = items
            self.items_changed(0, 0, len(items))
        else:
            self.items = items
            self.items_changed(0, old, len(items))
        if selected:
            self.select_recs(selected)
        self.emit("reset")

    # -- sélection ---------------------------------------------------------
    def selected_recs(self) -> list[Rec]:
        if not self.selection:
            return []
        bits = self.selection.get_selection()
        n = bits.get_size()
        if n == len(self.items) and n:
            return list(self.items)
        ok, it, pos = Gtk.BitsetIter.init_first(bits)
        out = []
        while ok:
            if pos < len(self.items):
                out.append(self.items[pos])
            ok, pos = it.next()
        return out

    def select_recs(self, recs: list[Rec]):
        if not self.selection:
            return
        bits = Gtk.Bitset.new_empty()
        ids = {id(r) for r in recs}
        names = {r.name for r in recs}
        for i, r in enumerate(self.items):
            if id(r) in ids or r.name in names:
                bits.add(i)
        mask = Gtk.Bitset.new_range(0, len(self.items)) if self.items else Gtk.Bitset.new_empty()
        self.selection.set_selection(bits, mask)

    # -- surveillance du dossier -------------------------------------------
    def _start_monitor(self, path: str):
        try:
            m = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        m.set_rate_limit(300)
        m.connect("changed", self._on_changed)
        self._monitor = m

    def _start_uri_monitor(self, uri: str):
        """Corbeille, réseau… : GVfs signale les changements (élément jeté, Freebox découverte sur le
        réseau quelques secondes après l'ouverture) ; on relit alors l'emplacement sans spinner."""
        try:
            m = Gio.File.new_for_uri(uri).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        m.set_rate_limit(500)
        m.connect("changed", self._on_uri_changed)
        self._monitor = m

    def _on_uri_changed(self, *_):
        if not self._pending_id:
            self._pending_id = GLib.timeout_add(400, self._reload_quiet)

    def reload_quiet(self):
        """Relit un emplacement GIO sans spinner ni nouvelle surveillance (la recréer fait protester
        GVfs : « MonitorClient.Changed… Object does not exist »)."""
        if not self._pending_id:
            self._pending_id = GLib.timeout_add(150, self._reload_quiet)

    def _reload_quiet(self):
        self._pending_id = 0
        location, gen, cancel = self.location, self._gen, self._cancel
        if not location or self.static or self.loading:
            return False

        def work():
            try:
                recs = scanner.scan_gio(Gio.File.new_for_uri(location), cancel)
            except Exception:
                return
            GLib.idle_add(self._reloaded, gen, recs)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _reloaded(self, gen, recs):
        if gen == self._gen:
            self.all = scanner.sort_recs(recs, self.sort_key, self.sort_reverse, self.folders_first)
            self._publish(keep_selection=True)
            self.emit("state", "ready")
        return False

    def _stop_monitor(self):
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        if self._pending_id:
            GLib.source_remove(self._pending_id)
            self._pending_id = 0
        self._pending.clear()

    def _on_changed(self, _m, f, other, event):
        E = Gio.FileMonitorEvent
        if event in (E.CHANGES_DONE_HINT, E.PRE_UNMOUNT):
            return
        if event == E.UNMOUNTED:
            return
        if f.get_path():
            self._pending[f.get_path()] = True
        if other is not None and other.get_path():
            self._pending[other.get_path()] = True
        if event in (E.DELETED, E.MOVED_OUT) and f.get_path() == self.location:
            # dossier affiché supprimé, jeté ou déplacé ailleurs : le panneau remonte (comme Nautilus)
            # au lieu d'afficher une erreur
            self.emit("gone")
            return
        if not self._pending_id:
            self._pending_id = GLib.timeout_add(150, self._flush_changes)

    def _flush_changes(self):
        self._pending_id = 0
        paths = list(self._pending)
        self._pending.clear()
        if not self.location or self.static:
            return False
        if len(paths) > 300:
            sel = self.selected_recs()
            loc = self.location
            names = [r.name for r in sel]
            self.load(loc)
            if names:
                self.connect_once_ready(lambda: self.select_names(names))
            return False
        by_name = {r.name: r for r in self.all}
        for p in paths:
            if os.path.dirname(p) != self.location:
                continue
            name = os.path.basename(p)
            old = by_name.get(name)
            new = scanner.rec_for_path(p)
            if old is not None and new is not None:
                self._update_rec(old, new)
            elif old is not None:
                self._remove_rec(old)
                del by_name[name]
            elif new is not None:
                self._insert_rec(new)
                by_name[name] = new
        return False

    def connect_once_ready(self, cb):
        holder = {}

        def on_state(_m, state):
            if state in ("ready", "error"):
                self.disconnect(holder["id"])
                if state == "ready":
                    cb()
        holder["id"] = self.connect("state", on_state)

    def select_names(self, names: list[str]):
        wanted = set(names)
        self.select_recs([r for r in self.items if r.name in wanted])

    def _sort_tuple(self, r: Rec):
        kf = scanner._key_func(self.sort_key)
        group = 0 if (self.folders_first and r.is_dir) else 1
        k = kf(r)
        return (group, _Rev(k) if self.sort_reverse else k)

    def _insert_rec(self, r: Rec):
        k = self._sort_tuple(r)
        i = bisect.bisect_right(self.all, k, key=self._sort_tuple)
        self.all.insert(i, r)
        if self._visible(r):
            j = bisect.bisect_right(self.items, k, key=self._sort_tuple)
            self.items.insert(j, r)
            self._index = None
            self.items_changed(j, 0, 1)

    def _remove_rec(self, r: Rec):
        try:
            self.all.remove(r)
        except ValueError:
            pass
        i = self.index_of(r)
        if i >= 0:
            del self.items[i]
            self._index = None
            self.items_changed(i, 1, 0)

    def _update_rec(self, old: Rec, new: Rec):
        # type changé (fichier vide remplacé par le vrai, ex. « x.zip » de Firefox écrasé par
        # « x.zip.part ») : l'icône suit ; trié par type, la ligne change de place
        if old.is_dir != new.is_dir or (old.ctype != new.ctype and self.sort_key == "type"):
            sel = old in self.selected_recs()
            self._remove_rec(old)
            self._insert_rec(new)
            if sel:
                self.select_recs(self.selected_recs() + [new])
            return
        old.ctype, old.is_link = new.ctype, new.is_link
        old.size, old.mtime, old.mode, old.uid, old.gid = new.size, new.mtime, new.mode, new.uid, new.gid
        old.stat_ok = True
        if old.extra:
            for k in ("thumb", "thumb_fail", "duration", "count", "meta"):
                old.extra.pop(k, None)
        self.notify_rec(old)


def _is_uri(location: str) -> bool:
    return "://" in location and not location.startswith("/")


class Selection(GObject.Object, Gio.ListModel, Gtk.SelectionModel):
    """Sélection multiple par simple ensemble de bits.

    Gtk.MultiSelection garde chaque élément sélectionné dans une table : « tout sélectionner »
    dans un dossier de 100 000 fichiers créait 100 000 objets (2 s). Ici, seules des positions
    sont mémorisées.
    """
    __gtype_name__ = "OdysseusSelection"

    def __init__(self, model: DirModel):
        super().__init__()
        self.model = model
        self.bits = Gtk.Bitset.new_empty()
        model.connect("items-changed", self._on_items_changed)
        # clic simple sur un élément déjà sélectionné parmi d'autres : comme Nautilus, la sélection
        # n'est réduite à lui qu'une fois sûr que ce n'est pas un double-clic (qui ouvre TOUTE la
        # sélection) ni le début d'un glisser. Piloté par Pane (clic) et la vue (glisser).
        self.clicking = False       # clic gauche simple en cours (GTK 4.14 sélectionne au relâchement)
        self.activated = False      # ce clic a ouvert la sélection (double-clic)
        self._pending = None
        self._pending_timer = 0

    def finish_click(self, delay_ms: int):
        """Bouton relâché : réduire la sélection différée, sauf si un 2e clic arrive avant."""
        if self._pending is not None and not self._pending_timer:
            self._pending_timer = GLib.timeout_add(delay_ms, self._collapse)

    def cancel_pending(self):
        """Double-clic (ouverture de la sélection) ou glisser : la sélection reste entière."""
        self._pending = None
        if self._pending_timer:
            GLib.source_remove(self._pending_timer)
            self._pending_timer = 0

    def _collapse(self):
        self._pending_timer = 0
        pos, self._pending = self._pending, None
        if pos is not None and pos < self.model.get_n_items():
            self.do_set_selection(Gtk.Bitset.new_range(pos, 1), Gtk.Bitset.new_range(0, self.model.get_n_items()))
        return False

    # -- Gio.ListModel -----------------------------------------------------
    def do_get_item_type(self):
        return Entry.__gtype__

    def do_get_n_items(self):
        return self.model.get_n_items()

    def do_get_item(self, i):
        return self.model.get_item(i)

    def _on_items_changed(self, _m, pos, removed, added):
        self.bits.splice(pos, removed, added)
        self.items_changed(pos, removed, added)

    # -- Gtk.SelectionModel ------------------------------------------------
    def do_is_selected(self, pos):
        return self.bits.contains(pos)

    def do_get_selection_in_range(self, pos, n):
        out = self.bits.copy()
        if n:
            out.intersect(Gtk.Bitset.new_range(pos, n))
        return out

    def do_set_selection(self, selected, mask):
        if self.clicking and selected.get_size() == 1 and self.bits.get_size() > 1 \
                and mask.get_size() >= self.model.get_n_items() and self.bits.contains(selected.get_minimum()):
            # « lui seul » sur un élément déjà sélectionné parmi d'autres : différé (voir __init__)
            if self._pending_timer:
                GLib.source_remove(self._pending_timer)
                self._pending_timer = 0
            self._pending = None if self.activated else selected.get_minimum()
            return True
        self.cancel_pending()
        # attention : Gtk.Bitset.difference() est la différence symétrique, subtract() la différence
        new = self.bits.copy()
        new.subtract(mask)
        sel = selected.copy()
        sel.intersect(mask)
        new.union(sel)
        changed = new.copy()
        changed.difference(self.bits)
        self.bits = new
        if not changed.is_empty():
            first = changed.get_minimum()
            self.selection_changed(first, changed.get_maximum() - first + 1)
        return True

    def do_select_item(self, pos, unselect_rest):
        n = self.model.get_n_items()
        mask = Gtk.Bitset.new_range(0, n) if unselect_rest else Gtk.Bitset.new_range(pos, 1)
        return self.do_set_selection(Gtk.Bitset.new_range(pos, 1), mask)

    def do_unselect_item(self, pos):
        return self.do_set_selection(Gtk.Bitset.new_empty(), Gtk.Bitset.new_range(pos, 1))

    def do_select_range(self, pos, n, unselect_rest):
        total = self.model.get_n_items()
        mask = Gtk.Bitset.new_range(0, total) if unselect_rest else Gtk.Bitset.new_range(pos, n)
        return self.do_set_selection(Gtk.Bitset.new_range(pos, n), mask)

    def do_unselect_range(self, pos, n):
        return self.do_set_selection(Gtk.Bitset.new_empty(), Gtk.Bitset.new_range(pos, n))

    def do_select_all(self):
        n = self.model.get_n_items()
        return self.do_set_selection(Gtk.Bitset.new_range(0, n), Gtk.Bitset.new_range(0, n))

    def do_unselect_all(self):
        n = self.model.get_n_items()
        return self.do_set_selection(Gtk.Bitset.new_empty(), Gtk.Bitset.new_range(0, n))
