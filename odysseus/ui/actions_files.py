"""Actions sur les fichiers de la fenêtre principale."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk

from .. import features
from ..backend import fileops, folder_colors, formats
from ..backend.engine import Job, human_size
from ..i18n import ntr, tr
from ..settings import settings
from . import clipboard
from .common import alert, choose_folder, copy_to_clipboard, describe_paths, full_date, open_with_default, toast
from .jobs import ProgressWindow, finished_toast, manager

TERMINALS = ["x-terminal-emulator", "gnome-terminal", "kgx", "ptyxis", "tilix", "terminator", "konsole",
             "xfce4-terminal", "mate-terminal", "lxterminal", "alacritty", "kitty", "xterm"]
EDITORS = ["gnome-text-editor", "gedit", "xed", "pluma", "mousepad", "kate", "kwrite", "featherpad", "geany"]


def find_terminal() -> Optional[str]:
    if settings["terminal"] and shutil.which(settings["terminal"].split()[0]):
        return settings["terminal"]
    for t in TERMINALS:
        if shutil.which(t):
            return t
    return None


def find_editor() -> Optional[str]:
    if settings["text_editor"] and shutil.which(settings["text_editor"].split()[0]):
        return settings["text_editor"]
    for e in EDITORS:
        if shutil.which(e):
            return e
    return None


class FileActions:
    """Mélangé à MainWindow."""

    def setup_file_actions(self):
        acts = {
            "open": self.open_selected, "open-default": self.open_default, "open-with": self.open_with_dialog,
            "open-tab": self.open_in_tab, "open-other": self.open_in_other, "open-location": self.open_item_location,
            "cut": lambda: self.clip(True), "copy": lambda: self.clip(False), "paste": self.paste,
            "paste-into": self.paste_into, "move-to": lambda: self.transfer_to(True),
            "copy-to": lambda: self.transfer_to(False),
            "copy-to-other": lambda: self.to_other(False), "move-to-other": lambda: self.to_other(True),
            "rename": self.rename, "make-link": self.make_link, "trash": self.trash, "delete": self.delete,
            "restore": self.restore, "empty-trash": self.empty_trash,
            "new-folder": self.new_folder, "new-file": self.new_file_named, "new-empty-file": self.new_file_named,
            "copy-path": self.copy_path, "copy-location": self.copy_location, "bookmark": self.bookmark_toggle,
            "properties": self.properties, "properties-folder": self.properties_folder,
            "terminal": self.terminal_here, "terminal-selected": self.terminal_selected,
            "edit-text": self.edit_text, "undo": self.undo,
            "folder-restore": self.folder_restore, "browse-archive": self.browse_archive,
        }
        for name, cb in acts.items():
            self._add(name, cb)
        self._add("open-with-app", self.open_with_app, "s")
        self._add("new-from-template", self.new_from_template, "s")
        self._add("properties-path", lambda p: self._show_properties([p]), "s")
        self._add("folder-color", self.folder_color, "s")
        self._add("emblem", self.emblem, "s")

    def file_accels(self) -> dict:
        return {
            "win.cut": ["<Ctrl>x"], "win.copy": ["<Ctrl>c"], "win.paste": ["<Ctrl>v"],
            "win.rename": ["F2"], "win.trash": ["Delete", "KP_Delete"], "win.delete": ["<Shift>Delete"],
            "win.new-folder": ["<Ctrl><Shift>n"], "win.properties": ["<Ctrl>i", "<Alt>Return"],
            "win.copy-path": ["<Ctrl><Shift>c"], "win.bookmark": ["<Ctrl>d"], "win.make-link": ["<Ctrl>m"],
            "win.open-tab": ["<Ctrl>Return"], "win.undo": ["<Ctrl>z"],
            "win.copy-to-other": ["F6"], "win.move-to-other": ["<Shift>F6"],
            "win.terminal": ["<Ctrl><Alt>t"],
        }

    # ------------------------------------------------------------------
    # Ouvrir
    # ------------------------------------------------------------------
    def _sel(self):
        return self.pane.selected_recs() if self.pane else []

    def _sel_paths(self):
        return [r.path for r in self._sel() if r.path]

    def open_selected(self):
        p = self.pane
        sel = self._sel()
        dirs = [r for r in sel if r.is_dir]
        if len(sel) == 1 and dirs:
            r = dirs[0]
            if p.mode == "archive":
                p.open_inner(r.get("inner", r.name))
            else:
                p.open(r.path or r.uri)
            return
        for r in dirs:
            self.new_tab(r.path or r.uri, select=False)
        for r in sel:
            if not r.is_dir:
                self._on_activated(p, r)

    def _on_activated(self, pane, rec):
        if pane.mode == "archive":
            self.preview_archive_item(pane, rec)
            return
        if pane.mode == "trash":
            return
        path = rec.path
        if path is None:
            uri = rec.get("target_uri") or rec.uri
            if uri:
                Gtk.FileLauncher.new(Gio.File.new_for_uri(uri)).launch(self, None, None)
            return
        if features.enabled("archives") and formats.looks_like_archive(rec.name):
            pane.open_archive(path)
            return
        open_with_default(self, path)

    def open_default(self):
        p = self.pane
        for r in self._sel():
            if p.mode == "archive":
                self.preview_archive_item(p, r, external=True)
            elif r.path:
                open_with_default(self, r.path)

    def open_with_app(self, app_id: str):
        app = Gio.DesktopAppInfo.new(app_id) if app_id else None
        files = [Gio.File.new_for_path(p) for p in self._sel_paths()]
        if app is None or not files:
            return
        try:
            app.launch(files, self.get_display().get_app_launch_context())
        except GLib.Error as e:
            alert(self, tr("Impossible d'ouvrir"), e.message)

    def open_with_dialog(self):
        sel = [r for r in self._sel() if r.path]
        if not sel:
            return
        from .dialogs import AppChooser
        AppChooser(self, sel[0].content_type, [r.path for r in sel]).present(self)

    def open_in_tab(self):
        for r in self._sel():
            if r.is_dir:
                self.new_tab(r.path or r.uri, select=False)

    def open_in_other(self):
        dirs = [r for r in self._sel() if r.is_dir and r.path]
        if dirs:
            self.open_location(dirs[0].path, "other")

    def open_item_location(self):
        sel = self._sel()
        if sel and sel[0].path:
            self.pane.open(os.path.dirname(sel[0].path), select=[sel[0].name])

    def browse_archive(self):
        sel = self._sel()
        if sel and sel[0].path:
            self.pane.open_archive(sel[0].path)

    # ------------------------------------------------------------------
    # Presse-papiers
    # ------------------------------------------------------------------
    def clip(self, cut: bool):
        paths = self._sel_paths()
        if not paths:
            return
        clipboard.put(self.get_display(), paths, cut)
        n = len(paths)
        toast(self, (ntr("{0} élément coupé", "{0} éléments coupés", n) if cut
                     else ntr("{0} élément copié", "{0} éléments copiés", n)).format(n), timeout=2)

    def paste(self, dest: Optional[str] = None):
        dest = dest or (self.pane.current_dir() if self.pane and self.pane.mode == "fs" else "")
        if not dest:
            return

        def got(paths, cut):
            if not paths:
                toast(self, tr("Rien à coller"))
                return
            self.run_transfer(paths, dest, move=cut)
            if cut:
                clipboard.state.clear()
                self.get_display().get_clipboard().set_content(None)
        clipboard.read(self.get_display(), got)

    def paste_into(self):
        dirs = [r for r in self._sel() if r.is_dir and r.path]
        if dirs:
            self.paste(dirs[0].path)

    def transfer_to(self, move: bool):
        paths = self._sel_paths()
        if not paths:
            return
        other = self.other_pane()
        initial = other.current_dir() if other else os.path.dirname(paths[0])
        choose_folder(self, tr("Déplacer vers") if move else tr("Copier vers"), initial,
                      lambda d: self.run_transfer(paths, d, move=move))

    def to_other(self, move: bool):
        other = self.other_pane()
        paths = self._sel_paths()
        if other is None or not paths:
            return
        dest = other.current_dir()
        if not dest or other.mode != "fs":
            toast(self, tr("L'autre panneau n'affiche pas un dossier"))
            return
        if move:
            n = len(paths)
            alert(self, tr("Déplacer ?"),
                  ntr("Déplacer {name} vers {dst} ?", "Déplacer {n} éléments vers {dst} ?", n).format(
                      name=os.path.basename(paths[0]), n=n, dst=dest),
                  responses=(("cancel", tr("Annuler")), ("move", tr("Déplacer"))), suggested="move",
                  default="move", callback=lambda r: r == "move" and self.run_transfer(paths, dest, move=True))
        else:
            self.run_transfer(paths, dest, move=False)

    def drop_files(self, paths: list[str], dest: str, action: str):
        """Fichiers déposés sur un dossier, un segment du chemin ou la barre latérale."""
        if dest == "trash:///":
            self._trash_paths(paths)
            return
        if not dest or not os.path.isdir(dest):
            return
        paths = [p for p in paths if p != dest and os.path.dirname(p.rstrip("/")) != dest or action == "link"]
        if not paths:
            return
        if action == "auto":
            try:
                dev = os.stat(dest).st_dev
                action = "move" if all(os.lstat(p).st_dev == dev for p in paths) else "copy"
            except OSError:
                action = "copy"
        if action == "link":
            self._run(tr("Création de liens"), lambda j: fileops.make_link(j, paths, dest), dest)
            return
        self.run_transfer(paths, dest, move=(action == "move"))

    # ------------------------------------------------------------------
    # Exécution des opérations
    # ------------------------------------------------------------------
    def _ask_conflict(self, c: fileops.Conflict) -> tuple:
        """Appelée depuis le fil de la tâche : affiche la question et attend la réponse."""
        done = threading.Event()
        answer = {"v": ("cancel", False)}

        def show():
            self._conflict_dialog(c, lambda choice, all_: (answer.__setitem__("v", (choice, all_)), done.set()))
            return False
        GLib.idle_add(show)
        done.wait()
        return answer["v"]

    def _conflict_dialog(self, c: fileops.Conflict, reply):
        name = os.path.basename(c.dst)
        merge = c.src_is_dir and c.dst_is_dir
        heading = tr("Fusionner le dossier « {0} » ?").format(name) if merge else \
            tr("Remplacer « {0} » ?").format(name)

        def describe(path):
            try:
                s = os.lstat(path)
            except OSError:
                return ""
            size = tr("dossier") if os.path.isdir(path) else human_size(s.st_size)
            return f"{size} · {full_date(s.st_mtime)}"
        body = (tr("Un élément du même nom existe déjà dans « {0} ».").format(os.path.dirname(c.dst)) + "\n\n" +
                tr("Existant : {0}").format(describe(c.dst)) + "\n" + tr("Nouveau : {0}").format(describe(c.src)))
        d = Adw.AlertDialog.new(heading, body)
        check = Gtk.CheckButton(label=tr("Appliquer à tous les conflits"))
        d.set_extra_child(check)
        d.add_response("cancel", tr("Annuler"))
        d.add_response("skip", tr("Ignorer"))
        d.add_response("rename", tr("Garder les deux"))
        d.add_response("merge" if merge else "replace", tr("Fusionner") if merge else tr("Remplacer"))
        d.set_response_appearance("merge" if merge else "replace",
                                  Adw.ResponseAppearance.SUGGESTED if merge else Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")
        d.set_default_response("rename")
        d.connect("response", lambda _d, r: reply(r, check.get_active()))
        d.present(self)

    def _watch_progress(self, job: Job):
        job.file_operation = True
        if features.enabled("progress_window"):
            if self.progress_window is None:
                self.progress_window = ProgressWindow(self)
            self.progress_window.watch(job, float(settings["progress_window_delay"]))

    def _run(self, title: str, func, refresh_dir: Optional[str] = None, on_done=None, undo=None):
        def done(job):
            if job.state == "done":
                res = job.result
                if undo is not None and isinstance(res, fileops.Result):
                    entry = undo(res)
                    if entry:
                        self.undo_stack.append(entry)
                        del self.undo_stack[:-30]
                if isinstance(res, fileops.Result):
                    msg = fileops.describe_result(res)
                    if res.errors:
                        msg += " — " + ntr("{0} erreur", "{0} erreurs", len(res.errors)).format(len(res.errors))
                        self._show_errors(job.title, res.errors)
                    if res.kind in ("trash", "move") and undo is not None:
                        toast(self, msg, tr("Annuler"), self.undo, timeout=6)
                    elif res.kind not in ("copy", "move"):
                        toast(self, msg)
                    self._select_created(res)
                if on_done:
                    on_done(job)
            else:
                finished_toast(self, job, "")
        job = manager.submit(title, func, done)
        self._watch_progress(job)
        return job

    def _show_errors(self, title, errors):
        text = "\n".join(f"{os.path.basename(p)} : {m}" for p, m in errors[:15])
        if len(errors) > 15:
            text += "\n" + tr("… et {0} autre(s)").format(len(errors) - 15)
        alert(self, tr("{0} : certains éléments n'ont pas pu être traités").format(title), text)

    def _select_created(self, res: fileops.Result):
        if not res.created or res.kind in ("trash", "delete"):
            return
        folder = os.path.dirname(res.created[0])
        for p in self.all_panes():
            if p.mode == "fs" and p.location == folder:
                names = [os.path.basename(c) for c in res.created if os.path.dirname(c) == folder]
                p.model.connect_once_ready(lambda p=p, n=names: p.select_names(n)) if p.model.loading \
                    else GLib.timeout_add(400, lambda p=p, n=names: (p.select_names(n), False)[1])

    def run_transfer(self, paths: list[str], dest: str, move: bool = False):
        n = len(paths)
        title = (ntr("Déplacement de {0} élément", "Déplacement de {0} éléments", n) if move else
                 ntr("Copie de {0} élément", "Copie de {0} éléments", n)).format(n) + " → " + \
            (os.path.basename(dest.rstrip("/")) or dest)
        undo = (lambda r: ("move", list(r.moved)) if move and r.moved else ("copy", list(r.created))
                if r.created else None)
        self._run(title, lambda j: fileops.transfer(j, paths, dest, self._ask_conflict, move=move), dest,
                  undo=undo)

    # ------------------------------------------------------------------
    # Renommer, créer
    # ------------------------------------------------------------------
    def ask_name(self, heading: str, initial: str, action: str, callback, folder: str = "",
                 select_stem: bool = True, current: str = ""):
        """Saisie d'un nom avec vérification immédiate (vide, « / », déjà existant)."""
        row = Adw.EntryRow(title=tr("Nom"), text=initial)
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(row)
        msg = Gtk.Label(xalign=0, css_classes=["error", "caption"], margin_top=6, visible=False, wrap=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(box)
        col.append(msg)
        d = Adw.AlertDialog.new(heading, None)
        d.add_response("cancel", tr("Annuler"))
        d.add_response("ok", action)
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        d.set_extra_child(col)

        def problem(text: str) -> str:
            err = fileops.valid_name(text)
            if err:
                return err
            if folder and text != current and os.path.lexists(os.path.join(folder, text)):
                return tr("« {0} » existe déjà").format(text)
            return ""

        def check(*_):
            err = problem(row.get_text())
            msg.set_text(err)
            msg.set_visible(bool(err) and bool(row.get_text()))
            d.set_response_enabled("ok", not err)
        row.connect("changed", check)
        state = {"done": False}

        def finish(ok):
            if state["done"]:
                return
            if ok and problem(row.get_text()):
                return
            state["done"] = True
            if ok and row.get_text() != current:
                callback(row.get_text())

        def activated(*_):
            if not problem(row.get_text()):
                finish(True)
                d.close()
        row.connect("entry-activated", activated)
        d.connect("response", lambda _d, r: finish(r == "ok"))
        check()
        d.present(self)

        def focus():
            row.grab_focus()
            if select_stem and initial:
                stem, _ext = fileops.split_ext(initial)
                row.select_region(0, len(stem))
            return False
        GLib.idle_add(focus)

    def rename(self):
        p = self.pane
        sel = self._sel()
        if len(sel) != 1:
            return
        rec = sel[0]
        if p.mode == "archive":
            self.rename_in_archive(p, rec)
            return
        if not rec.path:
            return
        folder = os.path.dirname(rec.path)

        def apply(new):
            try:
                target = fileops.rename(rec.path, new)
            except (OSError, Exception) as e:  # noqa: BLE001
                alert(self, tr("Renommage impossible"), getattr(e, "strerror", None) or str(e))
                return
            self.undo_stack.append(("rename", rec.path, target))
            p.model.connect_once_ready(lambda: p.select_names([new])) if p.model.loading else \
                GLib.timeout_add(300, lambda: (p.select_names([new]), False)[1])
        self.ask_name(tr("Renommer"), rec.name, tr("Renommer"), apply, folder, select_stem=not rec.is_dir,
                      current=rec.name)

    def new_folder(self):
        p = self.pane
        if p.mode == "archive":
            self.new_folder_in_archive(p)
            return
        folder = p.current_dir()
        if not folder:
            return

        def make(name):
            try:
                path = fileops.make_folder(folder, name)
            except Exception as e:  # noqa: BLE001
                alert(self, tr("Création impossible"), getattr(e, "strerror", None) or str(e))
                return
            self.undo_stack.append(("create", [path]))
            GLib.timeout_add(300, lambda: (p.select_names([name]), False)[1])
        self.ask_name(tr("Nouveau dossier"), fileops.unique_name(folder, tr("Nouveau dossier"), True),
                      tr("Créer"), make, folder, select_stem=False)

    def new_file_named(self, template: str = ""):
        p = self.pane
        folder = p.current_dir() if p.mode == "fs" else ""
        if not folder:
            return
        initial = os.path.basename(template) if template else tr("Nouveau document.txt")
        initial = fileops.unique_name(folder, initial)

        def make(name):
            try:
                path = fileops.make_file(folder, name, template)
            except Exception as e:  # noqa: BLE001
                alert(self, tr("Création impossible"), getattr(e, "strerror", None) or str(e))
                return
            self.undo_stack.append(("create", [path]))
            GLib.timeout_add(300, lambda: (p.select_names([name]), False)[1])
        self.ask_name(tr("Nouveau document"), initial, tr("Créer"), make, folder)

    def new_from_template(self, path: str):
        self.new_file_named(path)

    def make_link(self):
        paths = self._sel_paths()
        folder = self.pane.current_dir()
        if paths and folder:
            self._run(tr("Création de liens"), lambda j: fileops.make_link(j, paths, folder), folder,
                      undo=lambda r: ("create", list(r.created)))

    # ------------------------------------------------------------------
    # Corbeille et suppression
    # ------------------------------------------------------------------
    def trash(self):
        p = self.pane
        if p.mode == "archive":
            self.delete_in_archive(p)
            return
        if p.mode == "trash":
            self.delete()
            return
        paths = self._sel_paths()
        if not paths:
            return
        if settings["confirm_trash"]:
            alert(self, tr("Mettre à la corbeille ?"), describe_paths(paths),
                  responses=(("cancel", tr("Annuler")), ("trash", tr("Mettre à la corbeille"))),
                  destructive="trash", default="trash", callback=lambda r: r == "trash" and self._trash_paths(paths))
        else:
            self._trash_paths(paths)

    def _trash_paths(self, paths: list[str]):
        def undo(res):
            return ("trash", list(res.trashed)) if res.trashed else None
        self._run(ntr("Mise à la corbeille de {0} élément", "Mise à la corbeille de {0} éléments",
                      len(paths)).format(len(paths)), lambda j: fileops.trash(j, paths), undo=undo)

    def delete(self):
        p = self.pane
        if p.mode == "archive":
            self.delete_in_archive(p)
            return
        sel = self._sel()
        if not sel:
            return
        if p.mode == "trash":
            uris = [r.uri for r in sel if r.uri]

            def go_trash(r):
                if r != "delete":
                    return

                def work(j):
                    res = fileops.Result("delete")
                    for u in uris:
                        j.check_cancel()
                        try:
                            _delete_gio(Gio.File.new_for_uri(u))
                            res.created.append(u)
                        except GLib.Error as e:
                            res.errors.append((u, e.message))
                    return res
                self._run(tr("Suppression définitive"), work, on_done=lambda j: p.refresh())
            alert(self, tr("Supprimer définitivement ?"),
                  tr("{0} sera supprimé sans possibilité de retour.").format(describe_paths([r.name for r in sel])),
                  responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), destructive="delete",
                  default="cancel", callback=go_trash)
            return
        paths = [r.path for r in sel if r.path]
        if not paths:
            return

        def go(r):
            if r == "delete":
                self._run(tr("Suppression définitive"), lambda j: fileops.delete(j, paths))
        if settings["confirm_delete"]:
            n = len(paths)
            alert(self, tr("Supprimer définitivement ?"),
                  ntr("« {name} » sera supprimé définitivement. Cette action est irréversible.",
                      "Ces {n} éléments seront supprimés définitivement. Cette action est irréversible.",
                      n).format(name=os.path.basename(paths[0]), n=n),
                  responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), destructive="delete",
                  default="cancel", callback=go)
        else:
            go("delete")

    def restore(self):
        p = self.pane
        uris = [r.uri for r in self._sel() if r.uri]
        if p.mode == "trash" and uris:
            self._run(tr("Restauration"), lambda j: fileops.restore_from_trash(j, uris), on_done=lambda j: p.refresh())

    def empty_trash(self):
        def go(r):
            if r == "empty":
                def done(job):
                    for p in self.all_panes():
                        if p.mode == "trash":
                            p.refresh()
                    toast(self, tr("Corbeille vidée"))
                self._run(tr("Vidage de la corbeille"), fileops.empty_trash, on_done=done)
        alert(self, tr("Vider la corbeille ?"), tr("Tous les éléments de la corbeille seront définitivement supprimés."),
              responses=(("cancel", tr("Annuler")), ("empty", tr("Vider la corbeille"))), destructive="empty",
              default="cancel", callback=go)

    # ------------------------------------------------------------------
    # Annuler
    # ------------------------------------------------------------------
    def undo(self):
        if not self.undo_stack:
            toast(self, tr("Rien à annuler"))
            return
        entry = self.undo_stack.pop()
        kind = entry[0]
        if kind == "rename":
            _k, old, new = entry
            try:
                os.rename(new, old)
                toast(self, tr("Renommage annulé"))
            except OSError as e:
                alert(self, tr("Annulation impossible"), e.strerror or str(e))
        elif kind == "move":
            pairs = entry[1]

            def work(j):
                res = fileops.Result("move")
                for src, dst in pairs:
                    j.check_cancel()
                    try:
                        if os.path.lexists(src):
                            raise OSError(17, tr("« {0} » existe déjà").format(os.path.basename(src)))
                        shutil.move(dst, src)
                        res.created.append(src)
                    except OSError as e:
                        res.errors.append((dst, e.strerror or str(e)))
                return res
            self._run(tr("Annulation du déplacement"), work)
        elif kind == "trash":
            originals = entry[1]
            self._run(tr("Restauration depuis la corbeille"), lambda j: fileops.restore_trashed_paths(j, originals))
        elif kind in ("copy", "create"):
            paths = [p for p in entry[1] if os.path.lexists(p)]
            if paths:
                self._run(tr("Annulation : mise à la corbeille"), lambda j: fileops.trash(j, paths))

    # ------------------------------------------------------------------
    # Divers
    # ------------------------------------------------------------------
    def copy_path(self):
        p = self.pane
        if p.mode == "archive":
            sel = [p.archive_path + "/" + r.get("inner", r.name) for r in self._sel()]
        else:
            sel = [r.path or r.uri for r in self._sel()]
        text = "\n".join(s for s in sel if s) or p.location_text()
        copy_to_clipboard(self, text)
        toast(self, tr("Chemin copié"), timeout=2)

    def copy_location(self):
        copy_to_clipboard(self, self.pane.location_text())
        toast(self, tr("Emplacement copié"), timeout=2)

    def properties(self):
        sel = [r for r in self._sel() if r.path]
        if sel:
            self._show_properties([r.path for r in sel])
        else:
            self.properties_folder()

    def properties_folder(self):
        d = self.pane.current_dir() if self.pane else ""
        if d:
            self._show_properties([d])

    def _show_properties(self, paths: list[str]):
        from .properties import PropertiesDialog
        PropertiesDialog(self, paths).present(self)

    def _launch_terminal(self, folder: str):
        term = find_terminal()
        if not term:
            alert(self, tr("Aucun terminal trouvé"), tr("Indiquez votre terminal dans les préférences."))
            return
        argv = term.split()
        name = os.path.basename(argv[0])
        if name == "gnome-terminal":
            argv += ["--working-directory", folder]
        try:
            subprocess.Popen(argv, cwd=folder, start_new_session=True)
        except OSError as e:
            alert(self, tr("Impossible de lancer le terminal"), str(e))

    def terminal_here(self):
        if not features.enabled("terminal"):
            return
        d = self.pane.current_dir()
        if d:
            self._launch_terminal(d)

    def terminal_selected(self):
        dirs = [r.path for r in self._sel() if r.is_dir and r.path]
        if dirs:
            self._launch_terminal(dirs[0])

    def edit_text(self):
        paths = [r.path for r in self._sel() if r.path and not r.is_dir]
        if not paths:
            return
        editor = find_editor()
        if editor:
            try:
                subprocess.Popen(editor.split() + paths, start_new_session=True)
                return
            except OSError:
                pass
        for p in paths:
            open_with_default(self, p)

    # ------------------------------------------------------------------
    # Couleurs et emblèmes (Folder Color Revival)
    # ------------------------------------------------------------------
    def _refresh_meta(self, paths: list[str]):
        wanted = set(paths)
        for pane in self.all_panes():
            for r in pane.model.items:
                if r.path in wanted and r.extra:
                    r.extra.pop("meta", None)
                    pane.model.notify_rec(r)
            if pane.miller is not None:
                pane.miller.refresh_rows()

    def folder_color(self, color: str):
        from .icons import has_icon
        paths = [r.path for r in self._sel() if r.is_dir and r.path]
        for p in paths:
            folder_colors.set_color(p, color, has_icon)
        self._refresh_meta(paths)

    def emblem(self, emblem: str):
        paths = self._sel_paths()
        for p in paths:
            folder_colors.toggle_emblem(p, emblem)
        self._refresh_meta(paths)

    def folder_restore(self):
        paths = self._sel_paths()
        for p in paths:
            folder_colors.restore(p)
        self._refresh_meta(paths)


def _delete_gio(f: Gio.File):
    info = f.query_info("standard::type", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
    if info.get_file_type() == Gio.FileType.DIRECTORY:
        for ci in f.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None):
            _delete_gio(f.get_child(ci.get_name()))
    f.delete(None)
