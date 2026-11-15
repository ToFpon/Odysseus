"""Opérations sur les fichiers par GIO, pour les emplacements sans chemin local.

admin:// (fenêtre administrateur) et certains partages GVfs n'exposent pas de chemin local : on ne
peut pas y utiliser os.* ni copy_file_range. Copier, déplacer, supprimer, renommer et créer passent
alors par Gio.File, et c'est le démon GVfs (root pour admin://) qui fait le travail.
"""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Gio, GLib

from ..i18n import tr
from .engine import Job
from .fileops import Result, split_ext

COPY_FLAGS = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA


def gfile(location: str) -> Gio.File:
    return Gio.File.new_for_uri(location) if "://" in location else Gio.File.new_for_path(location)


def _exists(f: Gio.File) -> bool:
    return f.query_exists(None)


def source_name(f: Gio.File) -> str:
    """Nom à donner à la copie. Google Drive / OneDrive : le nom de l'URI est un identifiant
    (« 1AbC… »), le vrai nom est standard::display-name — comme Nautilus."""
    if f.is_native():
        return f.get_basename() or "?"
    try:
        info = f.query_info("standard::display-name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        return info.get_attribute_string("standard::display-name") or f.get_basename() or "?"
    except GLib.Error:
        return f.get_basename() or "?"


def valid_name(name: str, folder: Gio.File) -> str:
    """Un titre Google Drive / OneDrive peut contenir « / » (« Budget 2025/2026.ods ») ou valoir « .. » :
    pris tel quel comme nom, la copie partait dans un sous-dossier ou AU-DESSUS de la destination (un
    dossier partagé hostile pouvait écrire n'importe où dans le dossier personnel). « / » → « _ »,
    « . » / « .. » / vide → « _ » ; FAT / NTFS : caractères interdits remplacés (fsinfo.safe_name)."""
    name = name.replace("/", "_").replace("\0", "_")
    if name in ("", ".", ".."):
        name = "_"
    path = folder.get_path()
    if path:
        from . import fsinfo
        if fsinfo.is_fat(path):
            name = fsinfo.safe_name(name)
    return name


def child_named(folder: Gio.File, name: str) -> Gio.File:
    """Élément « name » de folder (nom rendu valide : valid_name). Hors disque local, par le nom affiché
    (get_child_for_display_name) : Google Drive crée alors un élément de ce titre au lieu de chercher un
    identifiant « name »."""
    name = valid_name(name, folder)
    if folder.is_native():
        return folder.get_child(name)
    try:
        return folder.get_child_for_display_name(name)
    except GLib.Error:
        return folder.get_child(name)


def unique_child(folder: Gio.File, name: str) -> Gio.File:
    """« nom (copie).ext », « nom (copie 2).ext »… comme pour les fichiers locaux."""
    child = child_named(folder, name)
    if not _exists(child):
        return child
    stem, ext = split_ext(name)
    n = 1
    while True:
        suffix = tr(" (copie)") if n == 1 else tr(" (copie {0})").format(n)
        child = child_named(folder, f"{stem}{suffix}{ext}")
        if not _exists(child):
            return child
        n += 1


def _is_dir(f: Gio.File) -> bool:
    return f.query_file_type(Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None) == Gio.FileType.DIRECTORY


FILE_WEIGHT = 256 * 1024     # chaque fichier « pèse » aussi ceci dans la progression : sur Google Drive,
                             # c'est le nombre de fichiers qui coûte (un aller-retour chacun), pas leur taille
SCAN_ATTRS = "standard::name,standard::display-name,standard::type,standard::size"


def _size(info: Gio.FileInfo) -> int:
    """Taille, 0 si l'emplacement ne la donne pas : Google Drive n'en a pas pour les dossiers, et
    get_size() sur un attribut absent écrit un CRITICAL de GIO (vu chez Tof)."""
    if info.has_attribute("standard::size"):
        return info.get_attribute_uint64("standard::size")
    return 0


class _Progress:
    """Avancement d'une copie par GIO : fichiers et octets, recensés d'abord (comme Nautilus). Avant, la
    barre n'avançait qu'à chaque élément du haut : un seul dossier = 0 % jusqu'à la fin (retour de Tof,
    dossier Google Drive de 70 petits fichiers)."""

    def __init__(self, job: Job):
        self.job = job
        self.total = 0
        self.files = 0
        self.done = 0
        self.done_files = 0
        self._last = 0.0
        self.cancellable = Gio.Cancellable()

    def add(self, size: int):
        self.files += 1
        self.total += size + FILE_WEIGHT

    def step(self, name: str, current: int = 0):
        import time
        now = time.monotonic()
        if current and now - self._last < 0.1:
            return
        self._last = now
        if self.job.cancelled:
            self.cancellable.cancel()
        frac = (self.done + current) / self.total if self.total else 0.0
        self._frac = max(getattr(self, "_frac", 0.0), frac)      # jamais en arrière (déplacements recensés
        frac = self._frac                                          # l'un après l'autre)
        self.job.progress(frac, f"{name} ({min(self.done_files + 1, self.files)} / {self.files})"
                          if self.files > 1 else name)

    def finished(self, size: int):
        self.done += size + FILE_WEIGHT
        self.done_files += 1


def _scan(job: Job, src: Gio.File, is_dir: bool, size: int, prog: _Progress, errors: Optional[list]) -> list:
    """Éléments à copier, dossier avant son contenu : (source, chemin relatif en noms, dossier ?, taille).
    Le type et la taille viennent de l'énumération : pas de requête de plus par fichier (sur Drive, chacune
    est un aller-retour vers Google)."""
    out = [(src, (), is_dir, size)]
    if not is_dir:
        prog.add(size)
        return out
    pending = [(src, ())]
    while pending:
        job.check_cancel()
        folder, rel = pending.pop(0)
        try:
            en = folder.enumerate_children(SCAN_ATTRS, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                                           prog.cancellable)
        except GLib.Error as e:
            if errors is None:
                raise
            errors.append((folder.get_path() or folder.get_uri(), e.message))
            continue
        try:
            while (info := en.next_file(prog.cancellable)) is not None:
                name = info.get_name()
                if not folder.is_native():       # Google Drive : identifiant → vrai nom
                    name = info.get_attribute_string("standard::display-name") or name
                child = folder.get_child(info.get_name())
                child_dir = info.get_file_type() == Gio.FileType.DIRECTORY
                out.append((child, rel + (name,), child_dir, _size(info)))
                if child_dir:
                    pending.append((child, rel + (name,)))
                else:
                    prog.add(_size(info))
        finally:
            en.close(None)
    return out


def _copy_tree(job: Job, src: Gio.File, dst: Gio.File, errors: Optional[list] = None,
               prog: Optional[_Progress] = None, info: Optional[Gio.FileInfo] = None):
    """Copie src (fichier ou dossier) vers dst. Avec `errors`, un élément du dossier qui échoue y est noté
    (chemin, message) et la copie continue avec les autres ; sans, la première erreur remonte."""
    prog = prog or _Progress(job)
    if info is None:
        info = src.query_info("standard::type,standard::size", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
    entries = _scan(job, src, info.get_file_type() == Gio.FileType.DIRECTORY, _size(info), prog, errors)
    _copy_entries(job, src, dst, entries, errors, prog)


def _copy_entries(job: Job, src: Gio.File, dst: Gio.File, entries: list, errors: Optional[list],
                  prog: _Progress):
    made = {(): dst}
    for child, rel, is_dir, size in entries:
        job.check_cancel()
        parent = made.get(rel[:-1]) if rel else None
        if rel and parent is None:
            continue                              # son dossier n'a pas pu être créé (erreur déjà notée)
        target = child_named(parent, rel[-1]) if rel else dst
        label = rel[-1] if rel else source_name(src)
        try:
            if is_dir:
                target.make_directory(prog.cancellable)
                made[rel] = target
            else:
                prog.step(label)
                child.copy(target, COPY_FLAGS, prog.cancellable,
                           lambda current, _total, *_: prog.step(label, current), None)
                prog.finished(size)
        except GLib.Error as e:
            job.check_cancel()                    # annulé : Cancelled, pas une erreur de plus
            if errors is None or not rel:
                raise
            errors.append((child.get_path() or child.get_uri(), e.message))


def delete_tree(f: Gio.File):
    if _is_dir(f):
        en = f.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        try:
            while (info := en.next_file(None)) is not None:
                delete_tree(f.get_child(info.get_name()))
        finally:
            en.close(None)
    f.delete(None)


def transfer(job: Job, sources: list[str], dest: str, move: bool = False) -> Result:
    """Copie ou déplace (chemins ou URI) dans dest (chemin ou URI). En cas de nom déjà pris :
    « nom (copie) », jamais d'écrasement."""
    res = Result("move" if move else "copy")
    folder = gfile(dest)
    prog = _Progress(job)
    if not move:
        return _copy_all(job, sources, folder, prog, res)
    for s in sources:
        job.check_cancel()
        src = gfile(s)
        try:
            # une seule requête : vrai nom (Drive), type et taille
            info = src.query_info("standard::display-name,standard::type,standard::size",
                                  Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
            name = (info.get_attribute_string("standard::display-name") if not src.is_native() else None) \
                or src.get_basename() or "?"
            dst = unique_child(folder, name)
            if move:
                try:
                    src.move(dst, Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None)
                except GLib.Error as e:
                    if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WOULD_RECURSE) and \
                            not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                        raise
                    # autre système de fichiers : copier puis supprimer — l'original seulement si TOUT
                    # est arrivé ; sinon il reste, la copie partielle aussi (comme une copie)
                    missed = []
                    _copy_tree(job, src, dst, missed, prog, info)
                    if missed:
                        res.errors.extend(missed)
                        res.created.append(dst.get_path() or dst.get_uri())
                        continue
                    delete_tree(src)
                res.moved.append((s, dst.get_path() or dst.get_uri()))
            else:
                missed = []
                _copy_tree(job, src, dst, missed, prog, info)
                res.errors.extend(missed)
                res.created.append(dst.get_path() or dst.get_uri())
        except GLib.Error as e:
            res.errors.append((s, e.message))
    job.progress(1.0, "")
    return res


def _copy_all(job: Job, sources: list[str], folder: Gio.File, prog: _Progress, res: Result) -> Result:
    """Copie : tout est recensé d'abord (fichiers, octets), puis copié — la barre avance d'un bout à
    l'autre, même pour un seul dossier."""
    planned = []
    for s in sources:
        job.check_cancel()
        src = gfile(s)
        job.progress(0.0, os.path.basename(s.rstrip("/")) if "://" not in s else (scanner_name(s) or ""))
        try:
            info = src.query_info("standard::display-name,standard::type,standard::size",
                                  Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
            missed: list = []
            entries = _scan(job, src, info.get_file_type() == Gio.FileType.DIRECTORY, _size(info),
                            prog, missed)
            planned.append((s, src, info, entries, missed))
        except GLib.Error as e:
            res.errors.append((s, e.message))
    for s, src, info, entries, missed in planned:
        job.check_cancel()
        name = (info.get_attribute_string("standard::display-name") if not src.is_native() else None) \
            or src.get_basename() or "?"
        try:
            dst = unique_child(folder, name)
            _copy_entries(job, src, dst, entries, missed, prog)
            res.created.append(dst.get_path() or dst.get_uri())
        except GLib.Error as e:
            res.errors.append((s, e.message))
        res.errors.extend(missed)
    job.progress(1.0, "")
    return res


def scanner_name(uri: str) -> Optional[str]:
    """Nom affiché d'une URI Drive déjà lue (cache du scanner), sans requête."""
    from .scanner import display_name
    return display_name(uri)


def delete(job: Job, locations: list[str]) -> Result:
    res = Result("delete")
    for loc in locations:
        job.check_cancel()
        try:
            delete_tree(gfile(loc))
            res.created.append(loc)
        except GLib.Error as e:
            res.errors.append((loc, e.message))
    return res


def rename(location: str, new_name: str) -> str:
    """Renomme ; renvoie le nouvel emplacement. Lève GLib.Error."""
    f = gfile(location)
    out = f.set_display_name(new_name, None)
    return out.get_path() or out.get_uri()


def make_folder(folder: str, name: str) -> str:
    f = child_named(gfile(folder), name)
    f.make_directory(None)
    return f.get_path() or f.get_uri()


def make_file(folder: str, name: str, template: Optional[str] = None) -> str:
    f = child_named(gfile(folder), name)
    if template:
        gfile(template).copy(f, Gio.FileCopyFlags.NONE, None, None)
    else:
        f.create(Gio.FileCreateFlags.NONE, None).close(None)
    return f.get_path() or f.get_uri()
