"""Renommage de plusieurs éléments à la fois, comme le dialogue « Renommer » de Nautilus 46
(nautilus-batch-rename-*.c) : un modèle fait de texte et de balises (« [Nom du fichier original] »,
« [01, 02, 03] », métadonnées…), ou rechercher / remplacer un texte dans les noms.

Ici, sans GTK : calcul des nouveaux noms, conflits, métadonnées (EXIF, ffprobe) et renommage sûr
(chaînes a → b → c et permutations a ↔ b gérées, jamais d'écrasement)."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from gi.repository import GLib

from ..i18n import tr
from . import fileops, fsinfo, media

# balises de numérotation : (identifiant, largeur)
NUMBERING = (("num1", 1), ("num2", 2), ("num3", 3))
# balises de métadonnées, dans l'ordre du menu de Nautilus
METADATA = ("creation_date", "camera", "season", "episode", "track", "artist", "title", "album")
ORIGINAL = "original"

SORTS = ("name_asc", "name_desc", "first_modified", "last_modified", "first_created", "last_created")


def tag_label(tag: str) -> str:
    """Texte d'une balise dans le modèle, entre crochets (celui de Nautilus)."""
    return {
        ORIGINAL: tr("Nom du fichier original"),
        "num1": "1, 2, 3", "num2": "01, 02, 03", "num3": "001, 002, 003",
        "creation_date": tr("Date de création"), "camera": tr("Modèle de l'appareil photo"),
        "season": tr("Numéro de saison"), "episode": tr("Numéro d'épisode"),
        "track": tr("Numéro de piste"), "artist": tr("Nom de l'artiste"), "title": tr("Titre"),
        "album": tr("Nom de l'album"),
    }[tag]


def tag_text(tag: str) -> str:
    return f"[{tag_label(tag)}]"


ALL_TAGS = (ORIGINAL,) + tuple(t for t, _w in NUMBERING) + METADATA


def split_template(text: str) -> list:
    """« [01, 02, 03] Paris [Date de création] » → [("tag", "num2"), ("text", " Paris "), ("tag", …)].
    Un crochet qui n'est pas une balise connue reste du texte."""
    known = {tag_text(t): t for t in ALL_TAGS}
    out: list = []
    i = 0
    buf = ""
    while i < len(text):
        if text[i] == "[":
            end = text.find("]", i)
            if end != -1 and text[i:end + 1] in known:
                if buf:
                    out.append(("text", buf))
                    buf = ""
                out.append(("tag", known[text[i:end + 1]]))
                i = end + 1
                continue
        buf += text[i]
        i += 1
    if buf:
        out.append(("text", buf))
    return out


def tag_spans(text: str) -> list[tuple[int, int]]:
    """Positions (début, fin) des balises dans le texte, en caractères : on les efface d'un bloc."""
    spans, pos = [], 0
    for kind, value in split_template(text):
        length = len(tag_text(value)) if kind == "tag" else len(value)
        if kind == "tag":
            spans.append((pos, pos + length))
        pos += length
    return spans


def uses_numbering(text: str) -> bool:
    return any(k == "tag" and v.startswith("num") for k, v in split_template(text))


@dataclass
class Item:
    path: str
    is_dir: bool = False
    mtime: float = 0.0
    meta: dict = field(default_factory=dict)       # métadonnées lues (read_metadata)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


def _collate(name: str):
    return GLib.utf8_collate_key_for_filename(name, -1)


def ordered(items: list[Item], sort: str) -> list[Item]:
    """Ordre de la numérotation automatique (menu « Ordre de numérotation automatique »)."""
    if sort == "name_desc":
        return sorted(items, key=lambda it: _collate(it.name), reverse=True)
    if sort in ("first_modified", "last_modified"):
        return sorted(items, key=lambda it: (it.mtime, _collate(it.name)), reverse=sort == "last_modified")
    if sort in ("first_created", "last_created"):
        def created(it):
            d = it.meta.get("created")
            return d.timestamp() if d else it.mtime
        return sorted(items, key=lambda it: (created(it), _collate(it.name)), reverse=sort == "last_created")
    return sorted(items, key=lambda it: _collate(it.name))


def _format_date(d: datetime) -> str:
    """Comme Nautilus : la date courte de la langue (%x), « / » remplacés par « - »."""
    g = GLib.DateTime.new_local(d.year, d.month, d.day, d.hour, d.minute, d.second)
    return (g.format("%x") if g else d.strftime("%Y-%m-%d")).replace("/", "-")


def meta_value(item: Item, tag: str) -> str:
    m = item.meta
    if tag == "creation_date":
        return _format_date(m["created"]) if m.get("created") else ""
    if tag == "track":
        try:
            return "%02d" % int(str(m.get("track", "")).split("/")[0])
        except ValueError:
            return ""
    return str(m.get(tag, "") or "")


def available_tags(items: list[Item]) -> list[str]:
    """Métadonnées présentes dans TOUS les éléments (Nautilus ne propose que celles-là)."""
    return [t for t in METADATA if items and all(meta_value(it, t) for it in items)]


