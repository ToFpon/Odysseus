"""Miniatures au format freedesktop (~/.cache/thumbnails), partagées avec Nautilus.

On réutilise d'abord les miniatures existantes ; sinon on les fabrique : images avec GdkPixbuf,
vidéos avec ffmpegthumbnailer (ou ffmpeg), PDF avec Poppler (ou pdftoppm). Les échecs sont
mémorisés dans fail/odysseus pour ne pas recommencer à chaque affichage.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from typing import Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "thumbnails")
SIZES = {"normal": 128, "large": 256, "x-large": 512}
FAIL_DIR = os.path.join(CACHE, "fail", "odysseus-1.0")
MAX_IMAGE_BYTES = 80 * 1024 * 1024

IMAGE_TYPES = ("image/",)
VIDEO_TYPES = ("video/",)
PDF_TYPES = ("application/pdf",)


def uri_for(path: str) -> str:
    return "file://" + urllib.parse.quote(os.path.abspath(path))


def _md5(path: str) -> str:
    return hashlib.md5(uri_for(path).encode("utf-8", "surrogateescape")).hexdigest()


def size_class(pixels: int) -> str:
    if pixels <= 128:
        return "normal"
    if pixels <= 256:
        return "large"
    return "x-large"


def lookup(path: str, mtime: float, pixels: int) -> Optional[str]:
    """Miniature déjà en cache et à jour (la plus petite taille suffisante, sinon plus grande)."""
    h = _md5(path) + ".png"
    wanted = size_class(pixels)
    order = list(SIZES)
    for cls in order[order.index(wanted):] + order[:order.index(wanted)][::-1]:
        p = os.path.join(CACHE, cls, h)
        if os.path.exists(p) and _fresh(p, mtime):
            return p
    return None


def _fresh(png: str, mtime: float) -> bool:
    try:
        pb_opts = GdkPixbuf.Pixbuf.get_file_info(png)
        if pb_opts is None:
            return False
        pb = GdkPixbuf.Pixbuf.new_from_file(png)
        m = pb.get_option("tEXt::Thumb::MTime")
        return m is None or int(m) == int(mtime)
    except Exception:  # noqa: BLE001
        return False


def failed(path: str, mtime: float) -> bool:
    p = os.path.join(FAIL_DIR, _md5(path) + ".png")
    try:
        with open(p, "rb") as fh:
            return fh.read().decode("ascii", "ignore").strip() == str(int(mtime))
    except OSError:
        return False


def _mark_failed(path: str, mtime: float):
    try:
        os.makedirs(FAIL_DIR, exist_ok=True)
        with open(os.path.join(FAIL_DIR, _md5(path) + ".png"), "w") as fh:
            fh.write(str(int(mtime)))
    except OSError:
        pass


def can_thumbnail(ctype: str) -> bool:
    return ctype.startswith(IMAGE_TYPES + VIDEO_TYPES) or ctype in PDF_TYPES


def _save(pb: GdkPixbuf.Pixbuf, path: str, mtime: float, cls: str) -> str:
    folder = os.path.join(CACHE, cls)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    target = os.path.join(folder, _md5(path) + ".png")
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".png")
    os.close(fd)
    pb.savev(tmp, "png", ["tEXt::Thumb::URI", "tEXt::Thumb::MTime", "tEXt::Software"],
             [uri_for(path), str(int(mtime)), "Odysseus"])
    os.replace(tmp, target)
    return target


def _scale(pb: GdkPixbuf.Pixbuf, pixels: int) -> GdkPixbuf.Pixbuf:
    w, h = pb.get_width(), pb.get_height()
    s = min(pixels / w, pixels / h, 1.0)
    if s < 1.0:
        pb = pb.scale_simple(max(1, int(w * s)), max(1, int(h * s)), GdkPixbuf.InterpType.BILINEAR)
    return pb


def generate(path: str, ctype: str, mtime: float, pixels: int) -> Optional[str]:
    """Fabrique la miniature (appelé hors du fil principal). Renvoie le PNG ou None."""
    cls = size_class(pixels)
    target_px = SIZES[cls]
    try:
        pb = None
        if ctype.startswith(IMAGE_TYPES):
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                return None
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, target_px, target_px, True)
            pb = pb.apply_embedded_orientation() or pb
        elif ctype.startswith(VIDEO_TYPES):
            pb = _video(path, target_px)
        elif ctype in PDF_TYPES:
            pb = _pdf(path, target_px)
        if pb is None:
            _mark_failed(path, mtime)
            return None
        return _save(_scale(pb, target_px), path, mtime, cls)
    except Exception:  # noqa: BLE001
        _mark_failed(path, mtime)
        return None


def _video(path: str, px: int) -> Optional[GdkPixbuf.Pixbuf]:
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        if shutil.which("ffmpegthumbnailer"):
            subprocess.run(["ffmpegthumbnailer", "-i", path, "-o", tmp, "-s", str(px), "-q", "6"],
                           capture_output=True, timeout=20)
        elif shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "3", "-i", path, "-frames:v", "1",
                            "-vf", f"scale={px}:-1", tmp], capture_output=True, timeout=20)
        if os.path.getsize(tmp) > 0:
            return GdkPixbuf.Pixbuf.new_from_file(tmp)
    except (OSError, subprocess.SubprocessError, Exception):  # noqa: BLE001
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


# Poppler n'est pas sûr entre fils : deux rendus de PDF en parallèle (miniatures, aperçu,
# dialogues PDF) peuvent corrompre la mémoire (« malloc(): unaligned fastbin chunk »).
# Tout usage de Poppler passe par ce verrou, et les objets sont libérés avant de le rendre.
POPPLER_LOCK = threading.RLock()


def pdf_page_pixbuf(path: str, px: int, page: int = 0) -> Optional[GdkPixbuf.Pixbuf]:
    """Rendu d'une page de PDF (Poppler si présent, sinon pdftoppm)."""
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler, Gdk
        import cairo
        with POPPLER_LOCK:
            doc = Poppler.Document.new_from_file(uri_for(path), None)
            if doc.get_n_pages() <= page:
                del doc
                return None
            pg = doc.get_page(page)
            w, h = pg.get_size()
            s = px / max(w, h)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w * s)), max(1, int(h * s)))
            ctx = cairo.Context(surf)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            ctx.scale(s, s)
            pg.render(ctx)
            del ctx, pg, doc
        surf.flush()
        return Gdk.pixbuf_get_from_surface(surf, 0, 0, surf.get_width(), surf.get_height())
    except Exception:  # noqa: BLE001
        pass
    if not shutil.which("pdftoppm"):
        return None
    tmpdir = tempfile.mkdtemp()
    try:
        subprocess.run(["pdftoppm", "-png", "-f", str(page + 1), "-l", str(page + 1), "-scale-to", str(px),
                        path, os.path.join(tmpdir, "p")], capture_output=True, timeout=20)
        pngs = sorted(f for f in os.listdir(tmpdir) if f.endswith(".png"))
        if pngs:
            return GdkPixbuf.Pixbuf.new_from_file(os.path.join(tmpdir, pngs[0]))
    except (OSError, subprocess.SubprocessError, Exception):  # noqa: BLE001
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def _pdf(path: str, px: int) -> Optional[GdkPixbuf.Pixbuf]:
    return pdf_page_pixbuf(path, px, 0)
