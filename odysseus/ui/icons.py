"""Icônes des éléments : thème, dossiers spéciaux, couleurs de dossiers, miniatures."""

from __future__ import annotations

import os
import stat as st
from collections import OrderedDict
from typing import Optional

from gi.repository import Gdk, Gio, GLib, Gtk

from .. import features
from ..backend import folder_colors, thumbnails
from ..backend.scanner import Rec
from ..backend.workers import pool, thumb_pool
from ..settings import settings

_ctype_icons: dict = {}
_special: Optional[dict] = None
_textures: "OrderedDict[str, Gdk.Texture]" = OrderedDict()
TEXTURE_CACHE = 600

SPECIAL_ICONS = [
    (GLib.UserDirectory.DIRECTORY_DESKTOP, "user-desktop"),
    (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "folder-documents"),
    (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "folder-download"),
    (GLib.UserDirectory.DIRECTORY_MUSIC, "folder-music"),
    (GLib.UserDirectory.DIRECTORY_PICTURES, "folder-pictures"),
    (GLib.UserDirectory.DIRECTORY_PUBLIC_SHARE, "folder-publicshare"),
    (GLib.UserDirectory.DIRECTORY_TEMPLATES, "folder-templates"),
    (GLib.UserDirectory.DIRECTORY_VIDEOS, "folder-videos"),
]


def special_folders() -> dict:
    global _special
    if _special is None:
        _special = {GLib.get_home_dir(): "user-home"}
        for d, icon in SPECIAL_ICONS:
            p = GLib.get_user_special_dir(d)
            if p and p != GLib.get_home_dir():
                _special[p] = icon
    return _special


def theme() -> Gtk.IconTheme:
    return Gtk.IconTheme.get_for_display(Gdk.Display.get_default())


def has_icon(name: str) -> bool:
    return theme().has_icon(name)


def first_icon(*names: str) -> str:
    t = theme()
    for n in names:
        if t.has_icon(n):
            return n
    return names[-1]


def ctype_icon(ctype: str) -> Gio.Icon:
    icon = _ctype_icons.get(ctype)
    if icon is None:
        icon = _ctype_icons[ctype] = Gio.content_type_get_icon(ctype)
    return icon


def gicon_for(rec: Rec) -> Gio.Icon:
    meta = rec.get("meta")
    if meta:
        if meta.get("icon_uri"):
            return Gio.FileIcon.new(Gio.File.new_for_uri(meta["icon_uri"]))
        if meta.get("icon_name"):
            return Gio.ThemedIcon.new(meta["icon_name"])
    g = rec.get("gicon")
    if g is not None:
        return g
    if rec.is_dir:
        name = special_folders().get(rec.path) if rec.path else None
        return Gio.ThemedIcon.new_with_default_fallbacks(name) if name else Gio.ThemedIcon.new("folder")
    ctype = rec.content_type
    if rec.is_executable and Gio.content_type_is_unknown(ctype):
        return Gio.ThemedIcon.new("application-x-executable")
    return ctype_icon(ctype)


_ids: Optional[tuple] = None


def _access(rec: Rec) -> tuple[bool, bool]:
    """(lisible, modifiable) d'après les droits de l'élément, sans appel système (comme le
    test d'accès de Nautilus, ACL mises à part). Inconnu (GIO, archive) : pas d'emblème."""
    global _ids
    if _ids is None:
        _ids = (os.geteuid(), set(os.getgroups()) | {os.getegid()})
    uid, groups = _ids
    if uid == 0 or not rec.stat_ok or not rec.mode or rec.uid < 0 or not rec.path:
        return True, True
    m = rec.mode
    if rec.uid == uid:
        return bool(m & st.S_IRUSR), bool(m & st.S_IWUSR)
    if rec.gid in groups:
        return bool(m & st.S_IRGRP), bool(m & st.S_IWGRP)
    return bool(m & st.S_IROTH), bool(m & st.S_IWOTH)


def emblems_for(rec: Rec) -> list[str]:
    """Emblèmes comme Nautilus : lien, puis non modifiable (cadenas), non lisible (croix), puis ceux
    choisis par l'utilisateur (Folder Color)."""
    out = []
    if rec.is_link:
        out.append("emblem-symbolic-link")
    readable, writable = _access(rec)
    if not writable:
        out.append("emblem-readonly")
    if not readable:
        out.append("emblem-unreadable")
    meta = rec.get("meta")
    if meta and meta.get("emblems"):
        out.extend(meta["emblems"])
    return out


# ---------------------------------------------------------------------------
# Requêtes paresseuses (appelées au moment d'afficher une ligne)
# ---------------------------------------------------------------------------

def request_meta(rec: Rec, model):
    """Couleur de dossier / emblèmes (métadonnées GVfs), lus hors du fil principal."""
    if not rec.path or rec.get("meta") is not None or not features.enabled("folder_colors"):
        return
    path = rec.path

    def done(meta):
        rec.set("meta", meta or {})
        if meta:
            model.notify_rec(rec)
    rec.set("meta", {})       # évite les demandes en double avant la réponse
    pool.submit(("meta", path), lambda: folder_colors.read(path), done)


def thumbnails_wanted(rec: Rec) -> bool:
    if not features.enabled("thumbnails") or rec.is_dir or not rec.path:
        return False
    policy = settings["thumbnails"]
    if policy == "never":
        return False
    if policy == "local" and _is_remote(rec.path):
        return False
    return thumbnails.can_thumbnail(rec.content_type)


_remote_cache: dict = {}


def _is_remote(path: str) -> bool:
    folder = os.path.dirname(path)
    r = _remote_cache.get(folder)
    if r is None:
        r = folder.startswith(("/run/user/", "/media/")) and "gvfs" in folder
        _remote_cache[folder] = r
    return r


def texture(path: str) -> Optional[Gdk.Texture]:
    t = _textures.get(path)
    if t is not None:
        _textures.move_to_end(path)
    return t


def _remember(path: str, tex: Gdk.Texture):
    _textures[path] = tex
    while len(_textures) > TEXTURE_CACHE:
        _textures.popitem(last=False)


def request_thumbnail(rec: Rec, pixels: int, model) -> Optional[Gdk.Texture]:
    """Renvoie la miniature si elle est prête, sinon la demande en arrière-plan."""
    if rec.get("thumb_fail"):
        return None
    png = rec.get("thumb")
    if png:
        tex = texture(png)
        if tex is not None:
            return tex
    path, mtime, ctype = rec.path, rec.mtime, rec.content_type
    key = ("thumb", path, thumbnails.size_class(pixels))

    def work():
        found = thumbnails.lookup(path, mtime, pixels)
        if not found:
            if thumbnails.failed(path, mtime):
                return None
            found = thumbnails.generate(path, ctype, mtime, pixels)
        if not found:
            return None
        try:
            return found, Gdk.Texture.new_from_filename(found)
        except GLib.Error:
            return None

    def done(result):
        if not result:
            rec.set("thumb_fail", True)
            return
        png_path, tex = result
        _remember(png_path, tex)
        rec.set("thumb", png_path)
        model.notify_rec(rec)
    thumb_pool.submit(key, work, done)
    return None
