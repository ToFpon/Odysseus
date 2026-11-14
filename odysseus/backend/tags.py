"""Étiquettes des fichiers audio (titre, artiste, album… et pochette), lues et écrites avec mutagen.

Formats : MP3, WAV et AIFF (ID3), FLAC, Ogg Vorbis, Opus (commentaires Vorbis), M4A / M4B (MP4).
Un fichier n'est jamais modifié sur place : copie cachée dans le même dossier, étiquettes écrites sur
la copie, mise sur disque, puis échange d'un coup (os.replace). Un plantage ou une coupure en plein
travail laisse l'ancien fichier intact, jamais un morceau à moitié réécrit.

Valeurs : listes de textes (un champ peut en avoir plusieurs : deux artistes). Seuls les champs donnés
à `write` changent ; les autres restent tels quels, valeurs multiples comprises.
"""

from __future__ import annotations

import base64
import re
import os
import shutil
from typing import Optional

from . import fileops

FIELDS = ("title", "artist", "album", "albumartist", "date", "genre", "tracknumber", "tracktotal",
          "discnumber", "disctotal", "comment")
SUPPORTED = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".wav", ".aif", ".aiff")
KEEP = object()                     # pochette : ne pas y toucher


def supported(name: str) -> bool:
    return name.lower().endswith(SUPPORTED)


def available() -> bool:
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return False


def _open(path: str):
    import mutagen
    f = mutagen.File(path)
    if f is None:
        raise ValueError("format inconnu")
    return f


def _kind(f) -> str:
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    if isinstance(f, MP4):
        return "mp4"
    if isinstance(f, (FLAC, OggVorbis, OggOpus)):
        return "vorbis"
    return "id3"                    # MP3, WAV, AIFF


# ----------------------------------------------------------------------
# ID3

_ID3 = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2", "date": "TDRC", "genre": "TCON"}


def _split(text: str) -> tuple[str, str]:
    """« 3/12 » → (« 3 », « 12 »)."""
    a, _sep, b = str(text).partition("/")
    return a.strip(), b.strip()


def _read_id3(f) -> dict:
    tags = f.tags or {}
    out: dict = {}
    for field, frame in _ID3.items():
        if frame in tags:
            out[field] = [str(t) for t in tags[frame].text if str(t)]
    for frame, num, tot in (("TRCK", "tracknumber", "tracktotal"), ("TPOS", "discnumber", "disctotal")):
        if frame in tags and tags[frame].text:
            a, b = _split(tags[frame].text[0])
            if a:
                out[num] = [a]
            if b:
                out[tot] = [b]
    comments = [c for c in tags.getall("COMM") if not c.desc] if hasattr(tags, "getall") else []
    if comments and comments[0].text:
        out["comment"] = [str(t) for t in comments[0].text]
    pics = tags.getall("APIC") if hasattr(tags, "getall") else []
    pics = sorted(pics, key=lambda p: p.type != 3)          # couverture d'abord
    if pics:
        out["cover"] = (bytes(pics[0].data), pics[0].mime or "image/jpeg")
    return out


def _write_id3(f, changes: dict, cover):
    from mutagen import id3
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    for field, frame in _ID3.items():
        if field in changes:
            tags.delall(frame)
            if changes[field]:
                tags.add(getattr(id3, frame)(encoding=3, text=list(changes[field])))
    current = _read_id3(f)
    for frame, num, tot in (("TRCK", "tracknumber", "tracktotal"), ("TPOS", "discnumber", "disctotal")):
        if num in changes or tot in changes:
            n = (changes.get(num, current.get(num)) or [""])[0]
            t = (changes.get(tot, current.get(tot)) or [""])[0]
            tags.delall(frame)
            if n or t:
                tags.add(getattr(id3, frame)(encoding=3, text=[f"{n}/{t}" if t else n]))
    if "comment" in changes:
        for key in [k for k in tags.keys() if k.startswith("COMM:") and not tags[k].desc]:
            del tags[key]                    # commentaire principal (sans description), toutes langues
        if changes["comment"]:
            tags.add(id3.COMM(encoding=3, lang="eng", desc="", text=list(changes["comment"])))
    if cover is not KEEP:
        tags.delall("APIC")
        if cover:
            tags.add(id3.APIC(encoding=3, mime=cover[1], type=3, desc="", data=cover[0]))


