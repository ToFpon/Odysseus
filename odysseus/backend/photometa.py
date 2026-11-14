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
import shutil
import struct
from datetime import datetime, timedelta
from typing import Optional

from gi.repository import GLib

from ..i18n import tr
from . import fileops

SUPPORTED = (".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff")

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


_COORD_WORDS = ("latitude", "longitude", "altitude", "gpslat", "gpslon", "gpsalt", "gpsposition")
_MAKER_PLACES = (".City", ".State", ".Country", ".Landmark", ".Location")


def _is_coord(tag: str) -> bool:
    """Coordonnées hors du bloc GPS d'EXIF : Xmp.exif.GPS*, drones DJI (Xmp.drone-dji.GpsLatitude)…"""
    low = tag.lower()
    return tag.startswith("Xmp.exif.GPS") or (tag.startswith("Xmp.") and any(w in low for w in _COORD_WORDS))


def _is_place(tag: str) -> bool:
    return (tag in PLACE_TAGS or tag.startswith(PLACE_PREFIXES[1:])
            or (tag.startswith("Exif.Panasonic") and tag.endswith(_MAKER_PLACES)))


def _place_tags(md) -> list[str]:
    return [t for t in _tags(md) if t.startswith("Exif.GPSInfo.") or _is_coord(t) or _is_place(t)]


def summary(path: str) -> dict:
    """Ce que la photo révèle : position, lieu, appareil, date, auteur… (pour la fenêtre)."""
    md = _open(path)
    out = {k: _value(md, names) for k, names in FIELDS.items()}
    # PyGObject perd le booléen « trouvé » de try_get_gps_info (0, 0 sans position) : d'après les étiquettes
    out["gps"] = None
    all_tags = _tags(md)
    if md.try_has_tag("Exif.GPSInfo.GPSLatitude") and md.try_has_tag("Exif.GPSInfo.GPSLongitude"):
        try:
            g = md.try_get_gps_info()
            out["gps"] = (g.latitude, g.longitude)                 # (latitude, longitude)
        except GLib.Error:
            out["gps"] = (0.0, 0.0)
    elif any(_is_coord(t) for t in all_tags):
        out["gps"] = (0.0, 0.0)                                    # position en XMP seulement (drone, logiciel)
    out["place"] = any(_is_place(t) for t in all_tags)
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
    except OverflowError:
        return None
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if len(s) == 10:
        # date seule : décalée de jours entiers seulement (timedelta(hours=-3).days vaut -1 en Python)
        days = int(delta.total_seconds() / 86400)
        return (d + timedelta(days=days)).date().isoformat() if days else s
    try:
        shifted = d + delta
    except OverflowError:
        return None
    sep = "T" if "T" in s else " "                    # « 2015-04-17 12:20:19 » (certains téléphones)
    out = shifted.isoformat(sep=sep, timespec="seconds")
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
            try:
                md.try_set_tag_string("Iptc.Envelope.CharacterSet", "\x1b%G")   # IPTC en UTF-8 (accents)
            except GLib.Error:
                pass
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


# ----------------------------------------------------------------------
# Ce qu'exiv2 ne voit pas ou laisse derrière lui

def _png_chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + size
        if end > len(data):
            return
        yield kind, data[pos + 8:pos + 8 + size], data[pos:end]
        pos = end
        if kind == b"IEND":
            return


def _scrub_png(path: str, everything: bool):
    """PNG d'ImageMagick : il recopie l'EXIF en blocs texte « exif:GPSLatitude=… » (tEXt / zTXt / iTXt)
    qu'exiv2 ne voit ni ne retire. Position : les « exif:GPS* » ; tout : tous les « exif:* » et les
    « Raw profile type … » (blocs EXIF / XMP / IPTC d'ImageMagick et d'exiv2)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return
    out, changed = [data[:8]], False
    for kind, body, raw in _png_chunks(data):
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            key = body.split(b"\0", 1)[0].decode("latin-1", "replace")
            low = key.lower()
            drop = (low.startswith("exif:gps") or "latitude" in low or "longitude" in low) or (everything and (
                low.startswith(("exif:", "raw profile type", "xml:com.adobe.xmp")) or key in ("date:create", "date:modify", "date:timestamp")))
            if drop:
                changed = True
                continue
        out.append(raw)
    if changed:
        with open(path, "wb") as f:
            f.write(b"".join(out))


def _drop_webp_xmp(path: str):
    """WebP : exiv2 0.27 ne retire pas un bloc XMP vidé (la ville restait dans le fichier). On retire le bloc
    RIFF « XMP  » nous-mêmes, et le drapeau XMP de l'en-tête VP8X."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return
    out, pos = [], 12
    while pos + 8 <= len(data):
        kind, size = struct.unpack("<4sI", data[pos:pos + 8])
        end = pos + 8 + size + (size & 1)
        chunk = bytearray(data[pos:end])
        if kind == b"VP8X" and len(chunk) > 8:
            chunk[8] &= ~0x04 & 0xFF
        if kind != b"XMP ":
            out.append(bytes(chunk))
        pos = end
    body = b"WEBP" + b"".join(out)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def leaks(path: str, strip_gps: bool, strip_all: bool) -> list[str]:
    """Ce qui reste alors qu'on l'a retiré (vérification après écriture, avec exiv2 ET les octets bruts)."""
    md = _open(path)
    left = [t for t in _place_tags(md)] if (strip_gps or strip_all) else []
    if strip_all:
        left += [t for t in _tags(md) if t not in ("Exif.Image.Orientation", "Xmp.tiff.Orientation")
                 and not t.startswith("Exif.Thumbnail")]
    with open(path, "rb") as f:
        raw = f.read()
    if (strip_gps or strip_all) and (b"exif:GPS" in raw or b"GPSLatitude" in raw):
        left.append("GPSLatitude")
    return left


