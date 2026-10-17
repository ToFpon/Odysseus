"""Lecture rapide des dossiers.

Mesuré sur 100 000 fichiers : énumération GIO + objets Python ≈ 1,3 s ; os.scandir seul ≈ 0,07 s,
avec stat() ≈ 0,4 s. On lit donc les dossiers locaux avec os.scandir dans un fil d'exécution,
en deux temps : noms et types d'abord (d_type, sans appel système supplémentaire), triés et
affichés tout de suite ; tailles et dates ensuite. Aucun objet GTK n'est créé ici : le modèle
de la vue ne fabrique un objet que pour les lignes réellement affichées.

Les emplacements non locaux (trash://, sftp:// via GVfs…) passent par GIO, toujours hors du fil
principal.
"""

from __future__ import annotations

import os
import stat as st
import threading
from typing import Callable, Optional

from gi.repository import Gio, GLib

ATTRS = ("standard::name,standard::display-name,standard::type,standard::size,standard::is-hidden,"
         "standard::is-backup,standard::is-symlink,standard::fast-content-type,standard::icon,"
         "time::modified,unix::mode,unix::uid,trash::orig-path,trash::deletion-date,standard::target-uri")


class Rec:
    """Un élément de dossier, sans aucun objet GTK."""
    __slots__ = ("name", "path", "uri", "is_dir", "is_link", "size", "mtime", "mode", "uid", "hidden",
                 "key", "ctype", "stat_ok", "extra", "obj")

    def __init__(self, name: str, path: Optional[str], is_dir: bool, is_link: bool = False):
        self.name = name
        self.path = path            # chemin local, ou None pour un emplacement GIO pur
        self.uri = None             # URI pour les emplacements non locaux
        self.is_dir = is_dir
        self.is_link = is_link
        self.size = 0
        self.mtime = 0.0
        self.mode = 0
        self.uid = -1
        self.hidden = name.startswith(".") or name.endswith("~")
        self.key = None
        self.ctype = None
        self.stat_ok = False
        self.extra = None           # dict facultatif : nombre d'éléments, durée, miniature, emblèmes…
        self.obj = None             # objet GObject créé à la demande par le modèle

    # -- accès pratiques ---------------------------------------------------
    def get(self, k, default=None):
        return self.extra.get(k, default) if self.extra else default

    def set(self, k, v):
        if self.extra is None:
            self.extra = {}
        self.extra[k] = v

    @property
    def gfile(self) -> Gio.File:
        return Gio.File.new_for_path(self.path) if self.path else Gio.File.new_for_uri(self.uri)

    @property
    def content_type(self) -> str:
        if self.ctype is None:
            if self.is_dir:
                self.ctype = "inode/directory"
            else:
                self.ctype = Gio.content_type_guess(self.name, None)[0] or "application/octet-stream"
        return self.ctype

    @property
    def is_executable(self) -> bool:
        return bool(self.mode and st.S_ISREG(self.mode) and self.mode & 0o111)


def sort_name(name: str) -> str:
    """Clé de tri des noms identique à Nautilus (g_utf8_collate_key_for_filename : 2 < 10)."""
    try:
        return GLib.utf8_collate_key_for_filename(name, -1)
    except (TypeError, UnicodeError):
        return GLib.utf8_collate_key_for_filename(
            name.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), -1)


def apply_stat(r: Rec, s: os.stat_result):
    r.size = s.st_size
    r.mtime = s.st_mtime
    r.mode = s.st_mode
    r.uid = s.st_uid
    r.stat_ok = True


def read_hidden_file(folder: str) -> set:
    """Fichier « .hidden » de Nautilus : un nom par ligne, masqué comme un fichier caché."""
    try:
        with open(os.path.join(folder, ".hidden"), encoding="utf-8", errors="surrogateescape") as fh:
            return {line.rstrip("\n") for line in fh if line.strip()}
    except OSError:
        return set()


def scan_local(path: str, cancel: threading.Event, with_stat: bool = True) -> list[Rec]:
    """Liste un dossier local. Lève OSError si le dossier est illisible."""
    out = []
    hidden_extra = read_hidden_file(path)
    with os.scandir(path) as it:
        for e in it:
            if cancel.is_set():
                return []
            try:
                is_link = e.is_symlink()
                is_dir = e.is_dir()
            except OSError:
                is_link, is_dir = True, False
            r = Rec(e.name, e.path, is_dir, is_link)
            if hidden_extra and e.name in hidden_extra:
                r.hidden = True
            r.key = sort_name(e.name)
            if with_stat:
                try:
                    apply_stat(r, e.stat())
                except OSError:
                    try:
                        apply_stat(r, e.stat(follow_symlinks=False))
                    except OSError:
                        pass
            out.append(r)
    return out


