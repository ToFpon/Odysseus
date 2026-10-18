"""Presse-papiers de fichiers compatible Nautilus (x-special/gnome-copied-files + text/uri-list)."""

from __future__ import annotations

from typing import Optional

from gi.repository import Gdk, Gio, GLib, GObject

GNOME_TYPE = "x-special/gnome-copied-files"


class ClipState(GObject.Object):
    """Mémorise les éléments coupés ou copiés par Odysseus (atténuation des éléments coupés,
    marque des éléments copiés)."""
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.paths: list[str] = []
        self.cut = False
        self._cut_set: set = set()
        self._copy_set: set = set()

    def set(self, paths: list[str], cut: bool):
        self.paths = list(paths)
        self.cut = cut
        self._cut_set = set(paths) if cut else set()
        self._copy_set = set() if cut else set(paths)
        self.emit("changed")

    def clear(self):
        self.set([], False)

    def is_cut(self, path: Optional[str]) -> bool:
        return bool(path) and path in self._cut_set

    def is_copied(self, path: Optional[str]) -> bool:
        return bool(path) and path in self._copy_set


state = ClipState()


def put(display: Gdk.Display, paths: list[str], cut: bool):
    # chemins locaux ou URI (admin://, smb://… sans chemin local)
    uris = [p if "://" in p else Gio.File.new_for_path(p).get_uri() for p in paths]
    gnome = ("cut" if cut else "copy") + "\n" + "\n".join(uris)
    providers = [
        Gdk.ContentProvider.new_for_bytes(GNOME_TYPE, GLib.Bytes.new(gnome.encode())),
        Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(("\r\n".join(uris) + "\r\n").encode())),
        Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list([Gio.File.new_for_uri(u) for u in uris])),
        Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new("\n".join(paths).encode("utf-8", "surrogateescape"))),
    ]
    cb = display.get_clipboard()
    cb.set_content(Gdk.ContentProvider.new_union(providers))
    state.set(paths, cut)
    if not getattr(cb, "_odysseus_watch", False):
        cb._odysseus_watch = True
        cb.connect("changed", _on_changed)


def _on_changed(cb: Gdk.Clipboard):
    """Une autre application (ou Nautilus) a remplacé le presse-papiers : plus rien n'est copié
    ni coupé chez nous."""
    if not cb.is_local() and state.paths:
        state.clear()


def _parse_uris(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = Gio.File.new_for_uri(line) if "://" in line else Gio.File.new_for_path(line)
        loc = f.get_path() or (f.get_uri() if "://" in line else None)
        if loc:
            out.append(loc)
    return out


def read(display: Gdk.Display, callback):
    """callback(paths, cut) — lit le presse-papiers, qu'il vienne d'Odysseus, de Nautilus ou d'ailleurs."""
    cb = display.get_clipboard()
    formats = cb.get_formats()
    if formats.contain_mime_type(GNOME_TYPE):
        def done(c, res):
            try:
                stream, _mime = c.read_finish(res)
            except GLib.Error:
                callback([], False)
                return
            _slurp(stream, lambda data: _gnome(data, callback))
        cb.read_async([GNOME_TYPE], GLib.PRIORITY_DEFAULT, None, done)
    elif formats.contain_mime_type("text/uri-list"):
        def done2(c, res):
            try:
                stream, _mime = c.read_finish(res)
            except GLib.Error:
                callback([], False)
                return
            _slurp(stream, lambda data: callback(_parse_uris(data.decode("utf-8", "replace")), False))
        cb.read_async(["text/uri-list"], GLib.PRIORITY_DEFAULT, None, done2)
    elif formats.contain_gtype(Gdk.FileList):
        def done3(c, res):
            try:
                v = c.read_value_finish(res)
                callback([f.get_path() or f.get_uri() for f in v.get_files()], False)
            except GLib.Error:
                callback([], False)
        cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, done3)
    else:
        callback([], False)


def _gnome(data: bytes, callback):
    lines = data.decode("utf-8", "replace").split("\n")
    cut = lines[0].strip() == "cut"
    callback(_parse_uris("\n".join(lines[1:])), cut)


def _slurp(stream: Gio.InputStream, done):
    chunks = []

    def step(s, res):
        try:
            b = s.read_bytes_finish(res)
        except GLib.Error:
            done(b"".join(chunks))
            return
        if b.get_size() == 0:
            s.close_async(GLib.PRIORITY_DEFAULT, None, None)
            done(b"".join(chunks))
            return
        chunks.append(b.get_data())
        s.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, step)
    stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, step)