def format_name(item: Item, chunks: list, count: int) -> str:
    """Nouveau nom d'après le modèle ; l'extension d'un fichier est gardée (comme Nautilus)."""
    stem, ext = (item.name, "") if item.is_dir else fileops.split_ext(item.name)
    out = ""
    for kind, value in chunks:
        if kind == "text":
            out += value
        elif value == ORIGINAL:
            out += stem
        elif value.startswith("num"):
            width = dict(NUMBERING)[value]
            out += str(count).zfill(width)
        else:
            out += meta_value(item, value)
    if not out:
        return item.name
    return out + ext


def template_names(items: list[Item], template: str, sort: str) -> dict[str, str]:
    """{chemin: nouveau nom} pour le mode modèle."""
    chunks = split_template(template)
    return {it.path: format_name(it, chunks, n) for n, it in enumerate(ordered(items, sort), 1)}


def replace_names(items: list[Item], find: str, replace: str) -> dict[str, str]:
    """{chemin: nouveau nom} pour « Rechercher et remplacer un texte » (toutes les occurrences)."""
    return {it.path: (it.name.replace(find, replace) if find else it.name) for it in items}


def name_error(name: str) -> str:
    """Message de Nautilus pour un nom inutilisable, sinon chaîne vide."""
    if not name:
        return tr("Le nom ne peut pas être vide.")
    if "/" in name:
        return tr("Le nom ne peut pas contenir « / ».")
    if name == ".":
        return tr("« . » n'est pas un nom valide.")
    if name == "..":
        return tr("« .. » n'est pas un nom valide.")
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        return tr("Le nom est trop long")
    return ""


def _key(folder: str, name: str, folded: bool) -> tuple:
    return folder, (name.casefold() if folded else name)


def conflicts(items: list[Item], new_names: dict[str, str],
              listdir: Callable[[str], list] = os.listdir) -> dict[str, str]:
    """{chemin: message} des éléments qui ne peuvent pas prendre leur nouveau nom : nom invalide,
    deux éléments vers le même nom (« ne serait pas un nouveau nom unique »), ou nom déjà pris
    par un élément qui n'est pas renommé (« créerait un conflit avec un fichier existant »).
    Sur FAT / NTFS, la casse ne compte pas."""
    out: dict[str, str] = {}
    folded_of = {}
    present: dict[str, set] = {}
    for it in items:
        if it.folder not in present:
            folded_of[it.folder] = fsinfo.is_windows_like(os.path.realpath(it.folder or "."))
            try:
                names = listdir(it.folder or ".")
            except OSError:
                names = []
            present[it.folder] = {_key(it.folder, n, folded_of[it.folder])[1] for n in names}
    renamed = {_key(it.folder, it.name, folded_of[it.folder]) for it in items}
    seen: dict[tuple, int] = {}
    for it in items:
        new = new_names.get(it.path, it.name)
        seen[_key(it.folder, new, folded_of[it.folder])] = seen.get(_key(it.folder, new, folded_of[it.folder]), 0) + 1
    for it in items:
        new = new_names.get(it.path, it.name)
        err = name_error(new)
        if err:
            out[it.path] = err
            continue
        k = _key(it.folder, new, folded_of[it.folder])
        if seen[k] > 1:
            out[it.path] = tr("« {0} » ne serait pas un nouveau nom unique.").format(new)
        elif k[1] in present[it.folder] and k not in renamed:
            out[it.path] = tr("« {0} » créerait un conflit avec un fichier existant.").format(new)
    return out