def stat_all(recs: list[Rec], cancel: threading.Event, every: int = 0, on_chunk: Optional[Callable] = None):
    """Complète tailles et dates (second temps du chargement)."""
    for i, r in enumerate(recs):
        if cancel.is_set():
            return
        if r.stat_ok or not r.path:
            continue
        try:
            apply_stat(r, os.stat(r.path))
        except OSError:
            try:
                apply_stat(r, os.lstat(r.path))
            except OSError:
                pass
        if every and on_chunk and i and i % every == 0:
            on_chunk()


def rec_from_info(info: Gio.FileInfo, parent: Gio.File) -> Rec:
    name = info.get_name()
    child = parent.get_child(name)
    is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
    r = Rec(info.get_display_name() or name, child.get_path(), is_dir, info.get_is_symlink())
    r.uri = child.get_uri()
    r.hidden = info.get_is_hidden() or info.get_is_backup()
    r.size = info.get_size()
    r.mtime = float(info.get_attribute_uint64("time::modified") or 0)
    r.mode = info.get_attribute_uint32("unix::mode") or 0
    r.stat_ok = True
    r.ctype = info.get_attribute_string("standard::fast-content-type") or None
    r.key = sort_name(r.name)
    orig = info.get_attribute_byte_string("trash::orig-path") if info.has_attribute("trash::orig-path") else None
    if orig:
        r.set("orig_path", orig)
        dt = info.get_attribute_string("trash::deletion-date")
        if dt:
            r.set("deletion_date", dt)
    target = info.get_attribute_string("standard::target-uri")
    if target:
        r.set("target_uri", target)
    icon = info.get_icon()
    if icon is not None and not r.path:
        r.set("gicon", icon)
    return r


def scan_gio(gfile: Gio.File, cancel: threading.Event) -> list[Rec]:
    """Liste un emplacement GIO (corbeille, réseau). Lève GLib.Error."""
    cancellable = Gio.Cancellable()
    out = []
    en = gfile.enumerate_children(ATTRS, Gio.FileQueryInfoFlags.NONE, cancellable)
    try:
        while not cancel.is_set():
            infos = en.next_files(500, cancellable)
            if not infos:
                break
            out.extend(rec_from_info(i, gfile) for i in infos)
    finally:
        en.close(None)
    return out


def rec_for_path(path: str) -> Optional[Rec]:
    """Un seul élément (événement de surveillance du dossier)."""
    name = os.path.basename(path)
    try:
        ls = os.lstat(path)
    except OSError:
        return None
    is_link = st.S_ISLNK(ls.st_mode)
    try:
        s = os.stat(path) if is_link else ls
    except OSError:
        s = ls
    r = Rec(name, path, st.S_ISDIR(s.st_mode), is_link)
    r.key = sort_name(name)
    if name in read_hidden_file(os.path.dirname(path)):
        r.hidden = True
    apply_stat(r, s)
    return r


# ---------------------------------------------------------------------------
# Tri
# ---------------------------------------------------------------------------

SORT_KEYS = ("name", "size", "type", "date", "duration", "perms", "owner", "location", "packed")


def _key_func(key: str) -> Callable:
    if key == "size":
        return lambda r: (r.get("count", -1) if r.is_dir else r.size, r.key)
    if key == "date":
        return lambda r: (r.mtime, r.key)
    if key == "type":
        return lambda r: (type_description(r.content_type), r.key)
    if key == "duration":
        return lambda r: (r.get("duration", -1.0), r.key)
    if key == "perms":
        return lambda r: (r.mode & 0o7777, r.key)
    if key == "owner":
        return lambda r: (owner_name(r.uid), r.key)
    if key == "location":
        return lambda r: (r.get("location", ""), r.key)
    if key == "packed":
        return lambda r: (r.get("packed", -1), r.key)
    return lambda r: r.key


def sort_recs(recs: list[Rec], key: str = "name", reverse: bool = False, folders_first: bool = True) -> list[Rec]:
    """Trie comme Nautilus : l'ordre inverse garde les dossiers en tête."""
    kf = _key_func(key)
    if folders_first:
        dirs = [r for r in recs if r.is_dir]
        files = [r for r in recs if not r.is_dir]
        dirs.sort(key=kf, reverse=reverse)
        files.sort(key=kf, reverse=reverse)
        return dirs + files
    return sorted(recs, key=kf, reverse=reverse)


_type_desc: dict = {}


def type_description(ctype: str) -> str:
    d = _type_desc.get(ctype)
    if d is None:
        d = _type_desc[ctype] = Gio.content_type_get_description(ctype) or ctype
    return d


_owners: dict = {}


def owner_name(uid: int) -> str:
    if uid < 0:
        return ""
    n = _owners.get(uid)
    if n is None:
        try:
            import pwd
            n = pwd.getpwuid(uid).pw_name
        except (KeyError, ImportError):
            n = str(uid)
        _owners[uid] = n
    return n


def count_children(path: str, show_hidden: bool) -> int:
    try:
        with os.scandir(path) as it:
            if show_hidden:
                return sum(1 for _ in it)
            return sum(1 for e in it if not e.name.startswith("."))
    except OSError:
        return -1
