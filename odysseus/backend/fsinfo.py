"""Système de fichiers d'un chemin, d'après /proc/self/mountinfo (sans toucher au dossier visé).

Les supports amovibles (FAT, exFAT, NTFS) réservent des surprises : noms insensibles à la casse,
caractères interdits (: ? * " < > | \\), pas de liens symboliques, fichiers limités à 4 Go en FAT32.
"""

from __future__ import annotations

import os
import re
from typing import Optional

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