# ----------------------------------------------------------------------
# Commentaires Vorbis (FLAC, Ogg, Opus)

_VORBIS = {"title": ("title",), "artist": ("artist",), "album": ("album",), "albumartist": ("albumartist",),
           "date": ("date", "year"), "genre": ("genre",), "tracknumber": ("tracknumber",),
           "tracktotal": ("tracktotal", "totaltracks"), "discnumber": ("discnumber",),
           "disctotal": ("disctotal", "totaldiscs"), "comment": ("comment", "description")}


def _read_vorbis(f) -> dict:
    tags = f.tags or {}
    out: dict = {}
    for field, keys in _VORBIS.items():
        for k in keys:
            vals = [v for v in (tags.get(k) or []) if v]
            if vals:
                if field in ("tracknumber", "discnumber") and "/" in vals[0]:
                    a, b = _split(vals[0])                     # « 3/12 » dans TRACKNUMBER
                    out[field] = [a]
                    tot = "tracktotal" if field == "tracknumber" else "disctotal"
                    if b and tot not in out:
                        out[tot] = [b]
                else:
                    out[field] = vals
                break
    from mutagen.flac import FLAC, Picture
    pics = list(f.pictures) if isinstance(f, FLAC) else []
    if not pics:
        for raw in tags.get("metadata_block_picture") or []:
            try:
                pics.append(Picture(base64.b64decode(raw)))
            except Exception:  # noqa: BLE001 — image illisible : ignorée
                pass
    pics.sort(key=lambda p: p.type != 3)
    if pics:
        out["cover"] = (bytes(pics[0].data), pics[0].mime or "image/jpeg")
    return out


def _write_vorbis(f, changes: dict, cover):
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    current = _read_vorbis(f)
    changes = dict(changes)
    for num, tot in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
        # « 3/12 » dans TRACKNUMBER : réécrit en deux champs dès que l'un des deux change
        if num in changes or tot in changes:
            changes.setdefault(num, current.get(num, []))
            changes.setdefault(tot, current.get(tot, []))
    for field, vals in changes.items():
        keys = _VORBIS.get(field)
        if not keys:
            continue
        for k in keys:
            if k in tags:
                del tags[k]
        if vals:
            tags[keys[0]] = list(vals)
    if cover is not KEEP:
        from mutagen.flac import FLAC, Picture
        if isinstance(f, FLAC):
            f.clear_pictures()
        if "metadata_block_picture" in tags:
            del tags["metadata_block_picture"]
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, cover[1], cover[0]
            if isinstance(f, FLAC):
                f.add_picture(pic)
            else:
                tags["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]


# ----------------------------------------------------------------------
# MP4 (M4A, M4B)

_MP4 = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART", "date": "\xa9day",
        "genre": "\xa9gen", "comment": "\xa9cmt"}


def _read_mp4(f) -> dict:
    tags = f.tags or {}
    out: dict = {}
    for field, key in _MP4.items():
        vals = [str(v) for v in (tags.get(key) or []) if str(v)]
        if vals:
            out[field] = vals
    for key, num, tot in (("trkn", "tracknumber", "tracktotal"), ("disk", "discnumber", "disctotal")):
        if tags.get(key):
            n, t = tags[key][0]
            if n:
                out[num] = [str(n)]
            if t:
                out[tot] = [str(t)]
    if tags.get("covr"):
        from mutagen.mp4 import MP4Cover
        c = tags["covr"][0]
        out["cover"] = (bytes(c), "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg")
    return out


def _int(vals) -> int:
    try:
        return int(str((vals or ["0"])[0]).strip() or 0)
    except ValueError:
        return 0