def sidecars(path: str) -> list[str]:
    """Fichiers XMP posés à côté (« photo.xmp », « photo.jpg.xmp » — darktable, digiKam, RawTherapee…) :
    ils gardent souvent la position et suivent la photo partout."""
    stem = os.path.splitext(path)[0]
    out = []
    for cand in (stem + ".xmp", stem + ".XMP", path + ".xmp", path + ".XMP"):
        if os.path.isfile(cand) and cand not in out:
            out.append(cand)
    return out


def _edit_file(tmp: str, changes: dict):
    md = _open(tmp)
    edit(md, **changes)
    md.save_file(tmp)
    everything, gps = changes.get("strip_all"), changes.get("strip_gps")
    low = tmp.lower()
    if low.endswith(".png") and (gps or everything):
        _scrub_png(tmp, bool(everything))
    if low.endswith(".webp") and (gps or everything) and leaks(tmp, bool(gps), bool(everything)):
        _drop_webp_xmp(tmp)
        if everything is False and changes.get("fields"):
            pass
    left = leaks(tmp, bool(gps), bool(everything))
    if left:
        # jamais « position retirée » alors qu'elle est encore là : l'original reste intact
        raise ValueError(tr("Position encore présente après écriture ({0}) : photo laissée intacte").format(
            ", ".join(left[:3])))


def write(path: str, dest: Optional[str] = None, **changes) -> str:
    """Écrit les métadonnées modifiées. dest None : l'original est remplacé (keep.rewrite : copie cachée
    échangée d'un coup, cible réelle d'un lien, lecture seule et propriétaire gardés) ; sinon une nouvelle
    photo est posée sous dest (« nom (2) » si pris). Les fichiers XMP posés à côté perdent aussi la position.
    Renvoie le chemin écrit."""
    from . import keep
    if dest is None:
        out = keep.rewrite(path, lambda tmp: _edit_file(tmp, changes))
    else:
        folder = os.path.dirname(os.path.abspath(dest))
        tmp = fileops._tmp_name(folder, "photo") + os.path.splitext(dest)[1].lower()
        try:
            shutil.copy2(keep.real(path), tmp)
            os.chmod(tmp, (os.stat(tmp).st_mode & 0o7777) | 0o200)
            _edit_file(tmp, changes)
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            out = fileops.place_new(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    if dest is None and (changes.get("strip_gps") or changes.get("strip_all")):
        for side in dict.fromkeys(sidecars(keep.real(path)) + sidecars(path)):   # à côté du lien et de la cible
            try:
                keep.rewrite(side, lambda tmp: _edit_side(tmp, changes))
            except Exception:  # noqa: BLE001 — fichier XMP illisible : la photo, elle, est faite
                pass
    return out


_PLACE_LOCALS = ("city", "state", "country", "countrycode", "location", "sublocation", "provincestate",
                 "countryname", "locationcreated", "locationshown")


def _edit_side(tmp: str, changes: dict):
    """Fichier XMP posé à côté : exiv2 le lit mais ne le réécrit pas. On retire nous-mêmes coordonnées et
    lieux (attributs et éléments), préfixes d'origine gardés. Jamais le reste, même pour « tout retirer » :
    ces fichiers portent aussi l'historique de retouche de darktable / digiKam."""
    import io
    import xml.etree.ElementTree as ET
    with open(tmp, "rb") as f:
        raw = f.read()
    for _ev, (prefix, uri) in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass
    root = ET.fromstring(raw)

    def bad(name: str) -> bool:
        local = name.rsplit("}", 1)[-1]
        low = local.lower()
        return (local.startswith("GPS") or any(w in low for w in _COORD_WORDS) or low in _PLACE_LOCALS)
    changed = False
    for el in list(root.iter()):
        for attr in [a for a in el.attrib if bad(a)]:
            del el.attrib[attr]
            changed = True
        for child in [c for c in list(el) if bad(c.tag)]:
            el.remove(child)
            changed = True
    if changed:
        text = ET.tostring(root, encoding="unicode")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + text + '\n<?xpacket end="w"?>\n')


def copy_name(path: str, suffix: str) -> str:
    stem, ext = os.path.splitext(path)
    return f"{stem} ({suffix}){ext}"


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
