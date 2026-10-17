"""Disques montés en permanence (fstab…), que le moniteur de volumes de GNOME masque souvent."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Points de montage du système : jamais affichés comme « périphériques ».
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/tmp", "/srv", "/opt",
                 "/recovery", "/root", "/nix", "/gnu"}
SYSTEM_PREFIXES = ("/boot/", "/usr/", "/var/", "/snap/", "/opt/", "/sys/", "/proc/", "/dev/", "/tmp/",
                   "/nix/", "/gnu/", "/run/")
USER_PREFIXES = ("/run/media/",)
IGNORED_FS = {"squashfs", "overlay", "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "autofs",
              "fuse.snapfuse", "nsfs", "tracefs", "debugfs", "securityfs", "pstore", "efivarfs", "bpf",
              "mqueue", "hugetlbfs", "configfs", "fusectl", "binfmt_misc", "ramfs", "erofs"}


@dataclass
class MountedDisk:
    path: str
    name: str
    device: str
    fstype: str


def _unescape(field: str) -> str:
    # /proc/mounts échappe les espaces et caractères spéciaux en octal (\040…)
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def _labels() -> dict:
    labels = {}
    base = "/dev/disk/by-label"
    try:
        for name in os.listdir(base):
            labels[os.path.realpath(os.path.join(base, name))] = _unescape(name.replace("\\x20", " "))
    except OSError:
        pass
    return labels


def is_user_mount(path: str, device: str, fstype: str, home: str = "") -> bool:
    if not device.startswith("/dev/") or device.startswith("/dev/loop") or fstype in IGNORED_FS:
        return False
    if path.startswith(USER_PREFIXES):
        return True
    if path in SYSTEM_MOUNTS or path.startswith(SYSTEM_PREFIXES):
        return False
    home = home or os.path.expanduser("~")
    if path == home or home.startswith(path.rstrip("/") + "/"):
        return False
    return True


def mounted_disks(mounts_file: str = "/proc/self/mounts", home: str = "") -> list[MountedDisk]:
    try:
        with open(mounts_file, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    labels = _labels()
    seen_paths, seen_devices, out = set(), set(), []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, path, fstype = _unescape(parts[0]), _unescape(parts[1]), parts[2]
        if path in seen_paths or not is_user_mount(path, device, fstype, home):
            continue
        real = os.path.realpath(device)
        if real in seen_devices:      # montage « bind » du même disque : on garde le premier
            continue
        seen_paths.add(path)
        seen_devices.add(real)
        name = labels.get(real) or os.path.basename(path.rstrip("/")) or path
        out.append(MountedDisk(path, name, device, fstype))
    return out


def _resolve_device(spec: str) -> str:
    for prefix, folder in (("UUID=", "by-uuid"), ("LABEL=", "by-label"), ("PARTUUID=", "by-partuuid"),
                           ("PARTLABEL=", "by-partlabel")):
        if spec.startswith(prefix):
            return os.path.realpath(os.path.join("/dev/disk", folder, spec[len(prefix):].strip('"')))
    return os.path.realpath(spec) if spec.startswith("/dev/") else ""


def fstab_entries(fstab: str = "/etc/fstab") -> tuple[set[str], set[str]]:
    """Points de montage et périphériques déclarés dans /etc/fstab : disques permanents,
    jamais proposés à l'éjection."""
    points, devs = set(), set()
    try:
        with open(fstab, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("/"):
                    points.add(_unescape(parts[1]).rstrip("/") or "/")
                    dev = _resolve_device(_unescape(parts[0]))
                    if dev:
                        devs.add(dev)
    except OSError:
        pass
    return points, devs


def mounts_signature(mounts_file: str = "/proc/self/mounts") -> str:
    try:
        with open(mounts_file, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
