"""Partage réseau d'un dossier (Samba), comme l'extension nautilus-share : tout passe par `net usershare`
(add / delete / info), sans droits d'administrateur.

Droits du partage (ceux de nautilus-share) : « Everyone:R,<moi>:F » (les autres lisent), ou « Everyone:F »
si les autres peuvent créer et supprimer des fichiers ; `guest_ok=y|n` seulement si Samba autorise
l'accès invité (testparm « usershare allow guests »).

Sous Ubuntu / Zorin, l'utilisateur doit faire partie du groupe `sambashare` (« You do not have
permission to create a usershare ») et le service smbd doit tourner (« Maybe smbd is not running ») :
ces deux cas sont reconnus pour afficher la marche à suivre au lieu du message brut.

Noms de partage : sans casse pour Samba (« Test » remplaçait « test » sans prévenir), donc comparés en
casefold ; plus de 12 caractères : simple avertissement (vieux clients Windows), comme nautilus-share.

Droits du dossier : un partage invité demande lecture + traversée pour les autres, un partage en
écriture l'écriture ; seuls les bits manquants sont ajoutés (après accord), notés dans
share-permissions.json et retirés quand le partage s'arrête ou que l'écriture est retirée."""

from __future__ import annotations

import configparser
import getpass
import json
import os
import shutil
import stat as st
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from ..i18n import tr
from ..settings import CONFIG_DIR

PERMS_FILE = os.path.join(CONFIG_DIR, "share-permissions.json")
GUEST_BITS = st.S_IRGRP | st.S_IXGRP | st.S_IROTH | st.S_IXOTH
WRITE_BITS = st.S_IWGRP | st.S_IWOTH
NAME_WARN_LEN = 12
FORBIDDEN = '%<>*?|/\\+=;:",'


@dataclass
class Share:
    name: str
    path: str
    comment: str = ""
    writable: bool = False
    guest_ok: bool = False


class ShareError(Exception):
    """kind : « group » (pas dans sambashare), « service » (smbd arrêté), « missing » (Samba absent),
    « other »."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


_cache: dict[str, Share] = {}         # chemin → partage
_stamp = 0.0


def available() -> bool:
    return bool(shutil.which("net"))


def _run(args: list[str]) -> str:
    if not available():
        raise ShareError(tr("Samba n'est pas installé"), "missing")
    try:
        p = subprocess.run(["net", "usershare", *args], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShareError(str(e))
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        if "do not have permission" in msg or "Permission denied" in msg:
            raise ShareError(msg, "group")
        if "smbd is not running" in msg:
            raise ShareError(msg, "service")
        raise ShareError(msg or tr("« net usershare » a échoué ({0})").format(p.returncode))
    return p.stdout


def guests_allowed() -> bool:
    """Samba accepte-t-il l'accès invité pour les partages d'utilisateur ? (comme nautilus-share)"""
    if not shutil.which("testparm"):
        return False
    try:
        p = subprocess.run(["testparm", "-s", "--parameter-name=usershare allow guests"],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.stdout.strip()[:3].lower() == "yes"


def parse_info(text: str) -> dict[str, Share]:
    """Sortie de `net usershare info` (une section par partage) → chemin → partage."""
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    cp.optionxform = str
    try:
        cp.read_string(text)
    except configparser.Error:
        return {}
    out: dict[str, Share] = {}
    for name in cp.sections():
        sec = cp[name]
        path = sec.get("path", "")
        if not path:
            continue
        acl = sec.get("usershare_acl", "")
        out.setdefault(os.path.normpath(path), Share(
            name, os.path.normpath(path), sec.get("comment", ""),
            "Everyone:F" in acl, sec.get("guest_ok", "n").strip().lower() == "y"))
    return out


def refresh() -> dict[str, Share]:
    """Relit les partages (lève ShareError). Un dossier peut avoir plusieurs partages : le premier."""
    global _cache, _stamp
    _cache = parse_info(_run(["info"]))
    _stamp = time.monotonic()
    return _cache


def stale(max_age: float = 10.0) -> bool:
    return time.monotonic() - _stamp > max_age


def mark_read():
    """Lecture tentée (même en échec) : pas de nouvel essai avant max_age."""
    global _stamp
    _stamp = time.monotonic()


def shares(max_age: float = 10.0) -> dict[str, Share]:
    """Partages connus ; relus s'ils datent de plus de max_age secondes. Ne lève jamais (Samba absent,
    erreur : aucun partage) — pour l'emblème, qui ne doit rien bloquer."""
    if time.monotonic() - _stamp > max_age:
        try:
            refresh()
        except ShareError:
            pass
    return _cache


