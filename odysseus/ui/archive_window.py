"""Fenêtre d'archive, comme l'extension Archive Browser (et File Roller avant qu'il ne sache plus le faire
avec Nautilus) : un double-clic sur une archive l'ouvre dans cette petite fenêtre, la fenêtre d'Odysseus
garde son dossier.

À gauche, l'arborescence de l'archive : dossiers dépliés sur place (on ne s'y perd pas, même avec des
dossiers sur plusieurs niveaux), filtre par nom. À droite, un dossier où naviguer. N'importe quel élément
de l'archive se glisse vers ce dossier, vers Odysseus, Nautilus ou le bureau (préparé d'avance :
archive_drag.DragCache). « Tout extraire » / « Extraire la sélection » : dans le dossier de droite.
Copies, conflits, progression et erreurs passent par la fenêtre principale (drop_files, _extract_one)."""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from ..backend import archive, scanner
from ..backend.engine import PasswordRequired, human_size
from ..i18n import ntr, tr
from ..settings import session_passwords, settings
from .archive_drag import DragCache
from .common import ask_password, open_with_default, popup_menu, toast
from .icons import ctype_icon, gicon_for
from .model import fold

PREVIEW_LIMIT = 512 * 1024 * 1024


class ArchItem(GObject.Object):
    __gtype_name__ = "OdysseusArchItem"

    def __init__(self, entry):
        super().__init__()
        self.entry = entry


class FsItem(GObject.Object):
    __gtype_name__ = "OdysseusFsItem"

    def __init__(self, rec):
        super().__init__()
        self.rec = rec


def _entry_key(e):
    return (not e.is_dir, scanner.sort_name(e.name))


def _icon_for_entry(e) -> Gio.Icon:
    if e.is_dir:
        return Gio.ThemedIcon.new("folder")
    ctype, _u = Gio.content_type_guess(e.name, None)
    return ctype_icon(ctype or "application/octet-stream")


def _header(*labels) -> Gtk.Box:
    """En-tête Nom / Taille, comme celui des colonnes d'Odysseus."""
    box = Gtk.Box(spacing=6, css_classes=["archive-browser-header", "dim-label", "caption-heading"])
    name = Gtk.Label(label=labels[0], xalign=0, hexpand=True)
    size = Gtk.Label(label=labels[1], xalign=1, width_chars=10)
    box.append(name)
    box.append(size)
    return box


def _row_widgets(with_expander: bool):
    img = Gtk.Image(pixel_size=16)
    name = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
    sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START, css_classes=["dim-label", "caption"],
                    visible=False)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
    col.append(name)
    col.append(sub)
    size = Gtk.Label(xalign=1, width_chars=10, css_classes=["dim-label", "numeric"])
    box = Gtk.Box(spacing=8, css_classes=["archive-browser-row"])
    box.append(img)
    box.append(col)
    box.append(size)
    top = Gtk.TreeExpander(child=box, indent_for_icon=True) if with_expander else box
    top._img, top._name, top._sub, top._size = img, name, sub, size
    return top


