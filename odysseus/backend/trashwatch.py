"""Contournement d'un bug de GVfs : au bout d'un moment (remontage, veille…), gvfsd-trash cesse de
voir les corbeilles des autres disques (.Trash-1000) ; les fichiers y sont bien, mais trash:/// ne
les montre plus. Relancer gvfsd-trash (GVfs le redémarre tout seul) les fait réapparaître.

À l'ouverture de la corbeille, on compte nous-mêmes les éléments des corbeilles (dossier personnel
et disques montés) ; si trash:/// en montre moins, on relance gvfsd-trash. Le jour où GVfs est
corrigé, les comptes sont égaux et rien ne se passe.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import fsinfo

# systèmes de fichiers sans corbeille, ou qu'un stat pourrait bloquer (réseau)
SKIP_TYPES = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs", "pstore",
              "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs", "configfs", "fusectl", "autofs", "efivarfs",
              "binfmt_misc", "squashfs", "overlay", "nsfs", "ramfs", "rpc_pipefs", "nfs", "nfs4", "cifs", "smb3",
              "smbfs", "9p", "fuse.sshfs", "fuse.gvfsd-fuse", "fuse.portal", "fuse.rclone", "davfs"}
MIN_INTERVAL = 60.0          # jamais plus d'une relance par minute
_last_restart = 0.0


def _count(folder: str) -> int:
    """Éléments de « files » qui ont leur fiche info/NOM.trashinfo (les orphelins, sans fiche, ne
    sont pas montrés par GVfs : ils fausseraient le compte)."""
    info = os.path.join(os.path.dirname(folder), "info")
    try:
        with os.scandir(folder) as it:
            names = [e.name for e in it]
        with os.scandir(info) as it:
            infos = {e.name for e in it}
    except OSError:
        return 0
    return sum(1 for n in names if n + ".trashinfo" in infos)


def trash_dirs(uid: int = -1) -> list[str]:
    """Dossiers « files » de toutes les corbeilles visibles : personnelle et disques montés."""
    uid = os.getuid() if uid < 0 else uid
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    out = [os.path.join(data, "Trash", "files")]
    for point, kind in fsinfo.mounts():
        if kind in SKIP_TYPES or kind.startswith("fuse.gvfs"):
            continue
        for d in (os.path.join(point, f".Trash-{uid}", "files"), os.path.join(point, ".Trash", str(uid), "files")):
            if d not in out:
                out.append(d)
    return out


def expected_count() -> int:
    return sum(_count(d) for d in trash_dirs())


def restart_gvfs_trash() -> bool:
    """Relance gvfsd-trash (une fois par minute au plus). True si une relance a eu lieu."""
    global _last_restart
    now = time.monotonic()
    if now - _last_restart < MIN_INTERVAL:
        return False
    _last_restart = now
    try:
        subprocess.run(["pkill", "-u", str(os.getuid()), "-x", "gvfsd-trash"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