def cached() -> dict[str, Share]:
    """Partages déjà lus, sans rien relancer (dessin des lignes)."""
    return _cache


def share_for(path: str) -> Optional[Share]:
    return shares().get(os.path.normpath(path))


def validate_name(name: str, path: str) -> tuple[str, str]:
    """(erreur, avertissement) pour un nom de partage, comme nautilus-share (+ casse ignorée)."""
    name = name.strip()
    if not name:
        return tr("Le nom du partage ne peut pas être vide"), ""
    bad = sorted({c for c in name if c in FORBIDDEN})
    if bad:
        return tr("Caractères interdits dans un nom de partage : {0}").format(" ".join(bad)), ""
    for p, s in _cache.items():
        if s.name.casefold() == name.casefold() and p != os.path.normpath(path):
            return tr("Un autre partage a déjà le même nom"), ""
    if len(name) > NAME_WARN_LEN:
        return "", tr("Le nom du partage est trop long")
    return "", ""


def _acl(writable: bool) -> str:
    return "Everyone:F" if writable else f"Everyone:R,{getpass.getuser()}:F"


def add(share: Share, guests: Optional[bool] = None):
    """Crée (ou remplace, s'il porte le même nom) le partage. guests : Samba autorise-t-il l'invité ?"""
    if guests is None:
        guests = guests_allowed()
    args = ["add", share.name, share.path, share.comment, _acl(share.writable)]
    if guests:
        args.append("guest_ok=y" if share.guest_ok else "guest_ok=n")
    _run(args)
    refresh()


def remove(name: str):
    _run(["delete", name])
    refresh()


def apply(old: Optional[Share], new: Optional[Share], guests: Optional[bool] = None):
    """Passe de l'ancien partage du dossier au nouveau (None : pas de partage). Renommé : l'ancien nom
    est retiré une fois le nouveau créé (s'il échoue, l'ancien partage reste)."""
    if new is not None:
        add(new, guests)
        if old is not None and old.name.casefold() != new.name.casefold():
            try:
                remove(old.name)
            except ShareError:
                pass
    elif old is not None:
        remove(old.name)


# ---------------------------------------------------------------------------
# Droits du dossier
# ---------------------------------------------------------------------------
def missing_bits(path: str, guest_ok: bool, writable: bool) -> int:
    """Bits à ajouter au dossier pour que le partage fonctionne (0 : rien)."""
    need = (GUEST_BITS if guest_ok else 0) | (WRITE_BITS if writable else 0)
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return 0
    return need & ~mode & 0o777


def describe_bits(bits: int) -> list[str]:
    out = []
    if bits & (st.S_IRGRP | st.S_IROTH):
        out.append(tr("lecture par des tiers"))
    if bits & WRITE_BITS:
        out.append(tr("écriture par des tiers"))
    if bits & (st.S_IXGRP | st.S_IXOTH):
        out.append(tr("exécution par des tiers"))
    return out


def _load_perms() -> dict:
    try:
        with open(PERMS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_perms(data: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PERMS_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(PERMS_FILE + ".tmp", PERMS_FILE)
    except OSError:
        pass


def add_bits(path: str, bits: int):
    """Ajoute les bits au dossier (jamais à travers un lien) et les note pour les retirer plus tard."""
    if not bits:
        return
    if os.path.islink(path):
        raise ShareError(tr("Impossible de modifier les permissions du dossier « {0} »").format(path))
    mode = os.stat(path).st_mode
    try:
        os.chmod(path, (mode | bits) & 0o7777)
    except OSError as e:
        raise ShareError(tr("Impossible de modifier les permissions du dossier « {0} »").format(path)
                         + f"\n{e.strerror}")
    data = _load_perms()
    key = os.path.normpath(path)
    data[key] = data.get(key, 0) | bits
    _save_perms(data)


def restore_bits(path: str, keep: int = 0):
    """Retire les bits ajoutés par Odysseus pour ce partage, sauf ceux encore nécessaires (keep)."""
    data = _load_perms()
    key = os.path.normpath(path)
    added = data.get(key, 0)
    drop = added & ~keep
    if not drop:
        return
    try:
        if not os.path.islink(path):
            os.chmod(path, os.stat(path).st_mode & ~drop & 0o7777)
    except OSError:
        return
    rest = added & keep
    if rest:
        data[key] = rest
    else:
        data.pop(key, None)
    _save_perms(data)


def needed_after(share: Optional[Share]) -> int:
    """Bits qui doivent rester pour un partage (0 sans partage)."""
    if share is None:
        return 0
    return (GUEST_BITS if share.guest_ok else 0) | (WRITE_BITS if share.writable else 0)
