"""Panneau de fichiers : un emplacement affiché (dossier, archive, corbeille, résultats de recherche).

Un onglet contient un panneau, ou deux en mode double panneau. Chaque panneau a son historique,
son mode d'affichage (liste, grille, colonnes) et son modèle virtuel.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import features
from ..backend import archive, formats, scanner, trashwatch
from ..backend.engine import PasswordRequired, human_size
from ..backend.scanner import Rec
from ..i18n import ntr, tr
from ..settings import session_passwords, settings
from . import clipboard
from .common import focus_later, shown
from .columns import MillerView
from .model import DirModel, Selection, fold
from .other_locations import OTHER_URI, OtherLocations
from .views import DRAG_PATHS, GRID_ZOOM, ZOOM, GridView, ListView

TRASH_URI = "trash:///"
SPECIAL_URIS = {"trash:///": tr("Corbeille"), "recent:///": tr("Récents"), "network:///": tr("Réseau"),
                OTHER_URI: tr("Autres emplacements")}
SEARCH_LIMIT = 20000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", ".tox"}


def is_uri(loc: str) -> bool:
    return "://" in loc and not loc.startswith("/")


def display_location(loc: str) -> str:
    if loc.startswith("file://"):
        return Gio.File.new_for_uri(loc).get_path() or loc
    return loc


class Pane(Gtk.Box):
    __gtype_name__ = "OdysseusPane"
    __gsignals__ = {
        "location-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "context-menu": (GObject.SignalFlags.RUN_FIRST, None, (float, float, bool)),
        "open-in-tab": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "focused": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "files-dropped": (GObject.SignalFlags.RUN_FIRST, None, (object, str, str)),
        "message": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, location: Optional[str] = None, view_mode: Optional[str] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["file-pane"])
        self.mode = "fs"              # fs, archive, search, trash, remote, other
        self.location = os.path.expanduser("~")
        self.view_mode = view_mode or settings["view_mode"]
        if self.view_mode == "columns" and not features.enabled("column_view"):
            self.view_mode = "list"
        self.back_stack: list[tuple] = []
        self.forward_stack: list[tuple] = []
        # archive ouverte
        self.archive_path = ""
        self.inner = ""
        self.password = ""
        self.listing: Optional[archive.ArchiveListing] = None
        # recherche
        self.search_root = ""
        self.search_query = ""
        self._search_cancel: Optional[threading.Event] = None
        self._select_after: Optional[list] = None
        self._load_token = 0

        self.model = DirModel()
        self.selection = Selection(self.model)
        self.model.selection = self.selection
        self.selection.connect("selection-changed", lambda *_: self._on_selection())
        self.model.connect("state", self._on_state)
        self.model.connect("reset", lambda *_: self._on_reset())
        self._empty_check_id = 0
        self.model.connect("items-changed", self._on_items_changed)
        self.model.connect("gone", lambda *_: GLib.idle_add(self._on_gone))

        self.top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)     # barre de chemin du double panneau
        self.append(self.top)
        self._build_strip()
        self._build_trash_bar()
        self._build_body()
        self._setup_drop()
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.emit("focused"))
        self.add_controller(focus)
        click = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: (self.emit("focused"), self._check_location()))
        self.add_controller(click)
        self._settings_cb = self._on_setting
        settings.connect(self._settings_cb)
        self._clip_id = clipboard.state.connect("changed", lambda *_: self.refresh_rows())
        self.connect("destroy", lambda *_: self._cleanup())
        self.open(location or os.path.expanduser("~"), push=False)

    def _cleanup(self):
        if getattr(self, "_retired", False):
            return
        self._retired = True
        try:
            settings.disconnect(self._settings_cb)
        except (TypeError, ValueError):
            pass
        try:
            clipboard.state.disconnect(self._clip_id)
        except TypeError:
            pass
        self.model.cancel()

    def retire(self):
        """Panneau retiré de l'écran (sortie du double panneau, onglet fermé). En GTK 4, « destroy »
        n'arrive pas tant que Python garde une référence : sans ce ménage, réglages, presse-papiers
        et modèle continuaient d'agir sur des vues hors de toute fenêtre (lignes créées, colonnes
        montrées / cachées), ce qui corrompait GTK (segfault à la sortie du double panneau)."""
        self._cleanup()
        for view in (self.list_view.view, self.grid_view.view):
            if view.get_model() is not None:
                view.set_model(None)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_strip(self):
        """Bandeau d'archive (repris de Pressoir), sur deux lignes pour tenir dans un demi-écran."""
        self.strip = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=14, css_classes=["archive-strip"])
        box.append(Gtk.Image(icon_name="package-x-generic", pixel_size=40, valign=Gtk.Align.CENTER))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        top = Gtk.Box(spacing=8)
        self.strip_title = Gtk.Label(xalign=0, css_classes=["title-4"], ellipsize=3, hexpand=True)
        self.strip_type = Gtk.Label(css_classes=["badge"], valign=Gtk.Align.CENTER)
        self.strip_lock = Gtk.Label(label=tr("🔒 chiffrée"), css_classes=["badge", "badge-lock"], visible=False,
                                    valign=Gtk.Align.CENTER)
        for w in (self.strip_title, self.strip_type, self.strip_lock):
            top.append(w)
        top.append(Gtk.Button(icon_name="window-close-symbolic", tooltip_text=tr("Fermer l'archive"),
                              action_name="win.close-archive", css_classes=["circular", "flat"]))
        col.append(top)
        bottom = Gtk.Box(spacing=6)
        self.strip_details = Gtk.Label(xalign=0, css_classes=["dim-label"], ellipsize=3, hexpand=True)
        bottom.append(self.strip_details)
        for icon, tip, action in (("dialog-information-symbolic", tr("Propriétés de l'archive"), "win.archive-info"),
                                  ("emblem-ok-symbolic", tr("Tester l'intégrité"), "win.archive-test"),
                                  ("list-add-symbolic", tr("Ajouter des fichiers…"), "win.archive-add")):
            bottom.append(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action,
                                     css_classes=["circular", "flat"]))
        extract = Gtk.Button(css_classes=["pill", "suggested-action", "small-pill"], action_name="win.extract-all")
        extract.set_child(Adw.ButtonContent(icon_name="folder-download-symbolic", label=tr("Tout extraire")))
        bottom.append(extract)
        col.append(bottom)
        box.append(col)
        self.strip.set_child(box)
        self.append(self.strip)

    def _build_trash_bar(self):
        self.trash_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=8, css_classes=["trash-bar"])
        box.append(Gtk.Label(label=tr("Corbeille"), xalign=0, hexpand=True, css_classes=["heading"]))
        self.restore_btn = Gtk.Button(label=tr("Restaurer"), action_name="win.restore")
        box.append(self.restore_btn)
        box.append(Gtk.Button(label=tr("Vider la corbeille…"), action_name="win.empty-trash",
                              css_classes=["destructive-action"]))
        self.trash_bar.set_child(box)
        self.append(self.trash_bar)

    def _build_body(self):
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120,
                               vexpand=True, hexpand=True)
        self.list_view = ListView(self)
        self.grid_view = GridView(self)
        for v in (self.list_view, self.grid_view):
            # Ctrl + molette : zoom, comme Nautilus. En capture, avant le défilement de la vue.
            zc = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL,
                                           propagation_phase=Gtk.PropagationPhase.CAPTURE)
            zc.connect("scroll", self._on_zoom_scroll)
            v.add_controller(zc)
        self._zoom_acc = 0.0
        self.miller: Optional[MillerView] = None
        self.other_page: Optional[OtherLocations] = None
        self.stack.add_named(self.list_view, "list")
        self.stack.add_named(self.grid_view, "grid")
        self.empty = Adw.StatusPage(icon_name="folder-symbolic", title=tr("Dossier vide"), css_classes=["compact"])
        self.stack.add_named(self.empty, "empty")
        spin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                       halign=Gtk.Align.CENTER)
        self.spinner = Gtk.Spinner(width_request=32, height_request=32)
        spin.append(self.spinner)
        self.loading_label = Gtk.Label(label=tr("Chargement…"), css_classes=["dim-label"])
        spin.append(self.loading_label)
        self.stack.add_named(spin, "loading")
        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic", title=tr("Impossible d'ouvrir"))
        self.stack.add_named(self.error_page, "error")
        # archive protégée : déverrouillage intégré (Pressoir)
        self.lock_page = Adw.StatusPage(icon_name="channel-secure-symbolic", title=tr("Archive protégée"),
                                        description=tr("Les noms de fichiers sont chiffrés. Saisissez le mot de passe."))
        lock_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                           width_request=320)
        lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.lock_entry = Adw.PasswordEntryRow(title=tr("Mot de passe"))
        self.lock_entry.connect("entry-activated", lambda *_: self._unlock())
        lb.append(self.lock_entry)
        lock_box.append(lb)
        ub = Gtk.Button(label=tr("Déverrouiller"), css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        ub.connect("clicked", lambda *_: self._unlock())
        lock_box.append(ub)
        self.lock_page.set_child(lock_box)
        self.stack.add_named(self.lock_page, "locked")

        self.overlay = Gtk.Overlay(vexpand=True)
        self.overlay.set_child(self.stack)
        self.floating = Gtk.Label(css_classes=["floating-bar"], halign=Gtk.Align.END, valign=Gtk.Align.END,
                                  visible=False, ellipsize=3, max_width_chars=90)
        self.floating.set_can_target(False)
        self.overlay.add_overlay(self.floating)
        # comme Nautilus : la barre passe de l'autre côté quand le pointeur arrive dessus, pour ne
        # pas cacher la ligne du dessous
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._dodge_floating)
        self.overlay.add_controller(motion)
        self.append(self.overlay)

    def _setup_drop(self):
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        drop.connect("enter", lambda t, x, y: (self.add_css_class("drop-hover"),
                                               self.preferred_action(t.get_current_drop(), None))[1])
        drop.connect("motion", lambda t, x, y: self.preferred_action(t.get_current_drop(), None))
        drop.connect("leave", lambda *_: self.remove_css_class("drop-hover"))

        def on_drop(t, value, x, y):
            self.remove_css_class("drop-hover")
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if not paths:
                return False
            if self.mode == "archive":
                self.emit("files-dropped", paths, "", "archive")
                return True
            if self.mode != "fs":
                return False
            self.handle_drop(paths, self.location, t.get_current_drop())
            return True
        drop.connect("drop", on_drop)
        self.overlay.add_controller(drop)

    # ------------------------------------------------------------------
    # Glisser-déposer
    # ------------------------------------------------------------------
    def drag_paths(self) -> list[str]:
        if self.mode == "archive":
            return self._extract_for_drag()
        return [r.path for r in self.selected_recs() if r.path]

    def _extract_for_drag(self) -> list[str]:
        """Glisser hors d'une archive (comme Archive Browser) : extraction rapide dans un dossier
        temporaire, puis glisser les fichiers extraits. Limité aux sélections raisonnables."""
        recs = self.selected_recs()
        if not recs or sum(r.size for r in recs) > 300 * 1024 * 1024:
            return []
        inner = [r.get("inner", r.name) for r in recs]
        from ..backend.engine import Job
        job = Job("drag", lambda j: archive.extract_to_temp(j, self.archive_path, inner, self.password)).run_sync()
        if job.state != "done" or not job.result:
            return []
        # éléments posés sans leur chemin dans l'archive (archive._extract_selected)
        out = [os.path.join(job.result, os.path.basename(i.rstrip("/"))) for i in inner]
        return [p for p in out if os.path.lexists(p)]

    def _decide_action(self, drop: Optional[Gdk.Drop], dest: str, paths: Optional[list] = None) -> str:
        """Comme Nautilus : déplacer sur le même disque, copier ailleurs ; Ctrl copie, Maj déplace."""
        acts = drop.get_actions() if drop is not None else Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        if acts == Gdk.DragAction.COPY:
            return "copy"
        if acts == Gdk.DragAction.MOVE:
            return "move"
        if acts == Gdk.DragAction.LINK:
            return "link"
        if paths:
            try:
                dev = os.stat(dest).st_dev
                if all(os.lstat(p).st_dev == dev for p in paths[:20]):
                    return "move"
            except OSError:
                pass
            return "copy"
        return "move"

    def preferred_action(self, drop: Optional[Gdk.Drop], rec: Optional[Rec]) -> Gdk.DragAction:
        if self.mode == "archive":
            return Gdk.DragAction.COPY
        if self.mode != "fs" and rec is None:
            return 0
        dest = rec.path if rec is not None else self.location
        # Les drapeaux GTK ne supportent pas l'arithmétique en Python (acts - 1 vaut None) : en entier.
        acts = int(drop.get_actions()) if drop is not None else 0
        if acts and not (acts & (acts - 1)):     # une seule action possible (touche enfoncée)
            return Gdk.DragAction(acts)
        # comme Nautilus : déplacer sur le même disque, copier ailleurs ; glisser des éléments dans
        # leur propre dossier n'a pas de sens
        src = DRAG_PATHS if drop is not None and drop.get_drag() is not None else []
        if src and all(os.path.dirname(p) == dest for p in src):
            return 0
        try:
            same = os.lstat(src[0]).st_dev == os.stat(dest).st_dev if src else True
        except OSError:
            same = True
        return Gdk.DragAction.MOVE if same else Gdk.DragAction.COPY

    def clear_drop_highlight(self):
        """Retire le cadre de dépôt de tous les panneaux : déposé sur une ligne, le panneau ne reçoit
        pas toujours « leave » et le cadre restait affiché."""
        root = self.get_root()
        for p in (root.all_panes() if hasattr(root, "all_panes") else [self]):
            p.remove_css_class("drop-hover")

    def handle_drop(self, paths: list[str], dest: str, drop: Optional[Gdk.Drop]):
        self.clear_drop_highlight()
        self.emit("files-dropped", paths, dest, self._decide_action(drop, dest, paths))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _current_entry(self) -> tuple:
        if self.mode == "archive":
            return ("archive", self.archive_path, self.inner)
        if self.mode == "search":
            return ("search", self.search_root, self.search_query)
        return ("loc", self.location)

    def _push(self):
        cur = self._current_entry()
        if not self.back_stack or self.back_stack[-1] != cur:
            self.back_stack.append(cur)
            del self.back_stack[:-100]
        self.forward_stack.clear()

    def _go(self, e: tuple):
        if e[0] == "archive":
            if self.mode == "archive" and self.archive_path == e[1] and self.listing:
                self.open_inner(e[2], push=False)
            else:
                self.open_archive(e[1], e[2], push=False)
        elif e[0] == "search":
            # revenir à une recherche la relance (avant : dossier racine non filtré, alors que le champ
            # affichait encore la recherche)
            self.open(e[1], push=False)
            if e[2]:
                self.start_search(e[2], recursive=True, push=False)
        else:
            self.open(e[1], push=False)

    def go_back(self):
        if self.back_stack:
            self.forward_stack.append(self._current_entry())
            self._go(self.back_stack.pop())

    def go_forward(self):
        if self.forward_stack:
            self.back_stack.append(self._current_entry())
            self._go(self.forward_stack.pop())

    def parent_location(self) -> Optional[str]:
        if self.mode == "archive":
            return None
        if is_uri(self.location):
            parent = Gio.File.new_for_uri(self.location).get_parent()
            return parent.get_uri() if parent else None
        p = os.path.dirname(self.location.rstrip("/")) or "/"
        return None if p == self.location else p

    def can_go_up(self) -> bool:
        return self.mode == "archive" or self.parent_location() is not None

    def go_up(self):
        if self.mode == "archive":
            if self.inner:
                child = self.inner.rsplit("/", 1)[-1]
                self.open_inner(self.inner.rsplit("/", 1)[0] if "/" in self.inner else "")
                self.select_names([child])
            else:
                name = os.path.basename(self.archive_path)
                self.open(os.path.dirname(self.archive_path), select=[name])
            return
        if self.mode == "search":
            self.open(self.search_root)
            return
        parent = self.parent_location()
        if parent:
            child = os.path.basename(self.location.rstrip("/")) if not is_uri(self.location) else None
            self.open(parent, select=[child] if child else None)

    def refresh(self):
        if self.mode == "other":
            self.other_page.refresh()
        elif self.mode == "archive":
            self.open_archive(self.archive_path, self.inner, push=False, password=self.password)
        elif self.mode == "search":
            self.start_search(self.search_query, recursive=True)
        else:
            sel = [r.name for r in self.selected_recs()]
            self._select_after = sel
            if self.miller is not None and self.view_mode == "columns" and self.mode == "fs":
                self.miller.refresh()
            else:
                self.model.load(self.location)

    def open(self, location: str, push: bool = True, select: Optional[list] = None) -> bool:
        """Ouvre un emplacement : dossier, URI GIO, archive, ou fichier (son dossier, fichier sélectionné)."""
        loc = os.path.expanduser(location.strip())
        if loc.startswith("file://"):
            loc = Gio.File.new_for_uri(loc).get_path() or loc
        if loc == OTHER_URI:
            self._open_other(push)
            return True
        if is_uri(loc):
            self._open_uri(loc, push, select)
            return True
        loc = os.path.abspath(loc)
        if os.path.isdir(loc):
            self._open_dir(loc, push, select)
            return True
        if os.path.isfile(loc):
            if features.enabled("archives") and formats.looks_like_archive(os.path.basename(loc)):
                self.open_archive(loc, push=push)
            else:
                self._open_dir(os.path.dirname(loc), push, [os.path.basename(loc)])
            return True
        # chemin à l'intérieur d'une archive : /chemin/archive.7z/dossier
        probe = loc
        while probe and probe != "/":
            if os.path.isfile(probe) and formats.is_archive(probe) and features.enabled("archives"):
                self.open_archive(probe, loc[len(probe):].strip("/"), push=push)
                return True
            probe = os.path.dirname(probe)
        self.emit("message", tr("Emplacement introuvable : {0}").format(loc))
        return False

    def _leave_special(self):
        if self.other_page is not None:
            self.other_page.stop()
        self._stop_search()
        self.strip.set_reveal_child(False)
        self.trash_bar.set_reveal_child(False)
        self.listing = None
        self.archive_path = ""
        self.password = ""

    def _open_dir(self, path: str, push: bool, select: Optional[list]):
        if push and (self.mode != "fs" or self.location != path):
            self._push()
        self._leave_special()
        self.mode = "fs"
        self.location = path
        self._select_after = select
        self._to_top = True              # nouvel emplacement : affiché depuis le début
        self.model.show_hidden = settings["show_hidden"]     # cachés montrés pour un fichier choisi : pas ailleurs
        self.list_view.update_columns()
        self._load_token += 1
        if self.view_mode == "columns":
            # la vue en colonnes a ses propres modèles : ne pas relire le même dossier deux fois
            self.model.cancel()
            self.model.location = path
            self._ensure_miller().open(path)
        else:
            self.model.load(path)
        self._show_content()
        self.emit("location-changed")

    def _open_other(self, push: bool):
        """Page « Autres emplacements » (disques, réseau, connexion à un serveur)."""
        if push and self.location != OTHER_URI:
            self._push()
        self._leave_special()
        self.mode = "other"
        self.location = OTHER_URI
        if self.other_page is None:       # avant set_static, qui affiche déjà la page
            self.other_page = OtherLocations(self)
            self.stack.add_named(self.other_page, "other")
        self.model.set_static(OTHER_URI, [])
        self.other_page.refresh()
        self._show_content()
        self._update_floating()
        self.emit("location-changed")

    def _open_uri(self, uri: str, push: bool, select: Optional[list]):
        if push and self.location != uri:
            self._push()
        self._leave_special()
        self.mode = "trash" if uri.startswith("trash:") else "remote"
        self.location = uri
        self._select_after = select
        self._to_top = True              # nouvel emplacement : affiché depuis le début
        self.trash_bar.set_reveal_child(False)     # affichée une fois la corbeille lue, si non vide
        self.list_view.update_columns()
        if self.view_mode == "columns":
            self.set_view_mode("list", save=False)
        self.model.load(uri)
        self.emit("location-changed")

    def mount_and_retry(self, window: Gtk.Window):
        """Emplacement réseau pas encore monté : le monter (mot de passe demandé), puis recharger."""
        f = Gio.File.new_for_uri(self.location)
        op = Gtk.MountOperation.new(window)

        def done(src, res):
            try:
                src.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                self.model.set_error(e.message)
                return
            self.model.load(self.location)
        f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, done)

    # ------------------------------------------------------------------
    # Archives (moteur Pressoir)
    # ------------------------------------------------------------------
    def open_archive(self, path: str, inner: str = "", push: bool = True, password: Optional[str] = None):
        path = os.path.abspath(path)
        if push:
            self._push()
        self._leave_special()
        if password is None:
            password = session_passwords.get(path, "")
        self.mode = "archive"
        self.archive_path = path
        self.location = os.path.dirname(path)
        self.inner = inner.strip("/")
        self.password = password or ""
        self.model.clear()
        self.list_view.update_columns()
        if self.view_mode == "columns":
            self.set_view_mode("list", save=False)
        self.strip_title.set_text(os.path.basename(path))
        _b, ext = formats.split_archive_name(os.path.basename(path))
        self.strip_type.set_text((ext or "?").upper())
        self.strip_details.set_text(tr("Lecture…"))
        self.strip_lock.set_visible(False)
        self.strip.set_reveal_child(True)
        self.loading_label.set_text(tr("Lecture de l'archive…"))
        self.stack.set_visible_child_name("loading")
        self.spinner.start()
        self.emit("location-changed")
        self._load_token += 1
        token = self._load_token
        pw = self.password

        def work():
            try:
                res, err = archive.list_archive(path, pw), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            GLib.idle_add(self._archive_loaded, token, res, err)
        threading.Thread(target=work, daemon=True).start()

    def _archive_loaded(self, token, listing, err):
        if token != self._load_token or self.mode != "archive":
            return False
        self.spinner.stop()
        if isinstance(err, PasswordRequired):
            self.lock_page.set_description(tr("Les noms de fichiers sont chiffrés. Saisissez le mot de passe.")
                                           if not self.password else tr("Mot de passe incorrect, réessayez."))
            self.lock_entry.set_text("")
            self.strip_details.set_text(tr("Contenu verrouillé"))
            self.strip_lock.set_visible(True)
            self.stack.set_visible_child_name("locked")
            focus_later(self.lock_entry)
            self.emit("location-changed")
            return False
        if err is not None:
            self.strip_details.set_text(tr("Illisible"))
            self.error_page.set_title(tr("Archive illisible"))
            self.error_page.set_description(GLib.markup_escape_text(str(err)))
            self.stack.set_visible_child_name("error")
            self.emit("location-changed")
            return False
        self.listing = listing
        if self.password and settings["remember_passwords"]:
            session_passwords[self.archive_path] = self.password
        if self.inner and not listing.get(self.inner):
            self.inner = ""
        self._update_strip()
        self.open_inner(self.inner, push=False)
        return False

    def _unlock(self):
        pw = self.lock_entry.get_text()
        if pw:
            self.open_archive(self.archive_path, self.inner, push=False, password=pw)

    def open_inner(self, inner: str, push: bool = True):
        if not self.listing:
            return
        if push:
            self._push()
        self.inner = inner.strip("/")
        recs = []
        for e in self.listing.children(self.inner):
            r = Rec(e.name, None, e.is_dir)
            r.hidden = False
            r.key = scanner.sort_name(e.name)
            r.size = e.size or 0
            r.mtime = e.modified.timestamp() if e.modified else 0
            r.stat_ok = True
            r.set("inner", e.path)
            if e.packed is not None:
                r.set("packed", e.packed)
            if e.encrypted:
                r.set("encrypted", True)
                r.set("meta", {"emblems": ["channel-secure-symbolic"]})
            if e.is_dir:
                r.set("children", len(self.listing.children(e.path)))
            recs.append(r)
        self.model.set_static(f"{self.archive_path}/{self.inner}", recs)
        self._show_content()
        self.emit("location-changed")

    def _update_strip(self):
        lst = self.listing
        phys = os.path.getsize(self.archive_path) if os.path.exists(self.archive_path) else 0
        total = lst.total_size
        typ = lst.info.get("Type", "")
        if typ:
            self.strip_type.set_text(typ.upper())
        parts = [ntr("{0} fichier", "{0} fichiers", lst.file_count).format(lst.file_count),
                 ntr("{0} dossier", "{0} dossiers", lst.dir_count).format(lst.dir_count), human_size(phys)]
        if total:
            parts.append(tr("{0} décompressés").format(human_size(total)))
            parts.append(tr("ratio {0} %").format(f"{100 * phys / total:.0f}"))
        self.strip_details.set_text("  ·  ".join(parts))
        self.strip_details.set_tooltip_text("\n".join(parts))
        self.strip_lock.set_visible(lst.encrypted)

    # ------------------------------------------------------------------
    # Recherche
    # ------------------------------------------------------------------
    def set_filter(self, text: str):
        if self.view_mode == "columns" and self.miller is not None and self.mode == "fs":
            col = self.miller.focused or (self.miller.columns[-1] if self.miller.columns else None)
            for c in self.miller.columns:
                c.model.set_filter(text if c is col else "")
            return
        self.model.set_filter(text)
        self._update_state()

    def start_search(self, query: str, recursive: bool, push: bool = True):
        """Filtre instantané du dossier, ou recherche récursive des noms dans les sous-dossiers."""
        query = query.strip()
        if not recursive or self.mode not in ("fs", "search") or is_uri(self.location if self.mode == "fs"
                                                                         else self.search_root):
            if self.mode == "search":
                self.open(self.search_root, push=False)
            self.set_filter(query)
            return
        if not query:
            if self.mode == "search":
                self.open(self.search_root, push=False)
            return
        root = self.search_root if self.mode == "search" else self.location
        if self.mode != "search" and push:
            self._push()
        self._stop_search()
        self.mode = "search"
        self.search_root = root
        self.search_query = query
        self.model.filter_text = ""
        self.model.set_static(root, [])
        self.list_view.update_columns()
        if self.view_mode == "columns":
            self.set_view_mode("list", save=False)
        cancel = threading.Event()
        self._search_cancel = cancel
        needle = fold(query)
        show_hidden = settings["show_hidden"]
        batch: list = []
        lock = threading.Lock()
        state = {"done": False, "count": 0}

        def work():
            for dirpath, dirnames, filenames in os.walk(root):
                if cancel.is_set():
                    return
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (show_hidden or not d.startswith("."))]
                rel = os.path.relpath(dirpath, root)
                rel = "" if rel == "." else rel
                for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
                    if not show_hidden and name.startswith("."):
                        continue
                    if needle in fold(name):
                        r = scanner.rec_for_path(os.path.join(dirpath, name))
                        if r is None:
                            continue
                        r.set("location", rel or os.path.basename(root) or root)
                        with lock:
                            batch.append(r)
                        state["count"] += 1
                        if state["count"] >= SEARCH_LIMIT:
                            state["done"] = True
                            return
            state["done"] = True

        def pump():
            if cancel.is_set():
                return False
            with lock:
                items = batch[:]
                batch.clear()
            self.model.append_static(items)
            self._update_state()
            if state["done"]:
                self.model.finish_static()
                self.spinner.stop()
                self._update_state(final=True)
                return False
            return True
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add(120, pump)
        self._show_content()
        self.emit("location-changed")

    def _stop_search(self):
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None

    # ------------------------------------------------------------------
    # Vues
    # ------------------------------------------------------------------
    def _ensure_miller(self) -> MillerView:
        if self.miller is None:
            self.miller = MillerView(self)
            self.stack.add_named(self.miller, "columns")
        return self.miller

    def set_view_mode(self, mode: str, save: bool = True):
        if mode == "columns" and (not features.enabled("column_view") or self.mode != "fs"):
            mode = "list" if mode == "columns" and self.mode != "fs" else mode
            if not features.enabled("column_view"):
                mode = "list"
        self.view_mode = mode
        if save:
            settings["view_mode"] = mode
        if mode == "columns":
            self._ensure_miller().open(self.location)
        elif self.mode == "fs" and (self.model.location != self.location or self.model.static
                                    or (not self.model.all and not self.model.loading)):
            self.model.load(self.location)
        self._show_content()
        GLib.idle_add(self.focus_view)
        self.emit("location-changed")

    @property
    def active_view(self):
        if self.view_mode == "columns" and self.miller is not None and self.mode == "fs":
            return self.miller
        return self.grid_view if self.view_mode == "grid" else self.list_view

    @property
    def list_zoom(self) -> tuple:
        return ZOOM.get(settings["list_zoom"], ZOOM["small"])

    @property
    def grid_icon_size(self) -> int:
        return GRID_ZOOM.get(settings["grid_zoom"], GRID_ZOOM["medium"])

    def columns_location(self, path: str):
        """La vue en colonnes a changé de dossier courant (sans reconstruire la chaîne)."""
        if path != self.location:
            self._push()
            self.location = path
            self.model.location = path
            self.emit("location-changed")

    def _attach_views(self):
        """Seule la vue affichée reçoit le modèle : une vue cachée construirait sinon des milliers
        de cellules pour rien (mesuré : 2 000 cases de grille et 200 000 objets demandés)."""
        if getattr(self, "_retired", False):
            return
        want = None if (self.view_mode == "columns" and self.mode == "fs") else self.view_mode
        target = self.selection if want == "list" else None
        if self.list_view.view.get_model() is not target:
            self.list_view.view.set_model(target)
        self.grid_view.attach(self.selection if want == "grid" else None)

    def _show_content(self):
        if self.mode == "other":
            self.stack.set_visible_child_name("other")
            return
        self._attach_views()
        if self.stack.get_visible_child_name() == "locked" and self.mode == "archive" and not self.listing:
            return
        name = "grid" if self.view_mode == "grid" else "list"
        if self.view_mode == "columns" and self.mode == "fs":
            self.stack.set_visible_child_name("columns")
            return
        if self.model.error:
            self.stack.set_visible_child_name("error")
        elif not self.model.items and not self.model.loading and self.mode != "search":
            self._fill_empty()
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name(name)

    def _fill_empty(self):
        if self.model.filter_text:
            self.empty.set_icon_name("edit-find-symbolic")
            self.empty.set_title(tr("Aucun résultat"))
            self.empty.set_description(tr("Rien ne correspond à « {0} »").format(self.model.filter_text))
        elif self.mode == "trash":
            self.empty.set_icon_name("user-trash-symbolic")
            self.empty.set_title(tr("La corbeille est vide"))
            self.empty.set_description("")
        elif self.mode == "archive":
            self.empty.set_icon_name("package-x-generic-symbolic")
            self.empty.set_title(tr("Dossier vide"))
            self.empty.set_description(tr("Déposez des fichiers ici pour les ajouter à l'archive."))
        else:
            self.empty.set_icon_name("folder-symbolic")
            self.empty.set_title(tr("Dossier vide"))
            self.empty.set_description("")

    def _on_state(self, _m, state):
        if state == "loading":
            if self.view_mode != "columns" or self.mode != "fs":
                self.loading_label.set_text(tr("Chargement…"))
                self.stack.set_visible_child_name("loading")
                self.spinner.start()
            return
        self.spinner.stop()
        if state == "error":
            if self.model.error_code == Gio.IOErrorEnum.NOT_MOUNTED and self.mode == "remote":
                win = self.get_root()
                if isinstance(win, Gtk.Window):
                    self.mount_and_retry(win)
                    return
            self.error_page.set_title(tr("Accès impossible"))
            self.error_page.set_description(GLib.markup_escape_text(self.model.error or ""))
            self.stack.set_visible_child_name("error")
            return
        self._show_content()
        if self._select_after:
            names, self._select_after = self._select_after, None
            self.select_names(names)
        elif getattr(self, "_to_top", False) and state == "ready":
            self._scroll_top()
        if state == "ready":
            self._to_top = False
        self._update_state()
        if self.mode == "trash" and state == "ready":
            self._check_trash()

    def _check_trash(self):
        """Bug de GVfs : gvfsd-trash finit par ne plus voir les corbeilles des autres disques. On compte
        nous-mêmes (dans un fil : un disque peut être lent) ; s'il en manque, relance et relecture."""
        shown = len(self.model.all)

        def work():
            expected = trashwatch.expected_count()
            if expected > shown and trashwatch.restart_gvfs_trash():
                GLib.timeout_add(1500, lambda: (self.mode == "trash" and not getattr(self, "_retired", False)
                                                and self.refresh(), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _on_items_changed(self, _m, _pos, removed, added):
        """Fichier arrivé dans un dossier vide (copie, surveillance) ou dernier fichier supprimé :
        passer de la page « Dossier vide » à la liste, et inversement. Jamais PENDANT le signal :
        _show_content rebranche la vue sur le modèle, et GTK (GtkListItemManager) est alors au milieu
        de son propre traitement de items-changed — l'état devient incohérent et GTK plante plus
        tard (segfault vu après un glisser vers un panneau vide)."""
        if not (removed or added) or self._empty_check_id:
            return
        self._empty_check_id = GLib.idle_add(self._check_empty_page)

    def _on_gone(self):
        """Dossier affiché supprimé, jeté ou déplacé : remonter au plus proche parent existant."""
        if getattr(self, "_retired", False) or self.mode != "fs" or not self.location:
            return False
        if os.path.isdir(self.location):
            self.refresh()
            return False
        child, up = self.location, os.path.dirname(self.location.rstrip("/"))
        while up and up != "/" and not os.path.isdir(up):
            child, up = up, os.path.dirname(up)
        self.open(up or "/", push=False, select=[os.path.basename(child)] if os.path.lexists(child) else None)
        return False

    def _check_location(self):
        """Au clic dans le panneau : un parent renommé ou supprimé par un autre programme n'est pas vu
        par la surveillance du dossier affiché ; un seul stat, local seulement."""
        if self.mode == "fs" and self.location and not is_uri(self.location) and not os.path.isdir(self.location):
            GLib.idle_add(self._on_gone)

    def follow_move(self, old: str, new: str):
        """Un dossier affiché (ou un de ses parents) a été renommé / déplacé dans Odysseus : suivre."""
        if getattr(self, "_retired", False) or self.mode != "fs" or not self.location:
            return
        old = old.rstrip("/")
        if self.location == old or self.location.startswith(old + "/"):
            self.open(new.rstrip("/") + self.location[len(old):], push=False)

    def _check_empty_page(self):
        self._empty_check_id = 0
        if self.model.loading or self.mode == "search" or self.get_root() is None:
            return False
        empty_shown = self.stack.get_visible_child_name() == "empty"
        if empty_shown == bool(self.model.items):
            self._show_content()
            self._update_floating()
        return False

    def _on_reset(self):
        if not self.model.loading:
            self._show_content()
        self.emit("selection-changed")

    def _update_state(self, final: bool = False):
        if self.mode == "search":
            n = len(self.model.items)
            if not final and self._search_cancel is not None:
                self.floating.set_text(tr("Recherche… {0} résultat(s)").format(n))
                self.floating.set_visible(True)
                self.stack.set_visible_child_name(self.view_mode if self.view_mode != "columns" else "list")
                return
            if n == 0:
                self.empty.set_icon_name("edit-find-symbolic")
                self.empty.set_title(tr("Aucun résultat"))
                self.empty.set_description(tr("Rien ne correspond à « {0} »").format(self.search_query))
                self.stack.set_visible_child_name("empty")
            if final:
                self._search_cancel = None
            self._update_floating()
            return
        # comme Nautilus : pas de barre « Restaurer / Vider » quand la corbeille est vide
        self.trash_bar.set_reveal_child(self.mode == "trash" and not self.model.loading and bool(self.model.all))
        self._show_content()
        self._update_floating()

    def refresh_rows(self):
        self.model.refresh_rows()
        if self.miller is not None:
            self.miller.refresh_rows()

    def set_sort(self, key: str, reverse: bool):
        """Tri choisi en cliquant sur un en-tête de colonne (un seul nouveau tri pour les deux réglages)."""
        if key == settings["sort_key"] and reverse == settings["sort_reverse"]:
            return
        settings.data["sort_key"] = key
        settings["sort_reverse"] = reverse

    def _on_setting(self, key, value):
        if key == "show_hidden":
            self.model.set_show_hidden(value)
            if self.miller is not None:
                self.miller.set_show_hidden(value)
            self._update_state()
        elif key in ("sort_key", "sort_reverse", "folders_first"):
            self.model.set_sort(settings["sort_key"], settings["sort_reverse"], settings["folders_first"])
            self.list_view.sync_sort()
        elif key in ("list_zoom", "grid_zoom"):
            self.list_view.apply_zoom()
            self.refresh_rows()
        elif key in ("list_columns", "features"):
            self.list_view.update_columns()
            self.refresh_rows()
            if key == "features" and self.view_mode == "columns" and not features.enabled("column_view"):
                self.set_view_mode("list", save=False)
        elif key in ("hidden_dim_mode", "thumbnails"):
            self.refresh_rows()
        elif key == "single_click":
            self.list_view.view.set_single_click_activate(value)
            self.grid_view.view.set_single_click_activate(value)

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------
    def _on_selection(self):
        self._update_floating()
        self.emit("selection-changed")

    def selected_recs(self) -> list[Rec]:
        if self.view_mode == "columns" and self.miller is not None and self.mode == "fs":
            return self.miller.selected_recs()
        return self.model.selected_recs()

    def selected_paths(self) -> list[str]:
        return [r.path for r in self.selected_recs() if r.path]

    def current_dir(self) -> str:
        """Dossier local où créer ou coller des éléments (vide si impossible)."""
        if self.mode == "fs":
            if self.view_mode == "columns" and self.miller is not None:
                return self.miller.current_dir() or self.location
            return self.location
        if self.mode == "archive":
            return os.path.dirname(self.archive_path)
        if self.mode == "search":
            return self.search_root
        f = Gio.File.new_for_uri(self.location) if is_uri(self.location) else None
        return f.get_path() if f is not None and f.get_path() else ""

    def select_names(self, names: list[str]):
        if not names:
            return
        if not self.model.show_hidden and any(r.hidden and r.name in names for r in self.model.all):
            # ShowItems / « ouvrir le dossier » d'un fichier caché : comme Nautilus, on montre les
            # fichiers cachés de ce panneau (sans changer la préférence)
            self.model.set_show_hidden(True)
        self.model.select_names(names)
        first = self.model.find_name(names[0])
        if first >= 0:
            self.scroll_to(first)

    def _on_zoom_scroll(self, ctrl, _dx, dy):
        if not ctrl.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return False
        # pavé tactile : petits pas cumulés ; molette : un cran = un niveau
        self._zoom_acc += dy
        steps = int(self._zoom_acc)
        if steps:
            self._zoom_acc -= steps
            win = self.get_root()
            for _ in range(abs(steps)):
                win.activate_action("win.zoom-out" if steps > 0 else "win.zoom-in", None)
        return True

    def _scroll_top(self):
        """Dossier ouvert : la vue repart du début. Gtk.ListView / GridView gardent sinon la position
        de défilement du dossier précédent (le modèle est le même, seul son contenu change)."""
        v = self.active_view
        adj = v.get_vadjustment() if isinstance(v, Gtk.ScrolledWindow) else None
        if adj is not None:
            adj.set_value(0)
            GLib.idle_add(lambda: (adj.set_value(0), False)[1])     # après la mise en page des lignes

    def scroll_to(self, pos: int):
        v = self.active_view
        if hasattr(v, "scroll_to"):
            GLib.idle_add(lambda: (v.get_root() is not None and v.scroll_to(pos), False)[1])

    def select_all(self):
        if self.view_mode != "columns":
            self.selection.select_all()

    def unselect_all(self):
        self.selection.unselect_all()

    def invert_selection(self):
        bits = self.selection.get_selection()
        n = len(self.model.items)
        inv = Gtk.Bitset.new_range(0, n) if n else Gtk.Bitset.new_empty()
        inv.difference(bits)
        self.selection.set_selection(inv, Gtk.Bitset.new_range(0, n) if n else Gtk.Bitset.new_empty())

    def focus_view(self):
        if self.get_root() is not None:      # panneau peut-être fermé depuis
            self.active_view.focus()
        return False

    def activate_position(self, pos: int):
        rec = self.model.rec_at(pos)
        if rec is None:
            return
        if rec.get("mountable"):
            self._mount_and_open(rec)
            return
        if rec.is_dir:
            if self.mode == "archive":
                self.open_inner(rec.get("inner", rec.name))
            elif self.mode == "remote" and rec.uri and not rec.get("target_uri"):
                self.open(rec.uri)        # rester dans admin://, smb://… plutôt que le chemin GVfs local
            elif rec.path:
                self.open(rec.path)
            elif rec.uri:
                self.open(rec.get("target_uri") or rec.uri)
            return
        self.emit("activated", rec)

    def _mount_and_open(self, rec: Rec):
        """Partage réseau (emplacement « Réseau ») : le monter puis l'ouvrir."""
        f = Gio.File.new_for_uri(rec.uri)
        op = Gtk.MountOperation.new(self.get_root())

        def done(src, res):
            try:
                target = src.mount_mountable_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    target = None
                else:
                    self.emit("message", e.message)
                    return
            uri = rec.get("target_uri") or (target.get_uri() if target else rec.uri)
            self.open(uri)
        f.mount_mountable(Gio.MountMountFlags.NONE, op, None, done)

    # ------------------------------------------------------------------
    # Textes pour la fenêtre
    # ------------------------------------------------------------------
    def title(self) -> str:
        if self.mode == "archive":
            name = os.path.basename(self.archive_path)
            return f"{name}/{self.inner}" if self.inner else name
        if self.mode == "search":
            return tr("Recherche « {0} »").format(self.search_query)
        if self.mode == "trash":
            return tr("Corbeille")
        if is_uri(self.location):
            if self.location in SPECIAL_URIS:
                return SPECIAL_URIS[self.location]
            f = Gio.File.new_for_uri(self.location)
            name = f.get_basename()
            return name if name and name != "/" else GLib.uri_unescape_string(self.location, None) or self.location
        if self.location == os.path.expanduser("~"):
            return tr("Dossier personnel")
        return os.path.basename(self.location) or "/"

    def location_text(self) -> str:
        if self.mode == "archive":
            return self.archive_path + ("/" + self.inner if self.inner else "")
        if self.mode == "search":
            return self.search_root
        return display_location(self.location)

    def bookmark_target(self) -> str:
        if self.mode == "archive":
            return self.archive_path
        if self.mode == "search":
            return self.search_root
        return self.location

    def _dodge_floating(self, _ctl, x, y):
        bar = self.floating
        if not bar.get_visible():
            return
        ok, bounds = bar.compute_bounds(self.overlay)
        if not ok or not (bounds.get_x() <= x <= bounds.get_x() + bounds.get_width()
                          and bounds.get_y() <= y <= bounds.get_y() + bounds.get_height()):
            return
        start = bar.get_halign() == Gtk.Align.END
        bar.set_halign(Gtk.Align.START if start else Gtk.Align.END)
        (bar.add_css_class if start else bar.remove_css_class)("start")

    def _update_floating(self):
        text = self.status_text()
        self.floating.set_text(shown(text))
        self.floating.set_visible(bool(text))

    def status_text(self) -> str:
        """Texte de la barre d'info, repris de Nautilus (nautilus-files-view.c)."""
        sel = self.selected_recs()
        if not sel:
            if self.model.loading:
                return tr("Chargement…")
            return ""
        dirs = [r for r in sel if r.is_dir]
        files = [r for r in sel if not r.is_dir]
        counts = [r.get("count", r.get("children")) for r in dirs]
        known = all(n is not None and n >= 0 for n in counts)
        inside = sum(counts) if dirs and known else 0
        size = human_size(sum(r.size for r in files)) if files else ""
        parts = []
        if len(dirs) == 1:
            text = tr("« {0} » sélectionné").format(dirs[0].name)
            if known:
                text += " " + ntr("(contenant {0} élément)", "(contenant {0} éléments)", inside).format(inside)
            parts.append(text)
        elif dirs:
            text = ntr("{0} dossier sélectionné", "{0} dossiers sélectionnés", len(dirs)).format(len(dirs))
            if known:
                text += " " + ntr("(contenant {0} élément au total)", "(contenant {0} éléments au total)",
                                  inside).format(inside)
            parts.append(text)
        if files:
            if dirs:
                text = ntr("{0} autre élément sélectionné", "{0} autres éléments sélectionnés",
                           len(files)).format(len(files))
            elif len(files) == 1:
                text = tr("« {0} » sélectionné").format(files[0].name)
            else:
                text = ntr("{0} élément sélectionné", "{0} éléments sélectionnés", len(files)).format(len(files))
            parts.append(f"{text} ({size})")
        return ", ".join(parts)