def apply(pairs: list[tuple[str, str]], progress: Optional[Callable[[int, int], None]] = None
          ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Renomme [(chemin, nouveau nom)…]. Ordre calculé pour les chaînes (a → b alors que b → c :
    b d'abord) ; une permutation (a ↔ b) passe par un nom temporaire caché. Jamais d'écrasement
    (renameat2 RENAME_NOREPLACE). Renvoie (faits [(ancien chemin, nouveau chemin)], erreurs
    [(chemin, message)]) ; un élément en erreur garde son nom, les autres continuent."""
    todo = []
    for path, new in pairs:
        if os.path.basename(path) != new:
            todo.append([path, os.path.join(os.path.dirname(path), new)])
    folded = {}

    def key(p):
        folder = os.path.dirname(p)
        if folder not in folded:
            folded[folder] = fsinfo.is_windows_like(os.path.realpath(folder or "."))
        return _key(folder, os.path.basename(p), folded[folder])

    done: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    total = len(todo)
    origin = {id(t): t[0] for t in todo}              # chemin d'origine, même après un passage en temporaire

    def rename_one(t) -> bool:
        src, dst = t
        try:
            if key(src) == key(dst):
                # seule la casse change sur un système qui l'ignore : en deux temps
                tmp = fileops._tmp_name(os.path.dirname(src), "renommage")
                fileops._rename_new(src, tmp)
                fileops._rename_new(tmp, dst)
            else:
                fileops._rename_new(src, dst)
        except OSError as e:
            if e.errno == 17:
                msg = tr("« {0} » existe déjà").format(os.path.basename(dst))
            else:
                msg = e.strerror or str(e)
            errors.append((origin[id(t)], msg))
            if src != origin[id(t)]:                   # resté sous son nom temporaire : on le lui rend
                try:
                    fileops._rename_new(src, origin[id(t)])
                except OSError:
                    pass
            return False
        done.append((origin[id(t)], dst))
        if progress:
            progress(len(done) + len(errors), total)
        return True

    while todo:
        sources = {key(t[0]) for t in todo}
        free = [t for t in todo if key(t[1]) not in sources or key(t[1]) == key(t[0])]
        if free:
            for t in free:
                todo.remove(t)
                rename_one(t)
            continue
        # uniquement des cycles : on en sort un sous un nom temporaire, la suite se débloque
        t = todo[0]
        tmp = fileops._tmp_name(os.path.dirname(t[0]), "renommage")
        try:
            fileops._rename_new(t[0], tmp)
        except OSError as e:
            todo.remove(t)
            errors.append((origin[id(t)], e.strerror or str(e)))
            continue
        t[0] = tmp
    return done, errors


# ----------------------------------------------------------------------
# Métadonnées : EXIF des photos (sans bibliothèque), étiquettes des fichiers audio / vidéo (ffprobe)

_EXIF_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _exif_from_tiff(data: bytes) -> dict:
    out: dict = {}
    if len(data) < 8:
        return out
    end = "<" if data[:2] == b"II" else ">" if data[:2] == b"MM" else None
    if end is None:
        return out

    def ifd(offset):
        tags = {}
        if offset + 2 > len(data):
            return tags
        (n,) = struct.unpack_from(end + "H", data, offset)
        for i in range(min(n, 512)):
            pos = offset + 2 + 12 * i
            if pos + 12 > len(data):
                break
            tag, typ, count = struct.unpack_from(end + "HHI", data, pos)
            size = _EXIF_TYPES.get(typ, 1) * count
            if size <= 4:
                raw = data[pos + 8:pos + 8 + size]
            else:
                (off,) = struct.unpack_from(end + "I", data, pos + 8)
                raw = data[off:off + size]
            if typ == 2:
                tags[tag] = raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
            elif typ == 4 and len(raw) >= 4:
                tags[tag] = struct.unpack_from(end + "I", raw)[0]
        return tags

    (first,) = struct.unpack_from(end + "I", data, 4)
    ifd0 = ifd(first)
    if ifd0.get(0x0110):
        out["camera"] = ifd0[0x0110]
    when = None
    if isinstance(ifd0.get(0x8769), int):
        exif = ifd(ifd0[0x8769])
        when = exif.get(0x9003) or exif.get(0x9004)
    when = when or ifd0.get(0x0132)
    if when:
        try:
            out["created"] = datetime.strptime(when[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    return out


def read_exif(path: str) -> dict:
    """Date de prise de vue et modèle d'appareil (JPEG, TIFF et formats RAW à base TIFF)."""
    try:
        with open(path, "rb") as f:
            head = f.read(2)
            if head == b"\xff\xd8":                       # JPEG : segments jusqu'au bloc APP1 « Exif »
                while True:
                    marker = f.read(4)
                    if len(marker) < 4 or marker[0] != 0xFF or marker[1] in (0xD9, 0xDA):
                        return {}
                    size = struct.unpack(">H", marker[2:])[0] - 2
                    if marker[1] == 0xE1:
                        block = f.read(size)
                        if block[:6] == b"Exif\0\0":
                            return _exif_from_tiff(block[6:])
                    else:
                        f.seek(size, 1)
            if head in (b"II", b"MM"):
                f.seek(0)
                return _exif_from_tiff(f.read(4 * 1024 * 1024))
    except (OSError, struct.error, IndexError):
        pass
    return {}


def _probe_tags(path: str) -> dict:
    info = media.probe(path)
    tags: dict = {}
    for src in [info.get("format", {})] + list(info.get("streams", [])):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    out: dict = {}
    for key, names in (("artist", ("artist", "album_artist")), ("title", ("title",)), ("album", ("album",)),
                       ("track", ("track",)), ("season", ("season_number",)),
                       ("episode", ("episode_sort", "episode_id")), ("camera", ("com.apple.quicktime.model",))):
        for n in names:
            if str(tags.get(n, "")).strip():
                out[key] = str(tags[n]).strip()
                break
    when = tags.get("creation_time") or tags.get("date")
    if when:
        try:
            d = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            out["created"] = d.astimezone().replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            pass
    return out


def read_metadata(path: str) -> dict:
    name = os.path.basename(path)
    low = name.lower()
    if low.endswith((".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".dng", ".nef", ".cr2", ".arw", ".orf", ".rw2", ".pef")):
        return read_exif(path)
    if media.is_media(name):
        return _probe_tags(path)
    return {}
