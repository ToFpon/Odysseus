"""Fenêtre « Rechercher dans le contenu » (F8), reprise de Search Content.

Deux modes dans la même fenêtre : rechercher, ou rechercher et remplacer avec aperçu ligne à
ligne (ancien texte barré, nouveau texte), cases à cocher, confirmation et sauvegarde .bak.
Les résultats arrivent au fil de l'eau et la recherche peut être arrêtée.
"""

from __future__ import annotations

import os
import subprocess
import threading

from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk, Pango

from ..backend import search
from ..i18n import ntr, tr
from .common import alert, choose_folder, copy_to_clipboard, focus_later, open_with_default, popup_menu, toast


class ResultItem(GObject.Object):
    __gtype_name__ = "OdysseusSearchResult"

    def __init__(self, m: search.Match, after: str = ""):
        super().__init__()
        self.m = m
        self.after = after
        self.checked = True


class ContentSearchWindow(Adw.Window):
    __gtype_name__ = "OdysseusContentSearch"

    def __init__(self, main_window, folder: str):
        super().__init__(title=tr("Recherche dans le contenu"), default_width=900, default_height=640,
                         transient_for=None)
        self.main = main_window
        self.folder = folder or os.path.expanduser("~")
        self._cancel = threading.Event()
        self._running = False
        self._backups: set = set()     # sauvegardes .bak créées ici : pas proposées au remplacement suivant
        self.query = None
        self.toast_overlay = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        modes = Gtk.Box(css_classes=["linked"])
        self.mode_search = Gtk.ToggleButton(label=tr("Rechercher"), active=True)
        self.mode_replace = Gtk.ToggleButton(label=tr("Rechercher et remplacer"), group=self.mode_search)
        modes.append(self.mode_search)
        modes.append(self.mode_replace)
        hb.set_title_widget(modes)
        tv.add_top_bar(hb)
        self.mode_replace.connect("toggled", lambda *_: self._mode_changed())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                       margin_top=12, margin_bottom=8)
        self.entry = Gtk.SearchEntry(placeholder_text=tr("Texte à rechercher…"), hexpand=True)
        self.entry.connect("activate", lambda *_: self.start())
        form.append(self.entry)
        self.replace = Gtk.Entry(placeholder_text=tr("Remplacer par…"), visible=False)
        self.replace.connect("activate", lambda *_: self.start())
        form.append(self.replace)
        row = Gtk.Box(spacing=8)
        self.exts = Gtk.Entry(placeholder_text=tr("Extensions (ex. py,txt,md — vide = toutes)"), hexpand=True)
        row.append(self.exts)
        self.folder_btn = Gtk.Button(tooltip_text=tr("Dossier de recherche"))
        fb = Gtk.Box(spacing=6)
        fb.append(Gtk.Image(icon_name="folder-symbolic"))
        self.folder_lbl = Gtk.Label(ellipsize=Pango.EllipsizeMode.START, max_width_chars=36)
        fb.append(self.folder_lbl)
        self.folder_btn.set_child(fb)
        self.folder_btn.connect("clicked", lambda *_: choose_folder(self, tr("Dossier de recherche"), self.folder,
                                                                    self._set_folder))
        row.append(self.folder_btn)
        form.append(row)
        opts = Gtk.Box(spacing=14)
        self.rec = Gtk.CheckButton(label=tr("Sous-dossiers"), active=True)
        self.case = Gtk.CheckButton(label=tr("Respecter la casse"))
        self.word = Gtk.CheckButton(label=tr("Mot entier"))
        self.regex = Gtk.CheckButton(label=tr("Expression régulière"))
        self.hidden = Gtk.CheckButton(label=tr("Fichiers cachés"))
        for w in (self.rec, self.case, self.word, self.regex, self.hidden):
            opts.append(w)
        tool = Gtk.Label(label=tr("Outil : {0}").format("ripgrep" if search.tool() == "rg" else "grep"),
                         css_classes=["dim-label"], hexpand=True, xalign=1)
        opts.append(tool)
        form.append(opts)
        btns = Gtk.Box(spacing=8)
        self.go_btn = Gtk.Button(label=tr("Rechercher"), css_classes=["suggested-action"])
        self.go_btn.connect("clicked", lambda *_: self.start())
        btns.append(self.go_btn)
        self.stop_btn = Gtk.Button(label=tr("Arrêter"), visible=False)
        self.stop_btn.connect("clicked", lambda *_: self._cancel.set())
        btns.append(self.stop_btn)
        self.apply_btn = Gtk.Button(label=tr("Remplacer les lignes cochées…"), css_classes=["destructive-action"],
                                    visible=False, sensitive=False)
        self.apply_btn.connect("clicked", lambda *_: self.confirm_replace())
        btns.append(self.apply_btn)
        self.all_check = Gtk.CheckButton(label=tr("Tout cocher"), active=True, visible=False)
        self.all_check.connect("toggled", self._toggle_all)
        btns.append(self.all_check)
        self.backup = Gtk.CheckButton(label=tr("Sauvegarde .bak"), active=True, visible=False)
        btns.append(self.backup)
        self.status = Gtk.Label(xalign=1, hexpand=True, css_classes=["dim-label"])
        btns.append(self.status)
        form.append(btns)
        body.append(form)
        body.append(Gtk.Separator())

        self.store = Gio.ListStore(item_type=ResultItem)
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._setup)
        f.connect("bind", self._bind)
        self.list = Gtk.ListView(model=Gtk.SingleSelection(model=self.store, autoselect=False), factory=f,
                                 css_classes=["navigation-sidebar"])
        self.list.connect("activate", lambda _l, pos: self._open(self.store.get_item(pos)))
        body.append(Gtk.ScrolledWindow(child=self.list, vexpand=True))
        tv.set_content(body)
        self.toast_overlay.set_child(tv)
        self.set_content(self.toast_overlay)
        self._set_folder(self.folder)
        esc = Gtk.ShortcutController()
        esc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                          Gtk.CallbackAction.new(lambda *_: self.close() or True)))
        self.add_controller(esc)
        self.connect("close-request", lambda *_: self._cancel.set() or False)
        focus_later(self.entry)

    # ------------------------------------------------------------------
    def _set_folder(self, path):
        self.folder = path
        self.folder_lbl.set_text(path)
        self.folder_btn.set_tooltip_text(path)

    def _mode_changed(self):
        rep = self.mode_replace.get_active()
        self.replace.set_visible(rep)
        self.apply_btn.set_visible(rep)
        self.all_check.set_visible(rep)
        self.backup.set_visible(rep)
        self.go_btn.set_label(tr("Aperçu") if rep else tr("Rechercher"))
        self.store.remove_all()
        self.status.set_text("")

    def _setup(self, _f, li):
        box = Gtk.Box(spacing=10, margin_start=4, margin_end=4)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.connect("toggled", lambda c: setattr(li.get_item(), "checked", c.get_active()) if li.get_item() else None)
        box.append(check)
        name = Gtk.Label(xalign=0, width_chars=28, max_width_chars=28, ellipsize=Pango.EllipsizeMode.MIDDLE,
                         css_classes=["search-result"])
        box.append(name)
        line = Gtk.Label(xalign=1, width_chars=6, css_classes=["dim-label", "numeric"])
        box.append(line)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        texts.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["search-result"]))
        texts.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["search-result", "diff-new"]))
        box.append(texts)
        li.set_child(box)
        g = Gtk.GestureClick(button=3)
        g.connect("pressed", lambda gest, n, x, y: self._menu(li.get_item(), box, x, y))
        box.add_controller(g)

    def _bind(self, _f, li):
        it: ResultItem = li.get_item()
        box = li.get_child()
        check = box.get_first_child()
        name = check.get_next_sibling()
        line = name.get_next_sibling()
        texts = box.get_last_child()
        old, new = texts.get_first_child(), texts.get_last_child()
        rep = bool(it.after)
        check.set_visible(rep)
        check.set_active(it.checked)
        rel = os.path.relpath(it.m.path, self.folder) if it.m.path.startswith(self.folder) else it.m.path
        name.set_text(rel)
        name.set_tooltip_text(it.m.path)
        line.set_text(str(it.m.line))
        old.set_text(it.m.text.strip())
        if rep:
            old.add_css_class("diff-old")
        else:
            old.remove_css_class("diff-old")
        new.set_visible(rep)
        new.set_text(it.after.strip())

    def _toggle_all(self, c):
        for i in range(self.store.get_n_items()):
            self.store.get_item(i).checked = c.get_active()
        n = self.store.get_n_items()
        self.store.items_changed(0, n, n)

    # ------------------------------------------------------------------
    def _query(self) -> search.Query:
        return search.Query(self.entry.get_text(), self.folder, self.exts.get_text(), self.rec.get_active(),
                            self.case.get_active(), self.regex.get_active(), self.word.get_active(),
                            self.hidden.get_active())

    def start(self):
        if self._running:
            return
        q = self._query()
        if not q.pattern:
            self.status.set_text(tr("Saisissez un texte à rechercher."))
            return
        if q.regex:
            try:
                search.compile_rx(q)
            except Exception as e:  # noqa: BLE001
                self.status.set_text(tr("Expression invalide : {0}").format(e))
                return
        self.query = q
        replacing = self.mode_replace.get_active()
        replacement = self.replace.get_text()
        self.store.remove_all()
        self._cancel = threading.Event()
        cancel = self._cancel
        self._running = True
        self.go_btn.set_sensitive(False)
        self.stop_btn.set_visible(True)
        self.apply_btn.set_sensitive(False)
        self.status.set_text(tr("Recherche en cours…"))

        def add(batch):
            batch = [m for m in batch if m.path not in self._backups]
            if replacing:
                items = [ResultItem(m, after) for m, after in search.preview_replace(q, batch, replacement)]
            else:
                items = [ResultItem(m) for m in batch]
            GLib.idle_add(lambda: (self.store.splice(self.store.get_n_items(), 0, items),
                                   self.status.set_text(ntr("{0} résultat…", "{0} résultats…",
                                                            self.store.get_n_items()).format(self.store.get_n_items())),
                                   False)[2])

        def work():
            count, truncated, err = search.run(q, add, cancel.is_set)
            GLib.idle_add(self._done, truncated, err)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, truncated, err):
        self._running = False
        self.go_btn.set_sensitive(True)
        self.stop_btn.set_visible(False)
        n = self.store.get_n_items()
        if err:
            self.status.set_text(tr("Erreur : {0}").format(err))
        elif n == 0:
            self.status.set_text(tr("Aucun résultat."))
        else:
            text = ntr("{0} résultat", "{0} résultats", n).format(n)
            files = len({self.store.get_item(i).m.path for i in range(n)})
            text += " · " + ntr("{0} fichier", "{0} fichiers", files).format(files)
            if truncated:
                text += " · " + tr("limite atteinte")
            if self._cancel.is_set():
                text += " · " + tr("interrompu")
            self.status.set_text(text)
        self.apply_btn.set_sensitive(self.mode_replace.get_active() and n > 0)
        return False

    def confirm_replace(self):
        items = [self.store.get_item(i) for i in range(self.store.get_n_items())]
        chosen = [it for it in items if it.checked and it.after]
        if not chosen:
            self.status.set_text(tr("Aucune ligne cochée."))
            return
        targets: dict = {}
        for it in chosen:
            targets.setdefault(it.m.path, set()).add(it.m.line)
        backup = self.backup.get_active()
        body = ntr("Cette action va modifier {0} fichier.", "Cette action va modifier {0} fichiers.",
                   len(targets)).format(len(targets))
        body += "\n" + (tr("Une sauvegarde .bak sera créée pour chaque fichier.") if backup
                        else tr("Aucune sauvegarde ne sera faite."))
        alert(self, tr("Confirmer le remplacement"), body,
              responses=(("cancel", tr("Annuler")), ("ok", tr("Remplacer"))), destructive="ok", default="cancel",
              callback=lambda r: r == "ok" and self._replace(targets, backup))

    def _replace(self, targets, backup):
        q, replacement = self.query, self.replace.get_text()

        def work():
            made = []
            res = search.apply_replace(q, targets, replacement, backup, made)
            GLib.idle_add(lambda: (self._backups.update(made), self._replaced(*res))[1])
        threading.Thread(target=work, daemon=True).start()
        self.status.set_text(tr("Remplacement en cours…"))

    def _replaced(self, total, files, errors):
        msg = tr("{0} remplacement(s) dans {1} fichier(s) ✓").format(total, files)
        self.status.set_text(msg)
        toast(self, msg)
        if errors:
            alert(self, tr("Certains fichiers n'ont pas été modifiés"),
                  "\n".join(f"{os.path.basename(p)} : {m}" for p, m in errors[:15]))
        GLib.timeout_add(300, lambda: (self.start(), False)[1])
        return False

    # ------------------------------------------------------------------
    def _open(self, it: ResultItem, editor: bool = False):
        if it is None:
            return
        if editor:
            from .actions_files import find_editor
            ed = find_editor()
            if ed:
                argv = ed.split()
                name = os.path.basename(argv[0])
                if name in ("gedit", "xed", "pluma", "kate", "geany", "mousepad", "gnome-text-editor"):
                    argv.append(f"+{it.m.line}")
                try:
                    subprocess.Popen(argv + [it.m.path], start_new_session=True)
                    return
                except OSError:
                    pass
        open_with_default(self, it.m.path)

    def _menu(self, it, widget, x, y):
        if it is None:
            return
        group = Gio.SimpleActionGroup()
        m = Gio.Menu()
        acts = [("open", tr("Ouvrir le fichier"), lambda: self._open(it)),
                ("edit", tr("Ouvrir dans l'éditeur à la ligne"), lambda: self._open(it, True)),
                ("show", tr("Afficher dans Odysseus"),
                 lambda: self.main.pane.open(os.path.dirname(it.m.path), select=[os.path.basename(it.m.path)])),
                ("copy", tr("Copier le chemin"), lambda: (copy_to_clipboard(self, it.m.path),
                                                          toast(self, tr("Chemin copié"))))]
        for name, label, cb in acts:
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            group.add_action(a)
            m.append(label, f"res.{name}")
        widget.insert_action_group("res", group)
        ok, pt = widget.compute_point(widget, Graphene.Point().init(x, y))
        popup_menu(widget, m, x, y)
