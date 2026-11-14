"""Métadonnées des photos (fonctionnalité `photo_metadata`) : retirer la position GPS ou toutes les
métadonnées, modifier auteur / copyright / description, décaler l'heure de prise de vue.

Par GExiv2 (gir1.2-gexiv2-0.10, la bibliothèque qu'emploient Nautilus et Shotwell) : seuls les blocs de
métadonnées sont réécrits, jamais l'image — pas de recompression (ImageMagick « -strip » réencodait le
JPEG). L'original n'est jamais modifié sur place : copie cachée dans le même dossier, métadonnées écrites
sur la copie, mise sur disque, échange d'un coup (ou nouvelle copie « nom (sans position).jpg »).

Annulation : l'original est gardé dans ~/.cache/odysseus/photo-undo par un lien dur (même disque :
instantané, aucune copie), sinon copié tant que le total reste raisonnable ; balayé au démarrage.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from datetime import datetime, timedelta
from typing import Optional

from gi.repository import GLib

from ..i18n import tr
from . import fileops

SUPPORTED = (".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff")
UNDO_DIR = os.path.join(GLib.get_user_cache_dir(), "odysseus", "photo-undo")
COPY_LIMIT = 512 * 1024 * 1024          # sauvegarde par copie (autre disque) : au plus 512 Mo par opération

AUTHOR = ("Exif.Image.Artist", "Xmp.dc.creator", "Iptc.Application2.Byline")
COPYRIGHT = ("Exif.Image.Copyright", "Xmp.dc.rights", "Iptc.Application2.Copyright")
DESCRIPTION = ("Exif.Image.ImageDescription", "Xmp.dc.description", "Iptc.Application2.Caption")
FIELDS = {"author": AUTHOR, "copyright": COPYRIGHT, "description": DESCRIPTION}
# lieu écrit en toutes lettres (ville, pays…) : aussi révélateur que les coordonnées
PLACE_TAGS = ("Iptc.Application2.City", "Iptc.Application2.SubLocation", "Iptc.Application2.ProvinceState",
              "Iptc.Application2.CountryName", "Iptc.Application2.CountryCode", "Iptc.Application2.LocationName",
              "Xmp.photoshop.City", "Xmp.photoshop.State", "Xmp.photoshop.Country", "Xmp.iptc.Location",
              "Xmp.iptc.CountryCode")
PLACE_PREFIXES = ("Xmp.exif.GPS", "Xmp.iptcExt.LocationCreated", "Xmp.iptcExt.LocationShown")
DATE_TAGS = ("Exif.Photo.DateTimeOriginal", "Exif.Photo.DateTimeDigitized", "Exif.Image.DateTime",
             "Xmp.xmp.CreateDate", "Xmp.photoshop.DateCreated", "Xmp.exif.DateTimeOriginal",
             "Xmp.exif.DateTimeDigitized", "Xmp.xmp.ModifyDate")


def supported(name: str) -> bool:
    return name.lower().endswith(SUPPORTED)


def _gexiv2():
    import gi
    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2
    return GExiv2


def available() -> bool:
    try:
        _gexiv2()
        return True
    except (ImportError, ValueError):
        return False


def _open(path: str):
    md = _gexiv2().Metadata()
    md.open_path(path)
    return md


def _tags(md) -> list[str]:
    return list(md.get_exif_tags()) + list(md.get_xmp_tags()) + list(md.get_iptc_tags())


def _value(md, names) -> str:
    for n in names:
        try:
            if md.try_has_tag(n):
                v = md.try_get_tag_interpreted_string(n) or ""
                v = v.replace('lang="x-default" ', "").strip()
                if v:
                    return v
        except GLib.Error:
            continue
    return ""


def _place_tags(md) -> list[str]:
    return [t for t in _tags(md) if t in PLACE_TAGS or t.startswith(PLACE_PREFIXES) or t.startswith("Exif.GPSInfo.")]


def summary(path: str) -> dict:
    """Ce que la photo révèle : position, lieu, appareil, date, auteur… (pour la fenêtre)."""
    md = _open(path)
    out = {k: _value(md, names) for k, names in FIELDS.items()}
    # PyGObject perd le booléen « trouvé » de try_get_gps_info (0, 0 sans position) : d'après les étiquettes
    out["gps"] = None
    if md.try_has_tag("Exif.GPSInfo.GPSLatitude") and md.try_has_tag("Exif.GPSInfo.GPSLongitude"):
        try:
            g = md.try_get_gps_info()
            out["gps"] = (g.latitude, g.longitude)                 # (latitude, longitude)
        except GLib.Error:
            out["gps"] = (0.0, 0.0)
    out["place"] = any(t in PLACE_TAGS or t.startswith(PLACE_PREFIXES[1:]) for t in _tags(md))
    out["camera"] = " ".join(x for x in (_value(md, ("Exif.Image.Make",)), _value(md, ("Exif.Image.Model",))) if x)
    out["date"] = _value(md, DATE_TAGS[:3])
    out["serial"] = bool(_value(md, ("Exif.Photo.BodySerialNumber", "Exif.Canon.SerialNumber",
                                     "Exif.Nikon3.SerialNumber", "Exif.Image.CameraSerialNumber")))
    out["count"] = len(_tags(md))
    return out


# ----------------------------------------------------------------------
# Dates

def _shift(text: str, delta: timedelta) -> Optional[str]:
    """Décale une date EXIF (« 2015:04:17 12:20:19 ») ou XMP (« 2015-04-17T12:20:19-07:00 ») en gardant
    sa forme ; None si illisible."""
    s = text.strip()
    try:
        d = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
        return (d + delta).strftime("%Y:%m:%d %H:%M:%S") + s[19:]
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if "T" not in s:
        # date seule : décalée de jours entiers seulement (timedelta(hours=-3).days vaut -1 en Python)
        days = int(delta.total_seconds() / 86400)
        return (d + timedelta(days=days)).date().isoformat() if days else s
    out = (d + delta).isoformat(timespec="seconds")
    return out[:-6] + "Z" if s.endswith("Z") and out.endswith("+00:00") else out


# ----------------------------------------------------------------------

def edit(md, strip_gps: bool = False, strip_all: bool = False, fields: Optional[dict] = None,
         shift: int = 0):
    """Applique les changements à des métadonnées ouvertes (sans écrire)."""
    if strip_all:
        G = _gexiv2()
        try:
            orient = md.try_get_orientation()
        except GLib.Error:
            orient = G.Orientation.UNSPECIFIED
        md.clear()
        if orient not in (G.Orientation.UNSPECIFIED, G.Orientation.NORMAL):
            md.try_set_orientation(orient)          # sinon la photo se retrouverait couchée
    elif strip_gps:
        try:
            md.try_delete_gps_info()
        except GLib.Error:
            pass
        for t in _place_tags(md):
            try:
                md.try_clear_tag(t)
            except GLib.Error:
                pass
    for key, value in (fields or {}).items():
        for name in FIELDS[key]:
            try:
                md.try_clear_tag(name)
            except GLib.Error:
                pass
        if value:
            for name in FIELDS[key]:
                try:
                    if name.startswith("Iptc.") and len(value.encode()) > 64 and key != "description":
                        continue                  # champs IPTC courts (Byline, Copyright : 32 / 128 octets)
                    md.try_set_tag_string(name, value)
                except GLib.Error:
                    pass
    if shift and not strip_all:
        delta = timedelta(seconds=shift)
        for name in DATE_TAGS:
            try:
                if md.try_has_tag(name):
                    new = _shift(md.try_get_tag_string(name) or "", delta)
                    if new:
                        md.try_set_tag_string(name, new)
            except GLib.Error:
                pass


def write(path: str, dest: Optional[str] = None, **changes) -> str:
    """Écrit les métadonnées modifiées. dest None : l'original est remplacé (copie cachée échangée d'un
    coup) ; sinon une nouvelle photo est posée sous dest (« nom (2) » si pris). Renvoie le chemin écrit."""
    folder = os.path.dirname(os.path.abspath(dest or path))
    tmp = fileops._tmp_name(folder, "photo")
    try:
        shutil.copy2(path, tmp)
        md = _open(tmp)
        edit(md, **changes)
        md.save_file(tmp)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if dest is None:
            os.replace(tmp, path)
            fileops._fsync_dir(folder)
            return path
        return fileops.place_new(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def copy_name(path: str, suffix: str) -> str:
    stem, ext = os.path.splitext(path)
    return f"{stem} ({suffix}){ext}"


# ----------------------------------------------------------------------
# Annulation : l'original est gardé tel quel

class Backups:
    """Originaux gardés pour Ctrl+Z, dans un dossier propre à l'opération."""

    def __init__(self):
        self.dir = os.path.join(UNDO_DIR, f"{int(time.time())}-{secrets.token_hex(4)}")
        self.copied = 0
        self.n = 0

    def keep(self, path: str) -> Optional[str]:
        os.makedirs(self.dir, exist_ok=True)
        self.n += 1
        dst = os.path.join(self.dir, str(self.n))
        try:
            os.link(path, dst)                         # même disque : instantané
            return dst
        except OSError:
            pass
        size = os.path.getsize(path)
        if self.copied + size > COPY_LIMIT:
            return None                                # trop gros pour être gardé : pas d'annulation
        shutil.copy2(path, dst)
        self.copied += size
        return dst


