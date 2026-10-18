"""Opérations sur les fichiers par GIO, pour les emplacements sans chemin local.

admin:// (fenêtre administrateur) et certains partages GVfs n'exposent pas de chemin local : on ne
peut pas y utiliser os.* ni copy_file_range. Copier, déplacer, supprimer, renommer et créer passent
alors par Gio.File, et c'est le démon GVfs (root pour admin://) qui fait le travail.
"""

from __future__ import annotations

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


def unique_child(folder: Gio.File, name: str) -> Gio.File:
    """« nom (copie).ext », « nom (copie 2).ext »… comme pour les fichiers locaux."""
    child = folder.get_child(name)
    if not _exists(child):
        return child
    stem, ext = split_ext(name)
    n = 1
    while True:
        suffix = tr(" (copie)") if n == 1 else tr(" (copie {0})").format(n)
        child = folder.get_child(f"{stem}{suffix}{ext}")
        if not _exists(child):
            return child
        n += 1


def _is_dir(f: Gio.File) -> bool:
    return f.query_file_type(Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None) == Gio.FileType.DIRECTORY


def _copy_tree(job: Job, src: Gio.File, dst: Gio.File):
    if _is_dir(src):
        dst.make_directory(None)
        en = src.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        try:
            while (info := en.next_file(None)) is not None:
                job.check_cancel()
                _copy_tree(job, src.get_child(info.get_name()), dst.get_child(info.get_name()))
        finally:
            en.close(None)
    else:
        src.copy(dst, COPY_FLAGS, None, None)


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
    n = len(sources)
    for i, s in enumerate(sources):
        job.check_cancel()
        src = gfile(s)
        name = src.get_basename() or "?"
        job.progress(i / max(n, 1), name)
        try:
            dst = unique_child(folder, name)
            if move:
                try:
                    src.move(dst, Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None)
                except GLib.Error as e:
                    if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.WOULD_RECURSE) and \
                            not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                        raise
                    _copy_tree(job, src, dst)        # autre système de fichiers : copier puis supprimer
                    delete_tree(src)
                res.moved.append((s, dst.get_path() or dst.get_uri()))
            else:
                _copy_tree(job, src, dst)
                res.created.append(dst.get_path() or dst.get_uri())
        except GLib.Error as e:
            res.errors.append((s, e.message))
    job.progress(1.0, "")
    return res


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
    f = gfile(folder).get_child(name)
    f.make_directory(None)
    return f.get_path() or f.get_uri()


def make_file(folder: str, name: str, template: Optional[str] = None) -> str:
    f = gfile(folder).get_child(name)
    if template:
        gfile(template).copy(f, Gio.FileCopyFlags.NONE, None, None)
    else:
        f.create(Gio.FileCreateFlags.NONE, None).close(None)
    return f.get_path() or f.get_uri()
