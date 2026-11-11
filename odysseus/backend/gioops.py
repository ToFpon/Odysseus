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


def child_named(folder: Gio.File, name: str) -> Gio.File:
    """Élément « name » de folder. Hors disque local, par le nom affiché (get_child_for_display_name) :
    Google Drive crée alors un élément de ce titre au lieu de chercher un identifiant « name »."""
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


def _copy_tree(job: Job, src: Gio.File, dst: Gio.File):
    if _is_dir(src):
        dst.make_directory(None)
        en = src.enumerate_children("standard::name,standard::display-name",
                                    Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        try:
            while (info := en.next_file(None)) is not None:
                job.check_cancel()
                name = info.get_name()
                if not src.is_native():       # Google Drive : identifiant → vrai nom
                    name = info.get_attribute_string("standard::display-name") or name
                _copy_tree(job, src.get_child(info.get_name()), child_named(dst, name))
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
        name = source_name(src)
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
