"""Listes de lecture M3U : en créer une à partir de morceaux ou de dossiers, et y ajouter d'autres
morceaux, d'où qu'ils viennent (un medley pris dans plusieurs dossiers).

- Chemins RELATIFS au dossier de la liste : copiée avec la musique (clé, téléphone, autre PC), elle
  marche toujours.
- M3U étendu (#EXTM3U, puis #EXTINF:durée,Artiste - Titre lus par ffprobe s'il est là) : les lecteurs
  affichent de vrais titres. Sans ffprobe : #EXTINF:-1,nom du fichier.
- Écrite en UTF-8, comme VLC (.m3u lu partout ; .m3u8 aussi accepté).
- Jamais d'écrasement : nouvelle liste posée par fileops.place_new (« nom (2) ») ; ajout = copie
  complète sous un nom caché, mise sur disque, puis échange (l'ancienne reste si ça échoue).
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from ..i18n import tr
from . import media, scanner
from .engine import Job, ToolError

AUDIO_EXTS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".opus", ".aac", ".wma",
                        ".aiff", ".aif", ".alac", ".ape", ".wv", ".mka", ".mpc", ".m4b"})
PLAYLIST_EXTS = (".m3u", ".m3u8")


def is_audio(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


def is_playlist(name: str) -> bool:
    return name.lower().endswith(PLAYLIST_EXTS)


def _natural(names: list[str]) -> list[str]:
    return sorted(names, key=scanner.collate_key)


def collect(paths: list[str], job: Optional[Job] = None) -> list[str]:
    """Morceaux de la sélection, dans l'ordre donné ; un dossier apporte ses morceaux dans l'ordre
    naturel des noms (01, 02… 10), sous-dossiers compris (après les fichiers, comme dans un album
    en plusieurs disques). Fichiers et dossiers cachés ignorés, liens de dossiers non suivis (boucles)."""
    out: list[str] = []
    seen: set = set()

    def add(p: str):
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            out.append(p)

    def walk(folder: str):
        if job is not None:
            job.check_cancel()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        files = [e.name for e in entries if not e.name.startswith(".") and e.is_file() and is_audio(e.name)]
        dirs = [e.name for e in entries if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)]
        for n in _natural(files):
            add(os.path.join(folder, n))
        for n in _natural(dirs):
            walk(os.path.join(folder, n))

    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            walk(p)
        elif os.path.isfile(p) and is_audio(p):
            add(p)
    return out


def _entry(track: str, folder: str) -> str:
    """Chemin écrit dans la liste : relatif à son dossier, séparateurs « / »."""
    return os.path.relpath(track, folder)


def _extinf(track: str) -> str:
    """#EXTINF:secondes,Artiste - Titre (ffprobe) ; sinon -1 et le nom du fichier."""
    stem = os.path.splitext(os.path.basename(track))[0]
    if not shutil.which("ffprobe"):
        return f"#EXTINF:-1,{stem}"
    info = media.probe(track)
    tags: dict = {}
    for src in [info.get("format", {})] + list(info.get("streams", [])):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), str(v).strip())
    try:
        seconds = int(round(float(info.get("format", {}).get("duration", -1))))
    except (TypeError, ValueError):
        seconds = -1
    artist = tags.get("artist") or tags.get("album_artist") or ""
    title = tags.get("title") or ""
    label = f"{artist} - {title}" if artist and title else (title or stem)
    return f"#EXTINF:{seconds},{' '.join(label.split())}"     # jamais de retour à la ligne dans le titre


def _lines(job: Job, tracks: list[str], folder: str, start: float = 0.0) -> list[str]:
    out = []
    n = max(len(tracks), 1)
    for i, t in enumerate(tracks):
        job.check_cancel()
        job.progress(start + (1 - start) * i / n, os.path.basename(t))
        out += [_extinf(t), _entry(t, folder)]
    return out


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def create(job: Job, tracks: list[str], dest: str) -> str:
    """Nouvelle liste dest (chemin complet, .m3u ou .m3u8) ; renvoie le chemin réellement posé
    (« nom (2).m3u » si le nom est pris)."""
    from .fileops import _tmp_name, place_new
    if not tracks:
        raise ToolError(tr("Aucun morceau de musique dans la sélection"))
    folder = os.path.dirname(dest)
    body = "#EXTM3U\n" + "\n".join(_lines(job, tracks, folder)) + "\n"
    tmp = _tmp_name(folder, "liste")
    try:
        _write(tmp, body)
        final = place_new(tmp, dest)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    job.progress(1.0, os.path.basename(final))
    return final


def _resolve(line: str, folder: str) -> Optional[str]:
    """Morceau désigné par une ligne de la liste (chemin relatif, absolu ou file://)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("file://"):
        from gi.repository import Gio
        p = Gio.File.new_for_uri(line).get_path()
        return os.path.realpath(p) if p else None
    if "://" in line:
        return line                                  # flux réseau : gardé tel quel
    return os.path.realpath(os.path.join(folder, line))


def append(job: Job, playlist: str, tracks: list[str]) -> tuple[int, int]:
    """Ajoute à la fin de playlist les morceaux qui n'y sont pas déjà ; renvoie (ajoutés, déjà là).
    Le contenu existant est gardé tel quel (commentaires, flux réseau compris)."""
    from .fileops import _fsync_dir, _tmp_name
    folder = os.path.dirname(playlist)
    with open(playlist, encoding="utf-8", errors="surrogateescape") as fh:
        old = fh.read()
    present = {r for r in (_resolve(line, folder) for line in old.splitlines()) if r}
    new = [t for t in tracks if os.path.realpath(t) not in present]
    if not new:
        return 0, len(tracks)
    extended = old.lstrip("﻿").startswith("#EXTM3U")
    lines = _lines(job, new, folder) if extended else [_entry(t, folder) for t in new]
    text = old + ("" if not old or old.endswith("\n") else "\n") + "\n".join(lines) + "\n"
    if not old.strip():
        text = "#EXTM3U\n" + "\n".join(_lines(job, new, folder)) + "\n"
    tmp = _tmp_name(folder, "liste")
    try:
        _write(tmp, text)
        try:
            shutil.copymode(playlist, tmp)
        except OSError:
            pass
        os.replace(tmp, playlist)                    # remplacement voulu : la liste complétée
        _fsync_dir(folder)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    job.progress(1.0, os.path.basename(playlist))
    return len(new), len(tracks) - len(new)
