"""Paquets .deb (reprise de Deb Installer) : informations, dépendances à installer, installation."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class DebInfo:
    name: str
    version: str = ""
    arch: str = ""
    description: str = ""
    maintainer: str = ""
    size: str = ""
    installed: str = ""     # version déjà installée


def info(path: str) -> DebInfo:
    fields = {}
    try:
        out = subprocess.run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture", "Description",
                              "Maintainer", "Installed-Size"], capture_output=True, text=True, timeout=15).stdout
        key = None
        for line in out.splitlines():
            if line.startswith(" ") and key:
                t = line.strip()
                fields[key] += "\n" + ("" if t == "." else t)
            elif ":" in line:
                key, _, v = line.partition(":")
                fields[key.strip()] = v.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    d = DebInfo(fields.get("Package") or os.path.basename(path), fields.get("Version", ""),
                fields.get("Architecture", ""), fields.get("Description", ""), fields.get("Maintainer", ""),
                fields.get("Installed-Size", ""))
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Status}|${Version}", d.name], capture_output=True, text=True,
                           timeout=10)
        status, _, ver = r.stdout.partition("|")
        if "install ok installed" in status:
            d.installed = ver
    except (OSError, subprocess.SubprocessError):
        pass
    return d


def missing_dependencies(path: str) -> Optional[list[str]]:
    """Paquets que apt ajouterait (simulation). None si la simulation est impossible."""
    try:
        r = subprocess.run(["apt-get", "install", "--simulate", "--no-install-recommends", path],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    name = info(path).name
    deps = []
    for line in (r.stdout + r.stderr).splitlines():
        if line.startswith("Inst "):
            parts = line.split()
            if parts[1] != name:
                deps.append(parts[1] + (" " + parts[2] if len(parts) > 2 else ""))
    if r.returncode != 0 and not deps:
        return None
    return deps


def install_command(path: str) -> list[str]:
    return ["pkexec", "apt-get", "install", "-y", "--no-install-recommends", os.path.abspath(path)]
