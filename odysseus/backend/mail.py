"""« Courriel… » : nouveau message avec des pièces jointes.

xdg-email ne reconnaît Thunderbird que si la commande de son lanceur commence par « thunderbird ».
En snap (« env BAMF_DESKTOP_FILE_HINT=… /snap/bin/thunderbird %u », Ubuntu 24.04 / Zorin 18) ou en
Flatpak (« flatpak run … org.mozilla.Thunderbird @@u %u @@ »), il ne le voit pas et passe
« mailto:?attach=… » : Thunderbird ignore les pièces jointes d'une adresse mailto (sécurité) et
ouvre un message vide (retour d'utilisateur). Pour Thunderbird, on lance donc nous-mêmes la commande
du lanceur avec « -compose attachment='…' », comme le fait xdg-email quand il le reconnaît.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
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
    app = flatpak_app(argv)
    if app:
        paths = [export_to_flatpak(app, p) for p in paths]
    return argv + ["-compose", "attachment='" + ",".join(attachment_uri(p) for p in paths) + "'"]


def flatpak_app(argv: list[str]) -> Optional[str]:
    """Identifiant de l'application d'une commande « flatpak run … ID … », sinon None."""
    if not argv or os.path.basename(argv[0]) != "flatpak" or "run" not in argv:
        return None
    for w in argv[argv.index("run") + 1:]:
        if not w.startswith("-"):
            return w
    return None


def export_to_flatpak(app: str, path: str) -> str:
    """Chemin lisible par une application Flatpak : le fichier passe par le portail de documents
    (« flatpak document-export », comme le fait --file-forwarding pour les fichiers d'une commande).
    Thunderbird / Betterbird de Flathub ne voient pas le dossier personnel en entier : le message
    s'ouvrait sans pièce jointe, sans erreur (retour d'utilisateur, Betterbird). Échec → chemin d'origine."""
    try:
        out = subprocess.run(["flatpak", "document-export", "--app=" + app, "-r", path],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return path
    exported = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
    return exported if exported.startswith("/") else path


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
