"""« Courriel… » : nouveau message avec des pièces jointes.

xdg-email ne reconnaît Thunderbird que si la commande de son lanceur commence par « thunderbird ».
En snap (« env BAMF_DESKTOP_FILE_HINT=… /snap/bin/thunderbird %u », Ubuntu 24.04 / Zorin 18) ou en
Flatpak (« flatpak run … org.mozilla.Thunderbird @@u %u @@ »), il ne le voit pas et passe
« mailto:?attach=… » : Thunderbird ignore les pièces jointes d'une adresse mailto (sécurité) et
ouvre un message vide (retour d'utilisateur). Pour Thunderbird, on lance donc nous-mêmes la commande
du lanceur avec « -compose attachment='…' », comme le fait xdg-email quand il le reconnaît.
"""

from __future__ import annotations

import re
import shlex
from typing import Optional

from gi.repository import Gio

THUNDERBIRD = re.compile(r"thunderbird|icedove|betterbird", re.I)


def attachment_uri(path: str) -> str:
    """URI file:// d'une pièce jointe, sans virgule ni apostrophe en clair : Thunderbird coupe la
    liste aux virgules et la valeur est entre apostrophes."""
    return Gio.File.new_for_path(path).get_uri().replace(",", "%2C").replace("'", "%27")


def thunderbird_argv(commandline: str, paths: list[str]) -> Optional[list[str]]:
    """Commande du lanceur de Thunderbird (codes %u, %U… et marqueurs @@ de Flatpak retirés) +
    « -compose attachment='uri1,uri2' ». None si ce n'est pas Thunderbird ou si la commande est
    illisible."""
    if not commandline or not THUNDERBIRD.search(commandline):
        return None
    try:
        words = shlex.split(commandline)
    except ValueError:
        return None
    argv = [w for w in words if not re.fullmatch(r"%[a-zA-Z%]|@@[uf]?", w)]
    if not argv:
        return None
    return argv + ["-compose", "attachment='" + ",".join(attachment_uri(p) for p in paths) + "'"]


def compose_argv(paths: list[str]) -> list[str]:
    """Commande à lancer : Thunderbird directement s'il est le logiciel de courriel par défaut
    (quelle que soit sa forme : deb, snap, Flatpak), sinon xdg-email."""
    app = Gio.AppInfo.get_default_for_uri_scheme("mailto")
    argv = thunderbird_argv(app.get_commandline() or "", paths) if app is not None else None
    if argv is not None:
        return argv
    argv = ["xdg-email"]
    for p in paths:
        argv += ["--attach", p]
    return argv