def _write_mp4(f, changes: dict, cover):
    from mutagen.mp4 import MP4Cover
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    for field, key in _MP4.items():
        if field in changes:
            if key in tags:
                del tags[key]
            if changes[field]:
                tags[key] = list(changes[field])
    current = _read_mp4(f)
    for key, num, tot in (("trkn", "tracknumber", "tracktotal"), ("disk", "discnumber", "disctotal")):
        if num in changes or tot in changes:
            n = _int(changes.get(num, current.get(num)))
            t = _int(changes.get(tot, current.get(tot)))
            if key in tags:
                del tags[key]
            if n or t:
                tags[key] = [(n, t)]
    if cover is not KEEP:
        if "covr" in tags:
            del tags["covr"]
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover[1] == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(cover[0], imageformat=fmt)]


# ----------------------------------------------------------------------

def read(path: str) -> dict:
    """{champ: [valeurs]} + « cover » : (octets, type MIME) s'il y a une pochette. Lève en cas d'échec."""
    f = _open(path)
    kind = _kind(f)
    return _read_mp4(f) if kind == "mp4" else _read_vorbis(f) if kind == "vorbis" else _read_id3(f)


def snapshot(path: str) -> dict:
    """Tout ce que `write` peut changer, pour l'annulation : chaque champ (liste vide s'il manque) et la
    pochette (None s'il n'y en a pas)."""
    cur = read(path)
    snap = {k: list(cur.get(k, [])) for k in FIELDS}
    snap["cover"] = cur.get("cover")
    return snap


def write(path: str, changes: dict, cover=KEEP):
    """Écrit les champs de `changes` ({champ: liste de valeurs, vide = effacer}) et la pochette
    ((octets, type MIME), None = la retirer, KEEP = n'y pas toucher), sur une copie cachée échangée d'un
    coup avec l'original. Lève OSError / ValueError / erreurs de mutagen ; l'original reste alors intact."""
    changes = {k: [str(v) for v in vals if str(v).strip()] for k, vals in changes.items() if k in FIELDS}
    if not changes and cover is KEEP:
        return
    folder = os.path.dirname(os.path.abspath(path))
    tmp = fileops._tmp_name(folder, "tags")
    try:
        shutil.copy2(path, tmp)
        f = _open(tmp)
        kind = _kind(f)
        (_write_mp4 if kind == "mp4" else _write_vorbis if kind == "vorbis" else _write_id3)(f, changes, cover)
        if kind == "id3":
            f.save(v2_version=3)            # ID3v2.3 : lu par tous les lecteurs (voitures, Windows…)
        else:
            f.save()
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


def merged(values: list[dict], field: str) -> Optional[str]:
    """Valeur commune d'un champ pour plusieurs fichiers (« ; » entre plusieurs valeurs d'un même fichier),
    ou None si elle diffère d'un fichier à l'autre."""
    texts = {"; ".join(v.get(field, [])) for v in values}
    return texts.pop() if len(texts) == 1 else None


def image_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


_LEADING = re.compile(r"^\s*(?:(?:cd|disc|disque)\s*\d+\s*[-_. ]\s*)?(\d{1,3})(?:\s*[-_.)\]]\s*|\s+)(.+)$", re.I)


def from_filename(name: str) -> dict:
    """Titre et numéro de piste d'après le nom du fichier : « 03 - Monospace.mp3 », « 03. Monospace »,
    « 03 Monospace », « Bénabar - 03 - Monospace » → piste 3, titre « Monospace ». Sans numéro : le titre
    seul (le nom sans extension, ou ce qui suit le dernier « - »)."""
    stem = os.path.splitext(os.path.basename(name))[0].replace("_", " ").strip()
    out: dict = {}
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    for i, part in enumerate(parts):                # « Artiste - 03 - Titre » : le numéro peut être au milieu
        if part.isdigit() and len(part) <= 3 and i < len(parts) - 1:
            out["tracknumber"] = [str(int(part))]
            out["title"] = [" - ".join(parts[i + 1:])]
            return out
    m = _LEADING.match(stem)
    if m:
        out["tracknumber"] = [str(int(m.group(1)))]
        stem = m.group(2).strip()
        parts = [p.strip() for p in stem.split(" - ") if p.strip()] or [stem]
    out["title"] = [parts[-1] if len(parts) > 1 else stem]
    return out
