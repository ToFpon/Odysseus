"""Système de fichiers d'un chemin, d'après /proc/self/mountinfo (sans toucher au dossier visé).

Les supports amovibles (FAT, exFAT, NTFS) réservent des surprises : noms insensibles à la casse,
caractères interdits (: ? * " < > | \\), pas de liens symboliques, fichiers limités à 4 Go en FAT32.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable, Optional

# Types de systèmes de fichiers « à la Windows »
FAT = {"vfat", "msdos", "fuse.fusefat"}
WINDOWS_LIKE = FAT | {"exfat", "fuse.exfat", "ntfs", "ntfs3", "fuseblk"}   # fuseblk : ntfs-3g, exfat-fuse
FAT_MAX_FILE = 4 * 1024 ** 3 - 1
_INVALID = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mounts() -> list[tuple[str, str]]:
    """[(point de montage, type)], du plus long au plus court."""
    out = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) >= 5 and right:
                    out.append((_unescape(fields[4]), right.split()[0]))
    except OSError:
        pass
    out.sort(key=lambda m: len(m[0]), reverse=True)
    return out


def fs_type(path: str) -> Optional[str]:
    """Type du système de fichiers qui contient path (préfixe le plus long, sans stat())."""
    path = os.path.abspath(path)
    for point, kind in mounts():
        if path == point or path.startswith(point.rstrip("/") + "/"):
            return kind
    return None


# Systèmes de fichiers réseau : un serveur qui ne répond plus (NAS éteint, Wi-Fi coupé) bloque tout
# appel système sur leurs chemins — stat, statvfs, inotify — de quelques secondes à plusieurs
# minutes. Jamais sur le fil principal (Nautilus se fige dans ce cas). gvfsd-fuse : les partages
# smb://, sftp://… de GVfs vus par leur chemin /run/user/UID/gvfs/…
NETWORK = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "9p", "afs", "ceph", "glusterfs", "davfs", "ncpfs",
           "fuse.sshfs", "fuse.gvfsd-fuse", "fuse.rclone", "fuse.s3fs", "fuse.curlftpfs", "fuse.glusterfs",
           "fuse.davfs2", "fuse.smbnetfs", "fuse.cryfs"}
_cached: tuple = (0.0, [])


def _mounts_recent() -> list[tuple[str, str]]:
    """mounts() relu au plus une fois par seconde (consulté à chaque clic, survol de dépôt…)."""
    global _cached
    now = time.monotonic()
    if now - _cached[0] > 1.0:
        _cached = (now, mounts())
    return _cached[1]


def is_network(path: str) -> bool:
    """Chemin sur un système de fichiers réseau (d'après mountinfo : aucun appel qui bloquerait)."""
    if not path or "://" in path:
        return False
    path = os.path.abspath(path)
    for point, kind in _mounts_recent():
        if path == point or path.startswith(point.rstrip("/") + "/"):
            return kind in NETWORK
    return False


def mount_point(path: str) -> str:
    """Point de montage qui contient path (sans stat)."""
    path = os.path.abspath(path)
    for point, _kind in _mounts_recent():
        if path == point or path.startswith(point.rstrip("/") + "/"):
            return point
    return "/"


_stalled: dict[str, threading.Thread] = {}     # point de montage → fil resté bloqué dessus


def bounded(fn: Callable, path: str, *args, timeout: float = 0.3, default=None):
    """fn(path, *args) dans un fil, attendu au plus `timeout` secondes : sinon `default` (le fil reste
    bloqué dans le noyau tant que le serveur ne répond pas, sans retenir l'appelant). Tant que ce fil
    n'est pas revenu, le montage est tenu pour muet : les appels suivants dessus rendent `default`
    sans attendre (ouvrir un dossier en enchaînait quatre, 1,2 s figées)."""
    point = mount_point(path)
    stuck = _stalled.get(point)
    if stuck is not None:
        if stuck.is_alive():
            return default
        del _stalled[point]
    box = []

    def run():
        try:
            box.append(fn(path, *args))
        except OSError:
            pass                  # erreur du serveur (déconnecté…) : `default`, sans trace dans la console
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if box:
        return box[0]
    if t.is_alive():
        _stalled[point] = t
    return default


def is_windows_like(path: str) -> bool:
    return fs_type(path) in WINDOWS_LIKE


def is_fat(path: str) -> bool:
    return fs_type(path) in FAT


def safe_name(name: str) -> str:
    """Nom accepté par FAT / exFAT / NTFS : caractères interdits remplacés par « _ », points et
    espaces finaux retirés (Windows les ignore et deux noms finiraient par se confondre), comme
    Nautilus."""
    out = _INVALID.sub("_", name).rstrip(" .")
    return out or "_"
