"""Actions sur les fichiers de la fenêtre principale."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk

from .. import features
from ..backend import batchrename, fileops, folder_colors, formats, fsinfo, gioops, scanner, starred
from ..backend.engine import Job, ToolError, human_size
from ..i18n import ntr, tr
from ..settings import settings
from . import clipboard
from .batch_rename import BatchRenameDialog
from .common import (AlertDialog, alert, choose_folder, copy_to_clipboard, describe_paths, full_date,
                     launch_default, launch_defaults, name_order, open_with_default, shown, toast)
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


def terminal_command(term: str, command: list) -> list:
    """Ligne de commande qui fait exécuter `command` par le terminal `term` : chacun a sa syntaxe."""
    argv = term.split()
    name = os.path.basename(argv[0])
    if name in ("gnome-terminal", "kgx", "ptyxis"):
        return argv + ["--"] + command
    if name == "kitty":
        return argv + command
    if name in ("xfce4-terminal", "mate-terminal", "terminator"):
        return argv + ["-x"] + command
    if name == "lxterminal":
        return argv + ["-e", shlex.join(command)]
    return argv + ["-e"] + command          # x-terminal-emulator (Debian / Ubuntu), xterm, konsole, tilix…


# Programmes binaires : lancés directement au double-clic, comme Nautilus 46 (un script texte, lui, s'ouvre
# dans l'éditeur ; « Exécuter comme un programme » le lance). Les exécutables « PIE » d'aujourd'hui sont
# application/x-pie-executable (ou x-sharedlib dans les vieilles bases MIME), que GLib ne dit pas
# « exécutables » : ils sont comptés quand même.
BINARY_TYPES = ("application/x-executable", "application/x-pie-executable", "application/x-sharedlib",
                "application/vnd.appimage", "application/x-appimage")


def _is_binary_type(ctype: str) -> bool:
    return any(Gio.content_type_is_a(ctype, t) for t in BINARY_TYPES) and not Gio.content_type_is_a(ctype, "text/plain")


def can_run(rec) -> bool:
    """Comme Nautilus : script ou programme exécutable (pas un .desktop, lancé autrement). D'après le nom
    seulement (aucun accès disque dans le menu) : un exécutable au type inconnu (programme sans extension,
    vu comme « octet-stream ») est compté."""
    if not (rec is not None and rec.path and not rec.is_dir and rec.is_executable
            and not rec.name.endswith(".desktop")):
        return False
    ctype = rec.content_type
    return bool(Gio.content_type_can_be_executable(ctype) or _is_binary_type(ctype)
                or Gio.content_type_is_unknown(ctype))


def launches_directly(rec) -> bool:
    """Double-clic : programme binaire exécutable (ELF, AppImage), lancé directement comme dans Nautilus 46
    (retour d'un utilisateur). Programme sans extension : type lu dans le contenu (4 Ko), seulement sur un
    disque local — aucun appel système sur un chemin réseau depuis le fil principal."""
    if not can_run(rec) or fsinfo.is_network(rec.path):
        return False
    ctype = rec.content_type
    if _is_binary_type(ctype):
        return True
    if not Gio.content_type_is_unknown(ctype):
        return False                    # script texte, document… : l'application par défaut
    try:
        with open(rec.path, "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return False
    return _is_binary_type(Gio.content_type_guess(rec.name, data)[0] or "")


NAME_DIALOG_MIN = 420     # « Nouveau dossier », noms courts : un peu plus large que la boîte d'origine
NAME_DIALOG_MAX = 760


def name_dialog_width(widget: Gtk.Widget, text: str) -> int:
    """Largeur de la boîte « Renommer » / « Nouveau dossier » : le nom entier lisible dans le champ, comme la
    bulle de Nautilus (remarque d'un utilisateur : Adw.MessageDialog seul fait ~370 px, un nom long y était
    coupé aux deux bouts). Mesuré dans la police du champ ; marges de la boîte (2 × 24), du champ (2 × 12),
    bouton crayon, plus une marge pour taper."""
    try:
        w = widget.create_pango_layout(text).get_pixel_size()[0]
    except (TypeError, AttributeError):
        w = 8 * len(text)
    return max(NAME_DIALOG_MIN, min(NAME_DIALOG_MAX, w + 150))


def find_editor() -> Optional[str]:
    if settings["text_editor"] and shutil.which(settings["text_editor"].split()[0]):
        return settings["text_editor"]
    for e in EDITORS:
        if shutil.which(e):
            return e
    return None




def _desktop_app_for(command: str):
    """Application installée correspondant à une commande (pour lancer l'éditeur par URI)."""
    if not command:
        return None
    exe = os.path.basename(command.split()[0])
    for app in Gio.AppInfo.get_all():
        if app.get_executable() and os.path.basename(app.get_executable()) == exe:
            return app
    return None
def _case_only(folder: str, text: str, same: Optional[str]) -> bool:
    """Renommer en ne changeant que la casse sur un support insensible à la casse (FAT, exFAT) :
    « Photo.jpg » « existe » parce que c'est le fichier lui-même. Seulement si aucun élément ne porte
    exactement ce nom (sur ext4, « Photo.jpg » et « photo.jpg » peuvent être deux fichiers)."""
    if not same or text.casefold() != same.casefold():
        return False
    try:
        return text not in os.listdir(folder)
    except OSError:
        return False


def _put_back_replaced(job, res, replaced: dict):
    """Annulation d'un « Remplacer » : les éléments remplacés sortent de la corbeille, à leur place
    (le nouvel élément vient d'en partir)."""
    if not replaced:
        return
    back = fileops.restore_trashed_uris(job, replaced, partial_ok=True)
    res.errors += back.errors


class FileActions:
    """Mélangé à MainWindow."""

    def setup_file_actions(self):
        acts = {
            "open": self.open_selected, "open-default": self.open_default, "open-with": self.open_with_dialog,
            "open-tab": self.open_in_tab, "open-other": self.open_in_other, "open-window": self.open_in_window, "open-location": self.open_item_location,
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
            "run-program": self.run_program, "email": self.email,
            "edit-text": self.edit_text, "undo": self.undo, "redo": self.redo,
            "open-admin": self.open_admin, "open-admin-folder": self.open_admin_folder,
            "folder-restore": self.folder_restore, "browse-archive": self.browse_archive,
            "share-folder": self.share_folder, "star": self.star_selected, "unstar": self.unstar_selected,
        }
        for name, cb in acts.items():
            self._add(name, cb)
        self._add("open-with-app", self.open_with_app, "s")
        self._add("send-to-device", self.send_to_device, "s")
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
            "win.open-tab": ["<Ctrl>Return"], "win.undo": ["<Ctrl>z"], "win.redo": ["<Ctrl><Shift>z"],
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

    def _gio_dir(self) -> str:
        """Emplacement GVfs sans chemin local (admin://…) : les opérations passent par GIO."""
        p = self.pane
        return p.location if p is not None and p.mode == "remote" and not p.current_dir() else ""

    def _sel_locs(self) -> list[str]:
        """Sélection en chemins, ou en URI pour les éléments sans chemin local."""
        return [r.path or r.uri for r in self._sel() if r.path or r.uri]

    def _run_gio(self, title: str, func):
        p = self.pane

        def done(job):
            if isinstance(job.result, fileops.Result) and job.result.errors:
                self._show_errors(job.title, job.result.errors)
            for pane in self.all_panes():
                if pane.mode == "remote":
                    pane.model.reload_quiet()
            if p is not None and p.mode == "fs":
                p.refresh()
        self._run(title, func, on_done=done)

    def _sel_gfiles(self, files_only: bool = True) -> list:
        """Sélection en fichiers GIO : par le chemin, ou par l'URI (admin://, smb://… sans chemin)."""
        out = []
        for r in self._sel():
            if files_only and r.is_dir:
                continue
            if r.path:
                out.append(Gio.File.new_for_path(r.path))
            elif r.uri:
                out.append(Gio.File.new_for_uri(r.get("target_uri") or r.uri))
        return out

    def _launch_app(self, app, files) -> bool:
        from .dialogs import launch_app
        try:
            launch_app(app, files, self.get_display().get_app_launch_context())
            return True
        except GLib.Error as e:
            alert(self, tr("Impossible d'ouvrir"), e.message)
            return False

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
        files = []
        for r in sel:
            if r.is_dir:
                continue
            if p.mode in ("archive", "trash") or (r.path and features.enabled("archives")
                                                  and formats.looks_like_archive(r.name)):
                self._on_activated(p, r)
            else:
                files.append(r)
        self._open_files(files)

    def _open_files(self, recs: list, activate: bool = True):
        """Plusieurs fichiers : un seul lancement par application par défaut, dans l'ordre des noms
        (6 vidéos → une seule fenêtre de VLC, les 6 dans sa liste de lecture, (1) à (6)).
        activate=False (« Ouvrir avec <application> ») : toujours l'application, même pour une
        archive qu'Odysseus ouvrirait lui-même."""
        if len(recs) == 1 and activate:
            self._on_activated(self.pane, recs[0])
            return
        if activate:                     # programmes de la sélection : lancés, chacun le sien
            for r in [r for r in recs if launches_directly(r)]:
                self.launch_program(r)
                recs = [x for x in recs if x is not r]
        broken = [r for r in recs if r.is_broken_link]
        if broken:                       # comme Nautilus : le lien cassé est signalé, le reste s'ouvre
            self.report_broken_link(broken[0])
            recs = [r for r in recs if not r.is_broken_link]
        gfiles = [Gio.File.new_for_path(r.path) if r.path else Gio.File.new_for_uri(r.get("target_uri") or r.uri)
                  for r in recs if r.path or r.uri]
        for f in launch_defaults(self, gfiles):
            if f.get_path():
                open_with_default(self, f.get_path())
            else:
                self._open_uri(f.get_uri())

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
                self._open_uri(uri)
            return
        if rec.is_broken_link:
            self.report_broken_link(rec)
            return
        if features.enabled("archives") and formats.looks_like_archive(rec.name):
            self.browse_archive_path(path, pane)
            return
        if launches_directly(rec):
            self.launch_program(rec)
            return
        open_with_default(self, path)

    def launch_program(self, rec):
        """Programme lancé comme le fait Nautilus : sans terminal, depuis son dossier, détaché d'Odysseus."""
        try:
            subprocess.Popen([rec.path], cwd=os.path.dirname(rec.path), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            alert(self, tr("Impossible de lancer « {0} »").format(rec.name), e.strerror or str(e))

    def report_broken_link(self, rec):
        """Lien cassé ouvert : comme Nautilus (report_broken_symbolic_link), proposer la corbeille."""
        try:
            target = os.readlink(rec.path)
        except OSError:
            target = ""
        detail = (tr("Impossible d’utiliser ce lien car sa cible « {0} » n’existe pas.").format(target) if target
                  else tr("Impossible d’utiliser ce lien car il ne pointe sur rien."))
        if self.pane.mode == "fs":
            alert(self, tr("Le lien « {0} » est cassé. Voulez-vous le mettre à la corbeille ?").format(rec.name),
                  detail, responses=(("cancel", tr("Annuler")), ("trash", tr("Mettre à la corbeille"))),
                  default="cancel", callback=lambda r: r == "trash" and self._trash_paths([rec.path]))
        else:
            alert(self, tr("Le lien « {0} » est rompu.").format(rec.name), detail,
                  responses=(("cancel", tr("Annuler")),), default="cancel")

    def open_default(self):
        p = self.pane
        if p.mode == "archive":
            for r in self._sel():
                self.preview_archive_item(p, r, external=True)
            return
        self._open_files([r for r in self._sel() if r.path or r.uri], activate=False)

    def _open_uri(self, uri: str):
        f = Gio.File.new_for_uri(uri)
        if not launch_default(self, f):
            Gtk.FileLauncher.new(f).launch(self, None, None)

    def open_with_app(self, app_id: str):
        app = Gio.DesktopAppInfo.new(app_id) if app_id else None
        files = name_order(self._sel_gfiles())
        if app is not None and files:
            self._launch_app(app, files)

    def open_with_dialog(self):
        sel = [r for r in self._sel() if not r.is_dir]
        files = name_order(self._sel_gfiles())
        if not files:
            return
        from .dialogs import AppChooser
        AppChooser(self, sel[0].content_type, files).present(self)

    def open_in_tab(self):
        for r in self._sel():
            if r.is_dir:
                self.new_tab(r.path or r.uri, select=False)

    def open_in_window(self):
        for r in self._sel():
            if r.is_dir:
                self.open_location(r.path or r.uri, "window")

    def open_in_other(self):
        dirs = [r for r in self._sel() if r.is_dir and r.path]
        if dirs:
            self.open_location(dirs[0].path, "other")

    def open_item_location(self):
        sel = self._sel()
        if sel and sel[0].path:
            self.pane.open(os.path.dirname(sel[0].path), select=[sel[0].name])

    def star_selected(self):
        paths = [r.path for r in self._sel() if r.path and starred.can_star(r.path)]
        if paths and features.enabled("starred"):
            starred.star(paths)

    def unstar_selected(self):
        """« Démarquer », avec la notification de Nautilus et « Annuler » (marquées de nouveau)."""
        paths = [r.path for r in self._sel() if r.path and starred.cached_starred(r.path)]
        if not paths:
            return
        starred.unstar(paths)
        msg = (tr("« {0} » est démarqué").format(os.path.basename(paths[0])) if len(paths) == 1 else
               ntr("{0} fichier démarqué", "{0} fichiers démarqués", len(paths)).format(len(paths)))
        toast(self, msg, tr("Annuler"), lambda: starred.star(paths), timeout=6)

    def share_folder(self):
        """Options de partage (Samba) du dossier sélectionné, comme nautilus-share."""
        sel = self._sel()
        if not features.available("samba_share") or len(sel) != 1 or not sel[0].is_dir or not sel[0].path:
            return
        from .share_dialog import ShareDialog
        ShareDialog(self, sel[0].path).present(self)

    def browse_archive(self):
        sel = self._sel()
        if sel and sel[0].path:
            self.browse_archive_path(sel[0].path)

    def browse_archive_path(self, path: str, pane=None):
        """Parcourir une archive (double-clic, menu, F7) : dans sa propre fenêtre, comme Archive Browser
        (réglage archive_window), sinon dans le panneau."""
        if settings["archive_window"]:
            from .archive_window import open_archive_window
            return open_archive_window(self.get_application(), path, self)
        (pane or self.pane).open_archive(path)
        return None

    # ------------------------------------------------------------------
    # Presse-papiers
    # ------------------------------------------------------------------
    def clip(self, cut: bool):
        paths = self._sel_locs() if self._gio_dir() else self._sel_paths()
        if not paths:
            return
        clipboard.put(self.get_display(), paths, cut)
        n = len(paths)
        toast(self, (ntr("{0} élément coupé", "{0} éléments coupés", n) if cut
                     else ntr("{0} élément copié", "{0} éléments copiés", n)).format(n), timeout=2)

    def paste(self, dest: Optional[str] = None):
        # « remote » avec un chemin FUSE (smb://… ouvert par son URI) : on colle dans ce chemin ; sans
        # chemin (Google Drive, admin://) : par GIO
        dest = dest or (self.pane.current_dir() if self.pane and self.pane.mode in ("fs", "remote") else "") \
            or self._gio_dir()
        if not dest:
            return

        def got(paths, cut):
            if not paths:
                toast(self, tr("Rien à coller"))
                return
            if cut and "://" not in dest and all(os.path.dirname(os.path.abspath(p)) == os.path.abspath(dest)
                                                 for p in paths):
                # couper puis coller au même endroit : rien ne bouge, le presse-papiers est gardé
                toast(self, tr("Les éléments sont déjà dans ce dossier"))
                return
            if "://" in dest or any("://" in p for p in paths):
                # admin://… : copie par GIO (jamais d'écrasement, « (copie) » en cas de doublon)
                n = len(paths)
                title = (ntr("Déplacement de {0} élément", "Déplacement de {0} éléments", n) if cut else
                         ntr("Copie de {0} élément", "Copie de {0} éléments", n)).format(n)
                self._run_gio(title, lambda j: gioops.transfer(j, paths, dest, move=cut))
            else:
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
            paths = [p for p in paths if "://" not in p]     # Google Drive : pas notre corbeille
            if paths:
                self._trash_paths(paths)
            return
        if dest and "://" in dest and not fsinfo.is_gio_only_uri(dest):
            return          # Récents, Favoris, Ce PC, Réseau, admin:/// de la barre de chemin : rien à y déposer
        if dest and ("://" in dest or any("://" in p for p in paths)):
            # Google Drive / OneDrive (source ou destination) : par GIO, sous leurs vrais noms
            paths = [p for p in paths if p != dest and p.rstrip("/").rsplit("/", 1)[0] != dest.rstrip("/")]
            if not paths or action == "link" or ("://" not in dest and not os.path.isdir(dest)):
                return
            move = action == "move"
            n = len(paths)
            title = (ntr("Déplacement de {0} élément", "Déplacement de {0} éléments", n) if move else
                     ntr("Copie de {0} élément", "Copie de {0} éléments", n)).format(n)
            self._run_gio(title, lambda j: gioops.transfer(j, paths, dest, move=move))
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
        # le nom du dossier, comme Nautilus : un chemin complet d'un seul tenant ne passe pas à la ligne et
        # faisait déborder la fenêtre (titre et boutons coupés, vu chez Tof)
        folder = os.path.basename(os.path.dirname(c.dst).rstrip("/")) or os.path.dirname(c.dst)
        body = (tr("Un élément du même nom existe déjà dans « {0} ».").format(folder) + "\n\n" +
                tr("Existant : {0}").format(describe(c.dst)) + "\n" + tr("Nouveau : {0}").format(describe(c.src)))
        d = AlertDialog.new(heading, body)
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
        pw = self.progress_window
        if pw is not None:
            pw.question_opened()

        def answered(_d, r):
            if pw is not None:
                pw.question_closed()
            reply(r, check.get_active())
        d.connect("response", answered)
        d.present(self)

    def _watch_progress(self, job: Job):
        job.file_operation = True
        if features.enabled("progress_window"):
            if self.progress_window is None:
                self.progress_window = ProgressWindow(self)
            self.progress_window.watch(job, float(settings["progress_window_delay"]))

    def _run(self, title: str, func, refresh_dir: Optional[str] = None, on_done=None, undo=None):
        def done(job):
            self._refresh_devices()      # même annulée ou en erreur : une partie a peut-être changé
            if job.state == "cancelled" and undo is not None and isinstance(job.partial, fileops.Result):
                # « Annuler » pendant l'opération : ce qui a déjà été fait reste annulable par Ctrl+Z
                # (avant : aucune entrée, et Ctrl+Z défaisait l'opération d'AVANT)
                entry = undo(job.partial)
                if entry:
                    self._push_undo(entry)
            if job.state == "done":
                res = job.result
                pushed = False
                entry = None
                if undo is not None and isinstance(res, fileops.Result):
                    entry = undo(res)
                    if entry:
                        self._push_undo(entry)
                        pushed = True
                if isinstance(res, fileops.Result):
                    msg = fileops.describe_result(res)
                    if res.errors:
                        msg += " — " + ntr("{0} erreur", "{0} erreurs", len(res.errors)).format(len(res.errors))
                        self._show_errors(job.title, res.errors)
                    # « Annuler » seulement si une entrée vient d'être posée : sinon il annulerait
                    # l'opération d'avant (étiquettes toutes en échec, sauvegarde impossible…)
                    if res.kind in ("trash", "move", "rename", "tags", "photo") and pushed:
                        toast(self, msg, tr("Annuler"), lambda e=entry: self._undo_entry(e), timeout=6)
                    elif res.kind not in ("copy", "move"):
                        toast(self, msg)
                    self._select_created(res)
                    if res.kind in ("move", "rename"):
                        self._follow_moves(res)
                    if res.kind in ("trash", "delete", "restore", "move"):
                        self.sidebar.refresh_trash_count()     # corbeille d'un disque créée à l'instant
                if on_done:
                    on_done(job)
            else:
                finished_toast(self, job, "")
        job = manager.submit(title, func, done)
        self._watch_progress(job)
        return job

    def _show_errors(self, title, errors):
        def name(p):        # URI Google Drive : vrai nom s'il a été lu (sinon l'identifiant)
            return (scanner.display_name(p) if "://" in p else None) or os.path.basename(p.rstrip("/")) or p
        text = "\n".join(f"{name(p)} : {m}" for p, m in errors[:15])
        if len(errors) > 15:
            text += "\n" + tr("… et {0} autre(s)").format(len(errors) - 15)
        alert(self, tr("{0} : certains éléments n'ont pas pu être traités").format(title), text)

    def _follow_moves(self, res: fileops.Result):
        if res.moved and starred.available():
            starred.moved(list(res.moved))          # comme Nautilus : l'étoile suit l'élément
        for src, dst in res.moved:
            # partage réseau : pas de stat par élément sur le fil principal (300 photos = 300 allers-retours) ;
            # follow_move ne fait rien pour un fichier, on le laisse en juger
            if fsinfo.is_network(dst) or "://" in dst or os.path.isdir(dst):
                for p in self.all_panes():
                    p.follow_move(src, dst)

    def _follow_rename(self, old: str, new: str):
        """Annuler / rétablir un renommage de dossier : les panneaux ouverts dedans le suivent."""
        if starred.available():
            starred.moved([(old, new)])
        if os.path.isdir(new):
            for p in self.all_panes():
                p.follow_move(old, new)

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
        # seulement ce qui a été créé / déplacé : dans un dossier fusionné, pas ce qu'il contenait déjà
        # un déplacement ne s'annule jamais comme une copie (corbeille) : ses fichiers n'existent plus
        # qu'à la destination
        # 3e élément : éléments remplacés (à la corbeille), remis à leur place par l'annulation
        undo = (lambda r: (("move", list(r.new_moves), dict(r.replaced)) if r.new_moves else None) if move else
                (("copy", list(r.new_items), dict(r.replaced)) if r.new_items else None))
        self._run(title, lambda j: fileops.transfer(j, paths, dest, self._ask_conflict, move=move), dest,
                  undo=undo)

    # ------------------------------------------------------------------
    # Renommer, créer
    # ------------------------------------------------------------------
    def ask_name(self, heading: str, initial: str, action: str, callback, folder: str = "",
                 select_stem: bool = True, current: str = "", taken: Optional[set] = None,
                 parent: Optional[Gtk.Window] = None):
        """Saisie d'un nom avec vérification immédiate (vide, « / », déjà existant). parent : fenêtre
        sur laquelle poser la question (la fenêtre d'archive), sinon celle-ci."""
        row = Adw.EntryRow(title=tr("Nom"), text=shown(initial))
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(row)
        msg = Gtk.Label(xalign=0, css_classes=["error", "caption"], margin_top=6, visible=False, wrap=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(box)
        col.append(msg)
        d = AlertDialog.new(heading, None)
        d.add_response("cancel", tr("Annuler"))
        d.add_response("ok", action)
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        d.set_extra_child(col)
        d.set_default_size(name_dialog_width(row, shown(initial)), -1)

        # nom non UTF-8 : le champ montre « caf�.txt » ; inchangé, il ne doit pas renommer le fichier
        same = shown(current) if current else None

        def problem(text: str) -> str:
            err = fileops.valid_name(text)
            if err:
                return err
            if folder and text != same and os.path.lexists(os.path.join(folder, text)) \
                    and not _case_only(folder, text, same):
                return tr("« {0} » existe déjà").format(text)
            if taken is not None and text != same and text in taken:      # noms dans une archive
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
            if ok and row.get_text() != same:
                callback(row.get_text())

        def activated(*_):
            if not problem(row.get_text()):
                finish(True)
                d.close()
        row.connect("entry-activated", activated)
        d.connect("response", lambda _d, r: finish(r == "ok"))
        check()
        d.present(parent or self)

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
        if len(sel) > 1 and all(r.path for r in sel):
            self.batch_rename(sel)
            return
        if len(sel) != 1:
            return
        rec = sel[0]
        if p.mode == "archive":
            self.rename_in_archive(p, rec)
            return
        if not rec.path:
            if rec.uri and p.mode == "remote":
                def apply_gio(new):
                    try:
                        gioops.rename(rec.uri, new)
                    except GLib.Error as e:
                        alert(self, tr("Renommage impossible"), e.message)
                        return
                    p.model.reload_quiet()
                self.ask_name(tr("Renommer"), rec.name, tr("Renommer"), apply_gio, select_stem=not rec.is_dir,
                              current=rec.name)
            return
        folder = os.path.dirname(rec.path)

        def apply(new):
            try:
                target = fileops.rename(rec.path, new)
            except (OSError, Exception) as e:  # noqa: BLE001
                alert(self, tr("Renommage impossible"), getattr(e, "strerror", None) or str(e))
                return
            self._push_undo(("rename", rec.path, target))
            if starred.available():
                starred.moved([(rec.path, target)])
            if rec.is_dir:
                for other in self.all_panes():           # panneaux ouverts dans ce dossier : suivre
                    other.follow_move(rec.path, target)
            p.model.connect_once_ready(lambda: p.select_names([new])) if p.model.loading else \
                GLib.timeout_add(300, lambda: (p.select_names([new]), False)[1])
        self.ask_name(tr("Renommer"), rec.name, tr("Renommer"), apply, folder, select_stem=not rec.is_dir,
                      current=rec.name)

    def batch_rename(self, sel):
        """Plusieurs éléments : le dialogue « Renommer » de Nautilus (modèle, rechercher / remplacer)."""
        items = []
        for r in sel:
            try:
                mtime = os.lstat(r.path).st_mtime
            except OSError:
                mtime = 0.0
            items.append(batchrename.Item(r.path, is_dir=r.is_dir, mtime=mtime))

        def apply(pairs):
            def work(j):
                res = fileops.Result("rename")
                done, errors = batchrename.apply(pairs, lambda n, total: j.progress(n / max(1, total)))
                res.moved = done
                res.created = [new for _old, new in done]
                res.errors = errors
                return res
            self._run(tr("Renommage"), work,
                      undo=lambda r: ("batch-rename", list(r.moved)) if r.moved else None)
        BatchRenameDialog(self, items, apply).present()

    def _refresh_devices(self):
        """Après nos propres opérations, les dossiers que personne ne surveille pour nous sont relus, dans TOUTES
        les fenêtres (copie d'une fenêtre vers une autre) : téléphone / appareil photo (pas de surveillance, voir
        DirModel.load), partage réseau et smb://… (retour d'un utilisateur : photo copiée sur le NAS absente,
        photo supprimée encore là, jusqu'à F5 — inotify ne voit rien de ce que GIO fait sur le partage, et GVfs
        n'a pas de surveillance pour smb). Relecture discrète : défilement et sélection gardés."""
        app = self.get_application() if hasattr(self, "get_application") else None
        windows = [w for w in app.get_windows() if hasattr(w, "all_panes")] if app is not None else [self]
        for w in windows:
            for p in w.all_panes():
                if not p.location:
                    continue
                if p.mode == "fs" and fsinfo.is_gvfs_device(p.location):
                    p.refresh()
                elif p.mode == "remote" or p.mode == "fs" and fsinfo.is_network(p.location):
                    if p.miller is not None and p.view_mode == "columns":
                        p.refresh()
                    else:
                        p.model.refresh_quiet()

    def new_folder(self):
        p = self.pane
        if p.mode == "archive":
            self.new_folder_in_archive(p)
            return
        folder = p.current_dir()
        if not folder and self._gio_dir():
            def make_gio(name):
                try:
                    gioops.make_folder(self._gio_dir(), name)
                except GLib.Error as e:
                    alert(self, tr("Création impossible"), e.message)
                    return
                p.model.reload_quiet()
            self.ask_name(tr("Nouveau dossier"), tr("Nouveau dossier"), tr("Créer"), make_gio, select_stem=False)
            return
        if not folder:
            return

        def make(name):
            try:
                path = fileops.make_folder(folder, name)
            except Exception as e:  # noqa: BLE001
                alert(self, tr("Création impossible"), getattr(e, "strerror", None) or str(e))
                return
            self._push_undo(("create", [path]))
            self._refresh_devices()
            GLib.timeout_add(300, lambda: (p.select_names([name]), False)[1])
        self.ask_name(tr("Nouveau dossier"), fileops.unique_name(folder, tr("Nouveau dossier"), True),
                      tr("Créer"), make, folder, select_stem=False)

    def new_file_named(self, template: str = ""):
        p = self.pane
        # même règle que « Nouveau dossier » : aussi dans une recherche (dossier de départ) et un smb://
        # ouvert par son URI (chemin FUSE) — avant, rien ne se passait, sans message
        folder = p.current_dir()
        if not folder and self._gio_dir():
            def make_gio(name):
                try:
                    gioops.make_file(self._gio_dir(), name, template or None)
                except GLib.Error as e:
                    alert(self, tr("Création impossible"), e.message)
                    return
                p.model.reload_quiet()
            self.ask_name(tr("Nouveau document"), os.path.basename(template) if template
                          else tr("Nouveau document.txt"), tr("Créer"), make_gio)
            return
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
            self._push_undo(("create", [path]))
            self._refresh_devices()
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
        if p.mode == "trash" or self._gio_dir():
            self.delete()        # admin://… : pas de corbeille, suppression après confirmation
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
            return ("trash", list(res.trashed), dict(res.trash_uris)) if res.trashed else None

        def done(job):
            # pas de corbeille sur ce support : comme Nautilus, proposer la suppression définitive
            left = getattr(job.result, "no_trash", None) if job.state == "done" else None
            if left:
                self._offer_delete(left)
        self._run(ntr("Mise à la corbeille de {0} élément", "Mise à la corbeille de {0} éléments",
                      len(paths)).format(len(paths)), lambda j: fileops.trash(j, paths), undo=undo, on_done=done)

    def _offer_delete(self, left: list):
        """Support sans corbeille : comme Nautilus, proposer la suppression définitive."""
        alert(self, ntr("Impossible de déplacer le fichier vers la corbeille, voulez-vous le supprimer "
                        "immédiatement ?",
                        "Impossible de déplacer les fichiers vers la corbeille, voulez-vous les supprimer "
                        "immédiatement ?", len(left)),
              describe_paths(left) + "\n" + tr("Ce support n'a pas de corbeille."),
              responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), destructive="delete",
              default="cancel",
              callback=lambda r: r == "delete" and self._run(tr("Suppression définitive"),
                                                             lambda j: fileops.delete(j, left)))

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
            alert(self, _delete_question([r.name for r in sel]),
                  tr("Les éléments définitivement supprimés ne peuvent pas être restaurés"),
                  responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), destructive="delete",
                  default="cancel", callback=go_trash)
            return
        paths = [r.path for r in sel if r.path]
        gio = bool(self._gio_dir())
        if gio:
            paths = self._sel_locs()
        if not paths:
            return

        def go(r):
            if r == "delete" and gio:
                self._run_gio(tr("Suppression définitive"), lambda j: gioops.delete(j, paths))
            elif r == "delete":
                self._run(tr("Suppression définitive"), lambda j: fileops.delete(j, paths))
        if settings["confirm_delete"] or gio:
            # Google Drive : l'URI ne porte qu'un identifiant (« 1AbC… ») — la question montre les vrais noms
            names = [r.name for r in sel if r.path or r.uri] if gio else \
                [os.path.basename(p.rstrip("/")) or p for p in paths]
            alert(self, _delete_question(names),
                  tr("Les éléments définitivement supprimés ne peuvent pas être restaurés"),
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
        try:
            info = Gio.File.new_for_uri("trash:///").query_info("trash::item-count", Gio.FileQueryInfoFlags.NONE, None)
            if info.get_attribute_uint32("trash::item-count") == 0:
                toast(self, tr("La corbeille est vide"))
                return
        except GLib.Error:
            pass

        def go(r):
            if r == "empty":
                def done(job):
                    for p in self.all_panes():
                        if p.mode == "trash":
                            p.refresh()
                    # ce qui passait par la corbeille ne peut plus être annulé / rétabli
                    self.undo_stack[:] = [e for e in self.undo_stack if e[0] != "trash"]
                    self.redo_stack[:] = [e for e in self.redo_stack if e[0] not in ("copy", "create")]
                    self._update_undo_actions()
                    toast(self, tr("Corbeille vidée"))
                self._run(tr("Vidage de la corbeille"), fileops.empty_trash, on_done=done)
        alert(self, tr("Vider tous les éléments de la corbeille ?"),
              tr("Tous les éléments de la corbeille seront définitivement supprimés."),
              responses=(("cancel", tr("Annuler")), ("empty", tr("Vider la corbeille"))), destructive="empty",
              default="cancel", callback=go)

    # ------------------------------------------------------------------
    # Annuler
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Ouvrir en tant qu'administrateur (admin:// de GVfs, comme Nautilus)
    # ------------------------------------------------------------------
    def is_admin(self) -> bool:
        p = self.pane
        return bool(p and p.location.startswith("admin:"))

    def open_admin(self):
        sel = [r for r in self._sel() if r.path]
        if len(sel) == 1:
            r = sel[0]
            if r.is_dir:
                self._admin_window(r.path)
            else:
                self._admin_file(r)

    def open_admin_folder(self):
        d = self.pane.current_dir() if self.pane else ""
        if d:
            self._admin_window(d)

    def _admin_window(self, path: str):
        """Dossier : nouvelle fenêtre sur admin://, bandeau d'avertissement ; le mot de passe est
        demandé par polkit au montage."""
        if not features.available("admin"):
            return
        self.get_application()._new_window("admin://" + path)

    def _admin_file(self, rec):
        """Fichier : l'application par défaut l'ouvre par admin:// (ou par le chemin FUSE de GVfs si
        elle ne gère pas les URI) ; l'enregistrement se fait alors avec les droits root."""
        f = Gio.File.new_for_uri("admin://" + rec.path)
        app = Gio.AppInfo.get_default_for_type(rec.content_type, False)
        if app is None:
            alert(self, tr("Aucune application"), tr("Aucune application n'ouvre ce type de fichier."))
            return

        def launch():
            try:
                if app.supports_uris():
                    app.launch_uris([f.get_uri()], None)
                elif f.get_path():
                    app.launch([Gio.File.new_for_path(f.get_path())], None)
                else:
                    raise GLib.Error(tr("Cette application ne sait pas ouvrir les fichiers distants."))
            except GLib.Error as e:
                alert(self, tr("Ouverture impossible"), e.message)

        def mounted(src, res):
            try:
                src.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    alert(self, tr("Accès administrateur refusé"), e.message)
                    return
            launch()
        f.mount_enclosing_volume(Gio.MountMountFlags.NONE, Gtk.MountOperation.new(self), None, mounted)

    def _push_undo(self, entry, keep_redo: bool = False):
        self.undo_stack.append(entry)
        dropped = self.undo_stack[:-30]
        del self.undo_stack[:-30]
        if not keep_redo:
            dropped += self.redo_stack
            self.redo_stack.clear()
        self._release_backups(dropped)
        self._update_undo_actions()

    def _release_backups(self, dropped: list):
        """Versions gardées (étiquettes, photos) d'entrées sorties des deux piles : effacées tout de suite,
        plutôt qu'au démarrage suivant (Odysseus, service D-Bus, reste lancé des jours)."""
        import shutil

        def dirs(entries):
            out = set()
            for e in entries:
                if e and e[0] in ("tags", "photo-meta"):
                    for item in e[1]:
                        for kept in item[1:3]:
                            if kept:
                                out.add(os.path.dirname(kept))
            return out
        gone = dirs(dropped) - dirs(self.undo_stack) - dirs(self.redo_stack)
        for d in gone:
            shutil.rmtree(d, ignore_errors=True)

    def _update_undo_actions(self):
        for name, stack in (("undo", self.undo_stack), ("redo", self.redo_stack)):
            a = self.lookup_action(name)
            if a is not None:
                a.set_enabled(bool(stack))

    @staticmethod
    def undo_label(entry, redo: bool = False) -> str:
        """Libellé du menu, comme Nautilus : « Annuler la mise à la corbeille »…"""
        kind = entry[0] if entry else ""
        if kind == "batch-rename":
            kind = "rename"
        if redo:
            return {"rename": tr("Rétablir le renommage"), "move": tr("Rétablir le déplacement"),
                    "tags": tr("Rétablir la modification des étiquettes"),
                    "photo-meta": tr("Rétablir la modification des métadonnées"),
                    "trash": tr("Rétablir la mise à la corbeille"), "copy": tr("Rétablir la copie"),
                    "create": tr("Rétablir la création")}.get(kind, tr("Rétablir"))
        return {"rename": tr("Annuler le renommage"), "move": tr("Annuler le déplacement"),
                "tags": tr("Annuler la modification des étiquettes"),
                "photo-meta": tr("Annuler la modification des métadonnées"),
                "trash": tr("Annuler la mise à la corbeille"), "copy": tr("Annuler la copie"),
                "create": tr("Annuler la création")}.get(kind, tr("Annuler la dernière opération"))

    def _batch_rename_steps(self, pairs, redo: bool):
        """Annuler (ou refaire) un renommage multiple : les mêmes renommages à l'envers, dans un ordre
        sûr (batchrename.apply). Seul ce qui a réussi passe dans l'autre pile."""
        steps = [(old, os.path.basename(new)) for old, new in pairs] if redo else \
            [(new, os.path.basename(old)) for old, new in pairs]

        def work(j):
            res = fileops.Result("rename")
            done, errors = batchrename.apply(steps)
            res.moved, res.errors = done, errors
            res.created = [new for _o, new in done]
            return res

        def finished(j):
            res = j.result
            if not isinstance(res, fileops.Result) or not res.moved:
                return
            if redo:
                self._push_undo(("batch-rename", list(res.moved)), keep_redo=True)
            else:
                # l'entrée garde le sens d'origine (ancien → nouveau) ; ici res.moved va du nouveau à l'ancien
                self.redo_stack.append(("batch-rename", [(dst, src) for src, dst in res.moved]))
                self._update_undo_actions()
        self._run(tr("Renommage"), work, on_done=finished)

    def _keep_steps(self, entry, redo: bool):
        """Annuler / rétablir les étiquettes audio ou les métadonnées photo : chaque fichier reprend sa version
        gardée d'avant (ou d'après), octet pour octet. Un fichier modifié ailleurs depuis n'est pas touché
        (son état ne correspond plus) ; seuls les fichiers remis passent dans l'autre pile."""
        from ..backend import keep
        kind, items = entry[0], entry[1]

        def work(job):
            res = fileops.Result("tags" if kind == "tags" else "photo")
            res.count = 0
            done = []
            for p, before, after, stamp in items:
                job.check_cancel()
                src = after if redo else before
                try:
                    if not src or not os.path.exists(src):
                        raise OSError(2, tr("Version gardée introuvable"))
                    # état noté juste après notre écriture (pas celui de la sauvegarde « après » : c'est un
                    # lien dur vers le fichier, une modification sur place par un autre programme la suivrait)
                    if keep.Backups.stamp(p)[:2] != tuple(stamp)[:2]:
                        raise OSError(0, tr("Modifié depuis : laissé tel quel"))
                    keep.restore(src, p)
                    done.append((p, before, after, keep.Backups.stamp(p)))
                    res.count += 1
                except OSError as e:
                    res.errors.append((p, e.strerror or str(e)))
            res.kept = done
            return res

        def finished(job):
            done = getattr(job.result, "kept", None)
            if not done:
                return
            if redo:
                self._push_undo((kind, done), keep_redo=True)
            else:
                self.redo_stack.append((kind, done))
                self._update_undo_actions()
        self._run(tr("Étiquettes") if kind == "tags" else tr("Métadonnées des photos"), work, on_done=finished)

    def redo(self):
        """Refait la dernière opération annulée."""
        if not self.redo_stack:
            return
        entry = self.redo_stack.pop()
        self._update_undo_actions()
        kind = entry[0]
        if kind in ("tags", "photo-meta"):
            self._keep_steps(entry, redo=True)
        elif kind == "batch-rename":
            self._batch_rename_steps(entry[1], redo=True)
        elif kind == "rename":
            _k, old, new = entry
            try:
                fileops.rename(old, os.path.basename(new))
                self._push_undo(entry, keep_redo=True)
                self._follow_rename(old, new)
            except (OSError, ToolError) as e:
                alert(self, tr("Rétablissement impossible"), getattr(e, "strerror", None) or str(e))
        elif kind == "move":
            pairs = entry[1]

            def work(j):
                res = fileops.Result("move")
                for src, dst in pairs:
                    j.check_cancel()
                    try:
                        if os.path.lexists(dst):
                            raise OSError(17, tr("« {0} » existe déjà").format(os.path.basename(dst)))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        fileops.move_new(src, dst)          # jamais par-dessus un élément arrivé entre-temps
                        res.created.append(dst)
                        res.moved.append((src, dst))
                    except OSError as e:
                        res.errors.append((src, e.strerror or str(e)))
                return res

            def redone(j):
                # n'annuler ensuite que ce qui a réellement bougé
                moved = list(j.result.moved) if isinstance(j.result, fileops.Result) else []
                if moved:
                    self._push_undo(("move", moved), keep_redo=True)
            self._run(tr("Déplacement"), work, on_done=redone)
        elif kind == "trash":
            originals = [p for p in entry[1] if os.path.lexists(p)]

            def done(j):
                res = j.result
                if isinstance(res, fileops.Result) and res.trashed:
                    self._push_undo(("trash", list(res.trashed), dict(res.trash_uris)), keep_redo=True)
            self._run(tr("Mise à la corbeille"), lambda j: fileops.trash(j, originals), on_done=done)
        elif kind in ("copy", "create"):
            # l'annulation les avait mis à la corbeille : on sort de la corbeille CES éléments-là (URI
            # notées à l'annulation), et l'entrée suivante vise l'endroit où ils sont réellement revenus
            # (« a (2).txt » si un autre « a.txt » est apparu entre-temps — avant, le Ctrl+Z suivant mettait
            # ce fichier-là, le travail de l'utilisateur, à la corbeille)
            uris = entry[3] if len(entry) > 3 else None

            def work(j):
                if uris:
                    return fileops.restore_trashed_uris(j, uris)
                return fileops.restore_trashed_paths(j, entry[1])

            def redone(j):
                back = list(j.result.created) if isinstance(j.result, fileops.Result) else []
                if back:
                    self._push_undo((kind, back), keep_redo=True)
            self._run(tr("Restauration depuis la corbeille"), work, on_done=redone)

    def _undo_entry(self, entry):
        """Bouton « Annuler » d'une notification : annule CETTE opération, seulement si elle est encore
        la dernière (avant : le sommet de la pile — un renommage fait entre-temps, ou une copie finie
        après, était défait à sa place)."""
        if self.undo_stack and self.undo_stack[-1] is entry:
            self.undo()
        else:
            toast(self, tr("Une autre opération a été faite depuis : rien n'a été annulé"))

    def undo(self):
        if not self.undo_stack:
            toast(self, tr("Rien à annuler"))
            return
        entry = self.undo_stack.pop()
        self.redo_stack.append(entry)
        self._update_undo_actions()
        kind = entry[0]
        if kind in ("tags", "photo-meta"):
            self.redo_stack.pop()                   # remis par _keep_steps
            self._update_undo_actions()
            self._keep_steps(entry, redo=False)
        elif kind == "batch-rename":
            self.redo_stack.pop()                   # remis par _batch_rename_steps si tout s'est passé
            self._update_undo_actions()
            self._batch_rename_steps(entry[1], redo=False)
        elif kind == "rename":
            _k, old, new = entry
            try:
                # fileops.rename refuse d'écraser un élément créé entre-temps sous l'ancien nom
                fileops.rename(new, os.path.basename(old))
                toast(self, tr("Renommage annulé"))
                self._follow_rename(new, old)
            except (OSError, ToolError) as e:
                self.redo_stack.pop()
                self._update_undo_actions()
                alert(self, tr("Annulation impossible"), getattr(e, "strerror", None) or str(e))
        elif kind == "move":
            pairs = entry[1]

            def work(j):
                res = fileops.Result("move")
                for src, dst in pairs:
                    j.check_cancel()
                    try:
                        if os.path.lexists(src):
                            raise OSError(17, tr("« {0} » existe déjà").format(os.path.basename(src)))
                        os.makedirs(os.path.dirname(src), exist_ok=True)   # dossier vidé puis retiré
                        fileops.move_new(dst, src)
                        res.created.append(src)
                        res.moved.append((dst, src))
                    except OSError as e:
                        res.errors.append((dst, e.strerror or str(e)))
                _put_back_replaced(j, res, replaced)
                return res
            replaced = entry[2] if len(entry) > 2 else {}
            if replaced:
                self._drop_redo(entry)             # un remplacement ne se rejoue pas
            self._run(tr("Annulation du déplacement"), work)
        elif kind == "trash":
            originals = entry[1]
            uris = entry[2] if len(entry) > 2 else {}

            def work_trash(j):
                # les éléments jetés par CETTE opération (URI notées), pas le plus récent homonyme de la
                # corbeille ; ceux sans URI connue (pas de GVfs) : par leur chemin d'origine, comme avant
                known = {p: uris[p] for p in originals if p in uris}
                rest = [p for p in originals if p not in uris]
                res = fileops.restore_trashed_uris(j, known, partial_ok=True)
                if rest:
                    try:
                        r = fileops.restore_trashed_paths(j, rest)
                        res.created += r.created
                        res.errors += r.errors
                    except ToolError as e:
                        res.errors.append((rest[0], e.args[0] if e.args else str(e)))
                fileops._raise_partial(res)
                return res

            def restored(j):
                # Rétablir doit renvoyer à la corbeille ce qui vient d'en sortir, là où c'est arrivé :
                # « a (2).txt » si un nouveau « a.txt » a été créé entre-temps (et pas ce dernier)
                back = list(j.result.created) if isinstance(j.result, fileops.Result) else []
                for i, e in enumerate(self.redo_stack):
                    if e is entry:
                        if back:
                            self.redo_stack[i] = ("trash", back)
                        else:
                            del self.redo_stack[i]
                        break
                self._update_undo_actions()
            self._run(tr("Restauration depuis la corbeille"), work_trash, on_done=restored)
        elif kind in ("copy", "create"):
            paths = [p for p in entry[1] if os.path.lexists(p)]
            replaced = entry[2] if len(entry) > 2 else {}

            def work_copy(j):
                res = fileops.trash(j, paths) if paths else fileops.Result("trash")
                _put_back_replaced(j, res, replaced)
                return res

            def undone(j):
                res = j.result if isinstance(j.result, fileops.Result) else None
                if res is not None and res.no_trash:
                    # support sans corbeille : rien n'a été retiré, on le dit et on propose de supprimer
                    self._offer_delete(res.no_trash)
                if res is None or replaced or not res.trashed:
                    self._drop_redo(entry)             # rien à rétablir (ou un remplacement)
                else:
                    self._set_redo(entry, (kind, entry[1], {}, dict(res.trash_uris)))
            if paths or replaced:
                self._run(tr("Annulation : mise à la corbeille"), work_copy, on_done=undone)
            else:
                self._drop_redo(entry)

    def _drop_redo(self, entry):
        self.redo_stack[:] = [e for e in self.redo_stack if e is not entry]
        self._update_undo_actions()

    def _set_redo(self, entry, new):
        for i, e in enumerate(self.redo_stack):
            if e is entry:
                self.redo_stack[i] = new
                break
        self._update_undo_actions()

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

    def _regular_files(self) -> list:
        return [r.path for r in self._sel() if r.path and not r.is_dir]

    def email(self):
        """« Courriel… » comme Nautilus : nouveau message du logiciel de courriel par défaut, avec les
        fichiers en pièces jointes (xdg-email : Thunderbird, Evolution, Geary…)."""
        paths = self._regular_files()
        if not paths:
            return
        from ..backend import mail

        def launch(argv):
            if argv[0] == "xdg-email" and not shutil.which("xdg-email"):
                alert(self, tr("Courriel impossible"), tr("Installez xdg-utils (sudo apt install xdg-utils)."))
                return False
            try:
                subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                alert(self, tr("Courriel impossible"), str(e))
            return False

        # dans un fil : Thunderbird en Flatpak = un « flatpak document-export » par fichier (jusqu'à 10 s
        # chacun), la fenêtre restait figée pendant ce temps
        def work():
            argv = mail.compose_argv(paths)    # Thunderbird (deb, snap, Flatpak) en direct, sinon xdg-email
            GLib.idle_add(launch, argv)
        threading.Thread(target=work, daemon=True).start()

    def send_to_device(self, path: str):
        from ..backend import gsconnect
        uris = [Gio.File.new_for_path(p).get_uri() for p in self._regular_files()]
        name = dict(gsconnect.devices.capable()).get(path, "")
        if uris and gsconnect.devices.send(path, uris):
            toast(self, ntr("{0} fichier envoyé vers {1}", "{0} fichiers envoyés vers {1}", len(uris))
                  .format(len(uris), name))

    def run_program(self):
        """« Exécuter comme un programme », comme Nautilus : dans un terminal, depuis le dossier du
        fichier. Le terminal reste ouvert à la fin pour qu'on puisse lire ce qui s'est passé."""
        sel = self._sel()
        rec = sel[0] if len(sel) == 1 else None
        if not can_run(rec):
            return
        term = find_terminal()
        if not term:
            alert(self, tr("Aucun terminal trouvé"), tr("Indiquez votre terminal dans les préférences."))
            return
        end = tr("Terminé (code {0}). Appuyez sur Entrée pour fermer.")
        # message traduit passé en argument (jamais dans le script) ; « {0} » remplacé par le code de sortie
        script = '"$1"; code=$?; printf "\\n%s\\n" "${2//"{0}"/$code}"; read -r _'
        command = ["bash", "-c", script, "odysseus", rec.path, end]
        try:
            subprocess.Popen(terminal_command(term, command), cwd=os.path.dirname(rec.path),
                             start_new_session=True)
        except OSError as e:
            alert(self, tr("Impossible de lancer le terminal"), str(e))

    def terminal_here(self):
        if not features.enabled("terminal"):
            return
        d = self.pane.current_dir()
        if d:
            self._launch_terminal(d)

    def terminal_selected(self):
        if not features.enabled("terminal"):
            return
        dirs = [r.path for r in self._sel() if r.is_dir and r.path]
        if dirs:
            self._launch_terminal(dirs[0])

    def edit_text(self):
        files = self._sel_gfiles()
        if not files:
            return
        paths = [f.get_path() for f in files]
        editor = find_editor()
        if not all(paths):
            # fichiers sans chemin local (admin://…) : l'éditeur les ouvre par leur URI
            app = _desktop_app_for(editor) or Gio.AppInfo.get_default_for_type("text/plain", False)
            if app is not None:
                self._launch_app(app, files)
            return
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


def _delete_question(names: list[str]) -> str:
    """Titre de la confirmation, mot pour mot comme Nautilus."""
    n = len(names)
    return ntr("Supprimer définitivement « {name} » ?", "Supprimer définitivement les {n} éléments sélectionnés ?",
               n).format(name=names[0] if names else "", n=n)
