"""Outils image (reprise de nautilus-image-converter, plus la conversion de format) : redimensionner,
pivoter, convertir, en une seule passe d'ImageMagick par image.

Jamais d'image à moitié écrite : la sortie est faite sous un nom caché dans le dossier de destination
puis posée d'un coup (fileops.place_new, « nom (2) » si le nom est pris). « Remplacer l'original » :
même format → échange atomique (os.replace, après fsync) ; autre format → la nouvelle image est posée,
mise sur disque, puis l'original part à la corbeille (récupérable)."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .. import features
from ..i18n import tr
from . import fileops
from .engine import Job, ToolError

# formats écrits (ImageMagick 6 d'Ubuntu 24.04 : HEIC / AVIF en lecture seule)
OUTPUT_FORMATS = [("jpg", "JPEG"), ("png", "PNG"), ("webp", "WebP"), ("tiff", "TIFF"), ("bmp", "BMP"),
                  ("gif", "GIF")]
CODER = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "tiff": "TIFF", "bmp": "BMP", "gif": "GIF"}
LOSSY = {"jpg", "webp"}
# extensions lues
READABLE = {"jpg": "jpg", "jpeg": "jpg", "jpe": "jpg", "png": "png", "webp": "webp", "tif": "tiff",
            "tiff": "tiff", "bmp": "bmp", "gif": "gif", "heic": "", "heif": "", "avif": "", "jfif": "jpg"}
# sans canal alpha : le fond découvert par une rotation libre est blanc
OPAQUE = {"jpg", "bmp"}


def tool() -> str:
    return shutil.which("magick") or shutil.which("convert") or ""


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1][1:].lower()


def is_image(name: str) -> bool:
    return ext_of(name) in READABLE


def source_format(name: str) -> str:
    """Format d'écriture « naturel » d'une image source (« » pour HEIC / AVIF, non écrits)."""
    return READABLE.get(ext_of(name), "")


@dataclass
class Options:
    resize: str = ""            # "" (non), "scale", "fit"
    scale: int = 50             # %
    width: int = 1920
    height: int = 1080
    no_enlarge: bool = True
    rotate: int = 0             # degrés, sens horaire (0 : non)
    fmt: str = ""               # format de sortie ("" : celui de la source)
    quality: int = 90           # JPEG / WebP
    replace: bool = False       # remplacer l'original
    suffix: str = ""            # ajouté au nom (nouvelle image)
    dest_dir: str = ""          # "" : à côté de chaque image


def output_format(src: str, o: Options) -> str:
    return o.fmt or source_format(src) or "jpg"


def output_name(src: str, o: Options) -> str:
    stem = os.path.splitext(os.path.basename(src))[0]
    fmt = output_format(src, o)
    ext = os.path.splitext(src)[1] if fmt == source_format(src) else "." + fmt
    return stem + ("" if o.replace else o.suffix) + ext


def command(src: str, out: str, o: Options) -> list[str]:
    """Ligne d'ImageMagick. L'entrée prend un sélecteur d'images explicite (« [0] ») : sinon des
    crochets dans le nom (« photo [1].jpg ») seraient lus comme tels. Le chemin est absolu : un
    « x:y.png » relatif serait lu comme le format « x »."""
    fmt = output_format(src, o)
    animated = fmt == "gif" and ext_of(src) == "gif"
    cmd = [tool(), os.path.abspath(src) + ("[0-99999]" if animated else "[0]")]
    if animated:
        cmd.append("-coalesce")
    cmd.append("-auto-orient")               # photos de téléphone : pixels remis droits d'abord
    if o.rotate % 360:
        cmd += ["-background", "white" if fmt in OPAQUE else "none", "-rotate", str(o.rotate % 360), "+repage"]
    if o.resize == "scale" and o.scale != 100:
        cmd += ["-resize", f"{max(1, o.scale)}%"]
    elif o.resize == "fit":
        cmd += ["-resize", f"{max(1, o.width)}x{max(1, o.height)}" + (">" if o.no_enlarge else "")]
    if fmt in OPAQUE:
        cmd += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
    if fmt in LOSSY:
        cmd += ["-quality", str(max(1, min(100, o.quality)))]
    if animated:
        cmd += ["-layers", "optimize"]
    cmd.append(f"{CODER[fmt]}:{out}")
    return cmd


def _fsync(path: str):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def process(job: Job, sources: list[str], o: Options, on_file=None) -> fileops.Result:
    """Traite chaque image. on_file(index, ok) signale chaque fin d'image."""
    if not tool():
        raise ToolError(tr("ImageMagick n'est pas installé"))
    res = fileops.Result("image")
    total = len(sources)
    for i, src in enumerate(sources):
        job.check_cancel()
        job.progress(i / total, tr("{0} ({1} sur {2})").format(os.path.basename(src), i + 1, total))
        if o.replace:
            src = os.path.realpath(src)              # lien symbolique : c'est l'image visée qu'on remplace
        folder = o.dest_dir if (o.dest_dir and not o.replace) else os.path.dirname(src)
        name = output_name(src, o)
        dst = os.path.join(folder, name)
        tmp = fileops._tmp_name(folder, "image")
        ok = False
        try:
            p = job.popen(command(src, tmp, o), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            _out, err = p.communicate()
            if job.cancelled:
                raise OSError(0, tr("Annulé"))
            if p.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                msg = (err or "").strip().splitlines()
                text = msg[-1] if msg else tr("ImageMagick a échoué")
                if ext_of(src) in ("heic", "heif") and not features.present("lib:heif-libde265"):
                    # Ubuntu 24.04 : le décodeur HEIC n'est que « suggéré » par libheif
                    text = tr("Décodeur HEIC absent. Installez-le avec :\n{0}").format(
                        "sudo apt install libheif-plugin-libde265")
                raise OSError(0, text)
            _fsync(tmp)
            if o.replace and os.path.abspath(dst) == os.path.abspath(src):
                try:
                    os.chmod(tmp, os.stat(src).st_mode & 0o7777)
                except OSError:
                    pass
                os.replace(tmp, src)                     # remplacement voulu, d'un coup
                fileops._fsync_dir(folder)
                res.created.append(src)
            else:
                final = fileops.place_new(tmp, dst)
                res.created.append(final)
                if o.replace:
                    # autre format : l'original ne part qu'une fois la nouvelle image sur le disque
                    fileops._fsync_dir(folder)
                    fileops.flush_to_disk(folder)
                    Gio.File.new_for_path(src).trash(None)
                    res.trashed.append(src)
                else:
                    res.new_items.append(final)
            ok = True
        except (OSError, GLib.Error) as e:
            if os.path.lexists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if job.cancelled:
                if on_file:
                    on_file(i, False)
                job.check_cancel()
            res.errors.append((src, getattr(e, "strerror", None) or getattr(e, "message", None) or str(e)))
        if on_file:
            on_file(i, ok)
    job.progress(1.0)
    if sources and not res.created:
        raise ToolError(tr("Aucune image n'a pu être traitée"),
                        "\n".join(f"{os.path.basename(p)} : {m}" for p, m in res.errors[:10]))
    return res