class FolderPanel(Gtk.Box):
    """Dossier de droite : où l'on dépose et où l'on extrait."""

    def __init__(self, win: "ArchiveWindow", path: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["archive-browser-folder"])
        self.win = win
        self.path = path
        self._gen = 0
        self._monitor = None
        self._refresh_id = 0
        bar = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=tr("Dossier parent"), css_classes=["flat"])
        up.connect("clicked", lambda *_: self.navigate(os.path.dirname(self.path.rstrip("/")) or "/"))
        self.entry = Gtk.Entry(hexpand=True)
        self.entry.connect("activate", self._on_entry)
        home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text=tr("Dossier personnel"), css_classes=["flat"])
        home.connect("clicked", lambda *_: self.navigate(os.path.expanduser("~")))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=tr("Actualiser"), css_classes=["flat"])
        refresh.connect("clicked", lambda *_: self.refresh())
        new = Gtk.Button(icon_name="folder-new-symbolic", tooltip_text=tr("Nouveau dossier (Ctrl+Maj+N)"),
                         css_classes=["flat"])
        new.connect("clicked", lambda *_: self.new_folder())
        for w in (up, self.entry, new, home, refresh):
            bar.append(w)
        self._select_after: Optional[str] = None
        self.append(bar)
        self.append(_header(tr("Nom"), tr("Taille")))
        self.store = Gio.ListStore(item_type=FsItem)
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=False, can_unselect=True)
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: li.set_child(_row_widgets(False)))
        f.connect("bind", self._bind)
        self.view = Gtk.ListView(model=self.selection, factory=f)
        self.view.add_css_class("archive-browser-list")
        self.view.connect("activate", self._on_activate)
        sw = Gtk.ScrolledWindow(vexpand=True, child=self.view)
        self.append(sw)
        # clic droit, comme le fond d'un dossier dans Nautilus : « Nouveau dossier… »
        actions = Gio.SimpleActionGroup()
        act = Gio.SimpleAction.new("new-folder", None)
        act.connect("activate", lambda *_: self.new_folder())
        actions.add_action(act)
        self.insert_action_group("folder", actions)
        self._menu = Gio.Menu()
        self._menu.append(tr("Nouveau dossier…"), "folder.new-folder")
        menu_click = Gtk.GestureClick(button=3)
        menu_click.connect("pressed", lambda g, _n, x, y: popup_menu(sw, self._menu, x, y))
        sw.add_controller(menu_click)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.connect("enter", lambda *_: (self.add_css_class("drop-hover"), Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_: self.remove_css_class("drop-hover"))
        drop.connect("drop", self._on_drop)
        sw.add_controller(drop)
        self.navigate(path)

    def _bind(self, _f, li):
        w, rec = li.get_child(), li.get_item().rec
        w._rec = rec
        w._img.set_from_gicon(gicon_for(rec))
        w._name.set_text(rec.name)
        w._size.set_text("" if rec.is_dir else human_size(rec.size))

    def _on_entry(self, entry):
        path = os.path.expanduser(entry.get_text().strip())
        if os.path.isdir(path):
            self.navigate(path)
        else:
            entry.set_text(self.path)
            toast(self, tr("Emplacement introuvable : {0}").format(path))

    def navigate(self, path: str):
        self.path = os.path.abspath(path)
        self.entry.set_text(self.path)
        self.entry.set_position(-1)             # la fin du chemin (le dossier) reste visible
        if self._monitor is not None:
            self._monitor.cancel()
        try:
            self._monitor = Gio.File.new_for_path(self.path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", lambda *_: self._queue_refresh())
        except GLib.Error:
            self._monitor = None
        self.refresh()

    def stop(self):
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    def _queue_refresh(self):
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(300, self._refresh_later)

    def _refresh_later(self):
        self._refresh_id = 0
        self.refresh()
        return False

    def refresh(self):
        self._gen += 1
        gen, path, hidden = self._gen, self.path, settings["show_hidden"]

        def work():
            try:
                recs = scanner.scan_local(path, threading.Event(), with_stat=True)
            except OSError:
                recs = []
            recs = scanner.sort_recs([r for r in recs if hidden or not r.hidden], "name", False, True)
            GLib.idle_add(self._fill, gen, recs)
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, gen, recs):
        if gen != self._gen:
            return False
        cur = self.selection.get_selected_item()
        keep = cur.rec.name if cur is not None else None      # la sélection survit à un rafraîchissement
        self.store.splice(0, self.store.get_n_items(), [FsItem(r) for r in recs])
        want = self._select_after or keep
        for i, r in enumerate(recs):
            if r.name == want:
                self.selection.set_selected(i)
                if want == self._select_after:
                    self.view.scroll_to(i, Gtk.ListScrollFlags.FOCUS, None)
                self._select_after = None
                break
        return False

    def new_folder(self):
        """Nouveau dossier dans le dossier affiché : même question que dans Odysseus, posée sur cette
        fenêtre ; annulable (Ctrl+Z) dans la fenêtre principale."""
        from ..backend import fileops
        from .common import alert
        host, folder = self.win.host_window(), self.path

        def make(name):
            try:
                path = fileops.make_folder(folder, name)
            except Exception as e:  # noqa: BLE001
                alert(self.win, tr("Création impossible"), getattr(e, "strerror", None) or str(e))
                return
            host._push_undo(("create", [path]))
            if folder == self.path:
                self._select_after = name
                self.refresh()
        host.ask_name(tr("Nouveau dossier"), fileops.unique_name(folder, tr("Nouveau dossier"), True),
                      tr("Créer"), make, folder, select_stem=False, parent=self.win)

    def _on_activate(self, _view, pos):
        rec = self.store.get_item(pos).rec
        if rec.is_dir:
            self.navigate(rec.path)
        elif settings["archive_window"] and archive_like(rec.name):
            open_archive_window(self.win.get_application(), rec.path, self.win.host_window())
        else:
            open_with_default(self.win, rec.path)

    def _folder_at(self, x: float, y: float) -> str:
        w = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and not hasattr(w, "_rec"):
            w = w.get_parent()
        rec = getattr(w, "_rec", None)
        return rec.path if rec is not None and rec.is_dir else self.path

    def _on_drop(self, target, value, x, y):
        self.remove_css_class("drop-hover")
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        ok, pt = target.get_widget().compute_point(self.view, Graphene.Point().init(x, y))
        dest = self._folder_at(pt.x, pt.y) if ok else self.path
        self.win.copy_into(paths, dest)
        return True


def archive_like(name: str) -> bool:
    from ..backend import formats
    return formats.looks_like_archive(name)


class ArchiveWindow(Adw.ApplicationWindow):
    __gtype_name__ = "OdysseusArchiveWindow"

    def __init__(self, app, path: str, host=None):
        super().__init__(application=app, default_width=1000, default_height=650)
        self.add_css_class("archive-browser")
        self.archive_file = os.path.abspath(path)
        self.host = host
        self.password = session_passwords.get(self.archive_file, "")
        self.listing = None
        self.drag = DragCache(lambda: (self.archive_file, self.password, self.listing),
                              lambda msg: toast(self, msg))
        self.title = Adw.WindowTitle(title=os.path.basename(path))
        self.set_title(os.path.basename(path))
        hb = Adw.HeaderBar(title_widget=self.title)
        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        self.toast_overlay = Adw.ToastOverlay(child=tv)
        self.set_content(self.toast_overlay)

        paned = Gtk.Paned(position=470, shrink_start_child=False, shrink_end_child=False)
        paned.set_start_child(self._build_archive_side())
        self.folder = FolderPanel(self, os.path.dirname(self.archive_file))
        paned.set_end_child(self.folder)
        tv.set_content(paned)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>w|<Control>q"),
                                           Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                           Gtk.CallbackAction.new(lambda *_: (self.filter.grab_focus(), True)[1])))
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control><Shift>n"),
                                           Gtk.CallbackAction.new(lambda *_: (self.folder.new_folder(), True)[1])))
        self.add_controller(keys)
        self.connect("close-request", self._on_close)
        self._load()

    def _on_close(self, *_):
        self.drag.forget()
        self.folder.stop()
        return False

    # ------------------------------------------------------------------
    # Construction : arborescence de l'archive
    # ------------------------------------------------------------------
    def _build_archive_side(self) -> Gtk.Widget:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.filter = Gtk.SearchEntry(placeholder_text=tr("Filtrer…"), margin_start=6, margin_end=6,
                                      margin_top=6, margin_bottom=6)
        self.filter.connect("search-changed", lambda *_: self._apply_filter())
        self.filter.connect("stop-search", lambda e: e.set_text(""))
        side.append(self.filter)
        side.append(_header(tr("Nom"), tr("Taille")))
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: li.set_child(_row_widgets(True)))
        f.connect("bind", self._bind)
        self.view = Gtk.ListView(factory=f)
        self.view.add_css_class("archive-browser-list")
        self.view.connect("activate", self._on_activate)
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", self._drag_prepare)
        drag.connect("drag-begin", self._drag_begin)
        self.view.add_controller(drag)
        self.stack = Gtk.Stack(vexpand=True)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        loading.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        loading.append(Gtk.Label(label=tr("Lecture de l'archive…"), css_classes=["dim-label"]))
        self.stack.add_named(loading, "loading")
        self.stack.add_named(Gtk.ScrolledWindow(child=self.view), "tree")
        side.append(self.stack)
        bar = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        self.extract_all_btn = Gtk.Button(label=tr("Tout extraire"), css_classes=["suggested-action"],
                                          sensitive=False)
        self.extract_all_btn.connect("clicked", lambda *_: self.extract(selection=False))
        self.extract_sel_btn = Gtk.Button(label=tr("Extraire la sélection"), sensitive=False)
        self.extract_sel_btn.connect("clicked", lambda *_: self.extract(selection=True))
        bar.append(self.extract_all_btn)
        bar.append(self.extract_sel_btn)
        side.append(bar)
        return side

    def _bind(self, _f, li):
        w, item = li.get_child(), li.get_item()
        if isinstance(item, Gtk.TreeListRow):
            w.set_list_row(item)
            entry, flat = item.get_item().entry, False
        else:
            w.set_list_row(None)
            entry, flat = item.entry, True
        w._item = item           # pas la position : elle change quand un dossier au-dessus se déplie
        w._img.set_from_gicon(_icon_for_entry(entry))
        w._name.set_text(entry.name)
        parent = entry.path.rpartition("/")[0]
        w._sub.set_text(parent)
        w._sub.set_visible(flat and bool(parent))
        if entry.is_dir:
            n = len(self.listing.children(entry.path)) if self.listing else 0
            w._size.set_text(ntr("{0} élément", "{0} éléments", n).format(n))
        else:
            w._size.set_text(human_size(entry.size))

    # ------------------------------------------------------------------
    # Lecture de l'archive
    # ------------------------------------------------------------------
    def _load(self, wrong: bool = False):
        self.stack.set_visible_child_name("loading")
        path, pw = self.archive_file, self.password

        def work():
            try:
                res, err = archive.list_archive(path, pw or None), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            GLib.idle_add(self._loaded, res, err)
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, listing, err):
        if isinstance(err, PasswordRequired):
            def answer(pw):
                if pw is None:
                    self.close()
                    return
                self.password = pw
                self._load()
            ask_password(self, self.archive_file, answer, wrong=bool(self.password))
            return False
        if err is not None or listing is None:
            toast(self, str(err) or tr("Lecture impossible"))
            self.stack.set_visible_child_name("tree")
            return False
        if self.password and settings["remember_passwords"]:
            session_passwords[self.archive_file] = self.password
        self.listing = listing
        files, size = listing.file_count, listing.total_size
        self.title.set_subtitle(ntr("{0} fichier", "{0} fichiers", files).format(files) + " · " + human_size(size))
        root = Gio.ListStore(item_type=ArchItem)
        root.splice(0, 0, [ArchItem(e) for e in sorted(listing.top_level(), key=_entry_key)])
        self.tree = Gtk.TreeListModel.new(root, False, False, self._children)
        self._set_model(self.tree)
        self.extract_all_btn.set_sensitive(True)
        self.stack.set_visible_child_name("tree")
        return False

    def _children(self, item):
        e = item.entry
        if not e.is_dir:
            return None
        store = Gio.ListStore(item_type=ArchItem)
        store.splice(0, 0, [ArchItem(c) for c in sorted(self.listing.children(e.path), key=_entry_key)])
        return store

    def _set_model(self, model):
        self.selection = Gtk.MultiSelection(model=model)
        self.selection.connect("selection-changed", lambda *_: self._on_selection())
        self.view.set_model(self.selection)
        self._on_selection()

    def _apply_filter(self):
        if self.listing is None:
            return
        text = fold(self.filter.get_text().strip())
        if not text:
            self._set_model(self.tree)
            return
        found = [e for e in self.listing.entries if e.path and text in fold(e.name)]
        found.sort(key=lambda e: (e.path.count("/"), scanner.sort_name(e.path)))
        store = Gio.ListStore(item_type=ArchItem)
        store.splice(0, 0, [ArchItem(e) for e in found])
        self._set_model(store)

    # ------------------------------------------------------------------
    # Sélection, ouverture
    # ------------------------------------------------------------------
    def _entry_at(self, pos: int):
        item = self.selection.get_item(pos)
        if isinstance(item, Gtk.TreeListRow):
            item = item.get_item()
        return item.entry if item is not None else None

    def selected_entries(self) -> list:
        out = []
        bits = self.selection.get_selection()
        ok, it, pos = Gtk.BitsetIter.init_first(bits)
        while ok:
            e = self._entry_at(pos)
            if e is not None:
                out.append(e)
            ok, pos = it.next()
        # un dossier et son contenu sélectionnés : le dossier suffit
        dirs = [e.path + "/" for e in out if e.is_dir]
        return [e for e in out if not any(e.path.startswith(d) for d in dirs)]

    def _items(self) -> list:
        return [(e.path, e.is_dir, e.size or 0) for e in self.selected_entries()]

    def _on_selection(self):
        self.extract_sel_btn.set_sensitive(bool(self.selection.get_selection().get_size()))
        self.drag.queue(self._items)

    def select_paths(self, paths: list[str]):
        """Sélectionne des éléments par leur chemin dans l'archive (dossiers parents dépliés)."""
        for p in paths:
            parts = p.strip("/").split("/")
            for depth in range(1, len(parts) + 1):
                want = "/".join(parts[:depth])
                for i in range(self.selection.get_n_items()):
                    row = self.selection.get_item(i)
                    e = (row.get_item() if isinstance(row, Gtk.TreeListRow) else row).entry
                    if e.path == want:
                        if depth < len(parts) and isinstance(row, Gtk.TreeListRow):
                            row.set_expanded(True)
                        elif depth == len(parts):
                            self.selection.select_item(i, False)
                        break

    def _on_activate(self, _view, pos):
        row = self.selection.get_item(pos)
        if isinstance(row, Gtk.TreeListRow) and row.is_expandable():
            row.set_expanded(not row.get_expanded())
            return
        e = self._entry_at(pos)
        if e is None or e.is_dir:
            return
        if e.size and e.size > PREVIEW_LIMIT:
            toast(self, tr("Fichier volumineux : utilisez « Extraire »"))
            return
        from .jobs import manager
        arch, pw = self.archive_file, self.password

        def done(job):
            if job.state != "done":
                toast(self, job.error or tr("Lecture impossible"))
                return
            target = os.path.join(job.result, e.name)
            if archive_like(e.name):
                open_archive_window(self.get_application(), target, self.host_window())
            else:
                open_with_default(self, target)
        manager.submit(tr("Ouverture de {0}").format(e.name),
                       lambda j: archive.extract_to_temp(j, arch, [e.path], pw), done)

    # ------------------------------------------------------------------
    # Glisser
    # ------------------------------------------------------------------
    def _position_of(self, item) -> Optional[int]:
        if isinstance(item, Gtk.TreeListRow):
            return item.get_position()
        for i in range(self.selection.get_n_items()):
            if self.selection.get_item(i) is item:
                return i
        return None

    def _drag_prepare(self, source, x, y):
        w = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and not hasattr(w, "_item"):
            w = w.get_parent()
        pos = self._position_of(w._item) if w is not None else None
        if pos is None:
            return None
        if not self.selection.is_selected(pos):
            self.selection.select_item(pos, True)
        files = self.drag.files_for(self._items())
        if not files:
            return None
        self._dragged = files
        return Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list([Gio.File.new_for_path(p) for p in files]))

    def _drag_begin(self, _source, drag):
        files = getattr(self, "_dragged", [])
        entries = self.selected_entries()
        box = Gtk.Box(spacing=6, css_classes=["drag-icon"])
        img = Gtk.Image(pixel_size=32)
        img.set_from_gicon(_icon_for_entry(entries[0]) if entries else Gio.ThemedIcon.new("text-x-generic"))
        box.append(img)
        if len(files) > 1:
            box.append(Gtk.Label(label=str(len(files)), css_classes=["drag-count"]))
        Gtk.DragIcon.get_for_drag(drag).set_child(box)

    # ------------------------------------------------------------------
    # Copier, extraire (par la fenêtre principale : conflits, progression, annulation)
    # ------------------------------------------------------------------
    def host_window(self):
        from .window import MainWindow
        app = self.get_application()
        wins = app.get_windows() if app else []
        if self.host is not None and self.host in wins:
            return self.host
        for w in wins:
            if isinstance(w, MainWindow):
                self.host = w
                return w
        self.host = MainWindow(app, self.folder.path)
        self.host.present()
        return self.host

    def copy_into(self, paths: list[str], dest: str):
        cache = os.path.join(tempfile.gettempdir(), archive.DRAG_PREFIX)
        action = "copy" if all(p.startswith(cache) for p in paths) else "auto"
        self.host_window().drop_files(paths, dest, action)

    def extract(self, selection: bool):
        if self.listing is None:
            return
        files = [e.path for e in self.selected_entries()] if selection else []
        if selection and not files:
            return
        o = archive.ExtractOptions(dest=self.folder.path, password=self.password,
                                   subfolder="never" if files else settings["extract_subfolder"],
                                   overwrite=settings["extract_overwrite"], files=files)
        self.host_window()._extract_one(self.archive_file, o)


def open_archive_window(app, path: str, host=None) -> ArchiveWindow:
    """Une fenêtre par archive : si elle est déjà ouverte, elle revient devant."""
    path = os.path.abspath(path)
    for w in app.get_windows():
        if isinstance(w, ArchiveWindow) and w.archive_file == path:
            w.present()
            return w
    w = ArchiveWindow(app, path, host)
    w.present()
    return w