def restore(backup: str, path: str):
    """Remet `backup` sous `path` (copie cachée puis échange : la sauvegarde reste intacte)."""
    folder = os.path.dirname(os.path.abspath(path))
    tmp = fileops._tmp_name(folder, "photo")
    try:
        shutil.copy2(backup, tmp)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        fileops._fsync_dir(folder)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sweep_backups(max_age: float = 24 * 3600):
    """Au démarrage : sauvegardes de plus d'un jour retirées (l'annulation ne survit pas à Odysseus)."""
    try:
        entries = list(os.scandir(UNDO_DIR))
    except OSError:
        return
    now = time.time()
    for e in entries:
        try:
            if now - e.stat(follow_symlinks=False).st_mtime > max_age:
                shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            pass


def describe(info: dict) -> list[str]:
    """Ce que la photo révèle, en clair (pour la fenêtre)."""
    out = []
    if info.get("gps"):
        out.append(tr("position GPS"))
    if info.get("place"):
        out.append(tr("lieu"))
    if info.get("camera"):
        out.append(tr("appareil photo"))
    if info.get("serial"):
        out.append(tr("numéro de série de l'appareil"))
    if info.get("date"):
        out.append(tr("date de prise de vue"))
    if info.get("author"):
        out.append(tr("auteur"))
    return out
