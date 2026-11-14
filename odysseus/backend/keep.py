"""Réécrire un fichier sans risque, et garder l'original pour Ctrl+Z (étiquettes audio, métadonnées photo).

`rewrite` : le fichier n'est jamais modifié sur place — copie cachée dans le même dossier (avec la même
extension : mutagen reconnaît un MP3 sans étiquette à son nom), modification de la copie, mise sur disque,
échange d'un coup. Un lien symbolique est suivi (c'est sa cible qui change, le lien reste un lien) ; un
fichier à plusieurs liens durs est refusé (l'échange casserait les autres liens) ; propriétaire et groupe
sont gardés quand c'est permis (dossier partagé en groupe).

`Backups` : l'original est gardé tel quel dans ~/.cache/odysseus/undo, par lien dur (même disque : rien
n'est copié) ou par copie (autre disque, 512 Mo au plus par opération) ; Ctrl+Z le remet à l'identique,
octet pour octet — pochettes secondaires, étiquettes inconnues, version ID3 comprises. Balayé au
démarrage au-delà d'un jour.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from typing import Callable, Optional

from gi.repository import GLib

from ..i18n import tr
from . import fileops

UNDO_DIR = os.path.join(GLib.get_user_cache_dir(), "odysseus", "undo")
COPY_LIMIT = 512 * 1024 * 1024


class MultipleLinks(OSError):
    pass


def _fsync(path: str):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def real(path: str) -> str:
    return os.path.realpath(path)


def _own_links(st: os.stat_result) -> int:
    """Liens durs vers ce fichier posés par nos propres sauvegardes (~/.cache/odysseus/undo) : ils ne
    comptent pas comme « plusieurs liens » — sinon la modification suivant une sauvegarde était refusée."""
    n = 0
    try:
        ops = list(os.scandir(UNDO_DIR))
    except OSError:
        return 0
    for op in ops:
        try:
            for e in os.scandir(op.path):
                s = e.stat(follow_symlinks=False)
                if s.st_ino == st.st_ino and s.st_dev == st.st_dev:
                    n += 1
        except OSError:
            continue
    return n


def rewrite(path: str, edit: Callable[[str], None]) -> str:
    """Applique `edit(copie)` à une copie cachée de `path` (cible réelle d'un lien), puis l'échange avec
    l'original. Renvoie le chemin réellement réécrit. L'original reste intact en cas d'échec."""
    target = real(path)
    st = os.stat(target)
    if st.st_nlink > 1 and st.st_nlink - _own_links(st) > 1:
        raise MultipleLinks(0, tr("Ce fichier a plusieurs liens durs : le modifier les séparerait"))
    folder = os.path.dirname(target)
    tmp = fileops._tmp_name(folder, "edit") + os.path.splitext(target)[1].lower()
    try:
        shutil.copy2(target, tmp)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)          # copy2 ne garde ni propriétaire ni groupe
        except OSError:
            pass
        os.chmod(tmp, (st.st_mode & 0o7777) | 0o200)     # photo / morceau en lecture seule : copie modifiable
        edit(tmp)
        os.chmod(tmp, st.st_mode & 0o7777)
        _fsync(tmp)
        os.replace(tmp, target)
        fileops._fsync_dir(folder)
        return target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restore(backup: str, path: str):
    """Remet `backup` sous `path` (cible réelle d'un lien), par une copie cachée échangée d'un coup :
    la sauvegarde reste intacte (Rétablir / annuler de nouveau)."""
    target = real(path)
    folder = os.path.dirname(target)
    tmp = fileops._tmp_name(folder, "edit") + os.path.splitext(target)[1].lower()
    try:
        st = os.stat(target) if os.path.exists(target) else None
        shutil.copy2(backup, tmp)
        if st is not None:
            try:
                os.chmod(tmp, st.st_mode & 0o7777)
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        _fsync(tmp)
        os.replace(tmp, target)
        fileops._fsync_dir(folder)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Backups:
    """Versions gardées pour Ctrl+Z, dans un dossier propre à l'opération."""

    def __init__(self, kind: str = "edit"):
        self.dir = os.path.join(UNDO_DIR, f"{int(time.time())}-{kind}-{secrets.token_hex(4)}")
        self.copied = 0
        self.n = 0

    @staticmethod
    def stamp(path: str) -> tuple:
        """Empreinte légère de l'état d'un fichier (taille, date à la nanoseconde, inode)."""
        st = os.stat(real(path))
        return (st.st_size, st.st_mtime_ns, st.st_ino)

    def keep(self, path: str) -> Optional[str]:
        """Garde l'état actuel de `path` ; None s'il est trop gros pour être copié (autre disque)."""
        os.makedirs(self.dir, exist_ok=True)
        self.n += 1
        dst = os.path.join(self.dir, str(self.n))
        src = real(path)
        try:
            os.link(src, dst)                          # même disque : instantané, rien n'est copié
            return dst
        except OSError:
            pass
        size = os.path.getsize(src)
        if self.copied + size > COPY_LIMIT:
            return None
        shutil.copy2(src, dst)
        self.copied += size
        return dst


def sweep(max_age: float = 24 * 3600):
    """Au démarrage : versions gardées de plus d'un jour retirées (et l'ancien dossier photo-undo)."""
    old = os.path.join(GLib.get_user_cache_dir(), "odysseus", "photo-undo")
    for base in (UNDO_DIR, old):
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        now = time.time()
        for e in entries:
            try:
                if now - e.stat(follow_symlinks=False).st_mtime > max_age:
                    shutil.rmtree(e.path, ignore_errors=True)
            except OSError:
                pass
