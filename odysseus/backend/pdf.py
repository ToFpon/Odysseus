"""Outils PDF : compresser, fusionner, filigraner, travailler les pages.

Améliorations par rapport aux extensions d'origine :
- compression Ghostscript avec vraie progression (page par page) et comparaison avant / après ;
  si le résultat est plus gros que l'original, on le signale au lieu de l'imposer ;
- fusion avec choix des pages de chaque fichier (qpdf, ou pypdf, ou Ghostscript en secours) ;
- filigrane vectoriel dessiné avec Cairo (texte net, vraie transparence, image possible), une page de
  filigrane par page du document (portrait et paysage mélangés), posé avec qpdf ; plus besoin
  d'ImageMagick ni de PDF écrit à la main ;
- aplatissement de sécurité par Ghostscript (pages converties en images), résolution réglable ;
- nouvel outil Pages : extraire, supprimer, faire pivoter, éclater en fichiers séparés.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from ..i18n import tr
from .engine import Job, ToolError

GS_LEVELS = [("/screen", tr("Écran (72 ppp, le plus léger)")), ("/ebook", tr("Livre numérique (150 ppp)")),
             ("/printer", tr("Impression (300 ppp)")), ("/prepress", tr("Prépresse (couleurs préservées)")),
             ("/default", tr("Par défaut"))]


def gs() -> Optional[str]:
    return shutil.which("gs") or shutil.which("ghostscript")


def qpdf() -> Optional[str]:
    return shutil.which("qpdf")


def has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(1024).lstrip().startswith(b"%PDF")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_sizes(path: str) -> list[tuple[float, float]]:
    """Taille (points) de chaque page, rotation comprise."""
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
        from .thumbnails import uri_for
        doc = Poppler.Document.new_from_file(uri_for(path), None)
        return [doc.get_page(i).get_size() for i in range(doc.get_n_pages())]
    except Exception:  # noqa: BLE001
        pass
    if has_pypdf():
        from pypdf import PdfReader
        out = []
        for p in PdfReader(path).pages:
            w, h = float(p.mediabox.width), float(p.mediabox.height)
            if (p.get("/Rotate") or 0) % 180:
                w, h = h, w
            out.append((w, h))
        return out
    n = page_count(path)
    return [(595.0, 842.0)] * n


def page_count(path: str) -> int:
    if qpdf():
        r = subprocess.run([qpdf(), "--show-npages", path], capture_output=True, text=True)
        if r.returncode in (0, 3) and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
        from .thumbnails import uri_for
        return Poppler.Document.new_from_file(uri_for(path), None).get_n_pages()
    except Exception:  # noqa: BLE001
        pass
    if has_pypdf():
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    return 0


def parse_ranges(text: str, n: int) -> list[int]:
    """« 1-3, 5, 8- , z » → indices (base 0). Vide = toutes les pages. Lève ValueError."""
    text = (text or "").strip().lower()
    if not text or text in ("*", tr("tout").lower(), "all"):
        return list(range(n))
    out = []

    def num(s: str) -> int:
        s = s.strip()
        if s in ("z", "fin", "end", "last"):
            return n
        if s.startswith("r") and s[1:].isdigit():      # r1 = dernière page (syntaxe qpdf)
            return n - int(s[1:]) + 1
        v = int(s)
        if v < 1 or v > n:
            raise ValueError(tr("Page {0} hors du document ({1} pages)").format(v, n))
        return v
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = num(a) if a.strip() else 1
            end = num(b) if b.strip() else n
            step = 1 if end >= start else -1
            out.extend(range(start - 1, end - 1 + step, step))
        else:
            out.append(num(part) - 1)
    return out


def to_qpdf_ranges(indices: list[int]) -> str:
    """Indices (base 0) → « 1-3,5,7 » compacté, ordre conservé."""
    if not indices:
        return ""
    parts = []
    start = prev = indices[0]
    for i in indices[1:] + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        parts.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
        if i is not None:
            start = prev = i
    return ",".join(parts)


def _tmp_near(dst: str, suffix: str = ".pdf") -> str:
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix=".odysseus-", dir=os.path.dirname(os.path.abspath(dst)))
    os.close(fd)
    return tmp


def _finish(tmp: str, dst: str):
    os.replace(tmp, dst)


def _run(job: Job, argv: list[str], label: str = "", total_pages: int = 0, base=(0.0, 1.0)) -> str:
    """Lance un outil ; suit « Page N » de Ghostscript si total_pages est donné."""
    p = job.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    out = []
    start, span = base
    for line in p.stdout:
        line = line.rstrip()
        m = re.match(r"^Page (\d+)$", line)
        if m and total_pages:
            k = int(m.group(1))
            job.progress(start + span * min(k / total_pages, 1.0),
                         (label + " — " if label else "") + tr("page {0} sur {1}").format(k, total_pages))
        elif line:
            out.append(line)
    code = p.wait()
    job.check_cancel()
    text = "\n".join(out)
    if code not in (0, 3):      # 3 = avertissements de qpdf
        lines = [l for l in out if l.strip()]
        raise ToolError(lines[-1] if lines else tr("code {0}").format(code), text, code)
    if text:
        job.log.append(text)
    return text


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

@dataclass
class CompressResult:
    src: str
    dst: str
    before: int
    after: int
    kept_original: bool = False


def compress(job: Job, src: str, dst: str, level: str = "/ebook", grayscale: bool = False,
             keep_if_bigger: bool = True, base=(0.0, 1.0)) -> CompressResult:
    exe = gs()
    if not exe:
        raise ToolError(tr("Ghostscript est introuvable (sudo apt install ghostscript)"))
    if not is_pdf(src):
        raise ToolError(tr("« {0} » n'est pas un PDF valide").format(os.path.basename(src)))
    before = os.path.getsize(src)
    n = page_count(src)
    tmp = _tmp_near(dst)
    argv = [exe, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5", f"-dPDFSETTINGS={level}",
            "-dNOPAUSE", "-dBATCH", "-dDetectDuplicateImages=true", "-dCompressFonts=true",
            f"-sOutputFile={tmp}"]
    if grayscale:
        argv += ["-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"]
    argv.append(src)
    try:
        _run(job, argv, os.path.basename(src), n, base)
        after = os.path.getsize(tmp)
        if keep_if_bigger and after >= before:
            os.remove(tmp)
            if os.path.abspath(dst) != os.path.abspath(src):
                shutil.copy2(src, dst)
            return CompressResult(src, dst, before, before, kept_original=True)
        _finish(tmp, dst)
        return CompressResult(src, dst, before, after)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def compress_many(job: Job, items: list[tuple[str, str]], level: str, grayscale: bool) -> list[CompressResult]:
    out = []
    total = max(len(items), 1)
    for i, (src, dst) in enumerate(items):
        job.check_cancel()
        out.append(compress(job, src, dst, level, grayscale, base=(i / total, 1 / total)))
    return out


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def merge(job: Job, items: list[tuple[str, str]], dst: str) -> str:
    """items : (fichier, pages) — pages au format « 1-3, 5 », vide = tout le document."""
    if not items:
        raise ToolError(tr("Aucun fichier à fusionner"))
    selections = []
    for path, ranges in items:
        n = page_count(path)
        try:
            idx = parse_ranges(ranges, n)
        except ValueError as e:
            raise ToolError(f"{os.path.basename(path)} : {e}")
        if not idx:
            raise ToolError(tr("{0} : aucune page sélectionnée").format(os.path.basename(path)))
        selections.append((path, idx, n))
    tmp = _tmp_near(dst)
    try:
        if qpdf():
            argv = [qpdf(), "--empty", "--pages"]
            for path, idx, n in selections:
                argv += [path, to_qpdf_ranges(idx)]
            argv += ["--", tmp]
            job.progress(0.1, tr("Fusion de {0} fichiers…").format(len(items)))
            _run(job, argv)
        elif has_pypdf():
            from pypdf import PdfReader, PdfWriter
            w = PdfWriter()
            for k, (path, idx, n) in enumerate(selections):
                job.check_cancel()
                job.progress(k / len(selections), os.path.basename(path))
                r = PdfReader(path)
                for i in idx:
                    w.add_page(r.pages[i])
            with open(tmp, "wb") as fh:
                w.write(fh)
        elif gs():
            if any(idx != list(range(n)) for _p, idx, n in selections):
                raise ToolError(tr("Le choix des pages demande qpdf ou pypdf (sudo apt install qpdf)"))
            total = sum(n for _p, _i, n in selections)
            _run(job, [gs(), "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dPDFSETTINGS=/default",
                       f"-sOutputFile={tmp}"] + [p for p, _i, _n in selections], tr("Fusion"), total)
        else:
            raise ToolError(tr("Installez qpdf pour fusionner des PDF (sudo apt install qpdf)"))
        _finish(tmp, dst)
        return dst
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------------------
# Filigrane
# ---------------------------------------------------------------------------

POSITIONS = ["center", "tiled", "top-left", "top-right", "bottom-left", "bottom-right", "top", "bottom"]


@dataclass
class WatermarkOptions:
    text: str = ""
    image: str = ""                 # chemin d'une image (PNG, JPEG, SVG…) à la place du texte
    font: str = "Sans Bold"
    size: float = 60.0              # points (texte) ; pour une image : % de la largeur de page
    color: tuple = (0.8, 0.0, 0.0)
    opacity: float = 0.3
    angle: float = 45.0
    position: str = "center"
    pages: str = ""                 # vide = toutes
    flatten: bool = False
    dpi: int = 150
    margin: float = 36.0


def _draw_text(ctx, o: WatermarkOptions, w: float, h: float):
    import cairo
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        layout = PangoCairo.create_layout(ctx)
        desc = Pango.FontDescription.from_string(o.font)
        desc.set_absolute_size(o.size * Pango.SCALE)
        layout.set_font_description(desc)
        layout.set_text(o.text, -1)
        ink, logical = layout.get_pixel_extents()
        tw, th = logical.width, logical.height

        def show():
            PangoCairo.show_layout(ctx, layout)
    except Exception:  # noqa: BLE001 — repli sur l'API texte simple de Cairo
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(o.size)
        ext = ctx.text_extents(o.text)
        tw, th = ext.width, o.size

        def show():
            ctx.move_to(0, o.size * 0.8)
            ctx.show_text(o.text)
    r, g, b = o.color
    ctx.set_source_rgba(r, g, b, o.opacity)

    def stamp(cx, cy):
        ctx.save()
        ctx.translate(cx, cy)
        ctx.rotate(-math.radians(o.angle))
        ctx.translate(-tw / 2, -th / 2)
        show()
        ctx.restore()
    _place(o, w, h, tw, th, stamp)


def gi_require():
    import gi
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")


def _draw_image(ctx, o: WatermarkOptions, w: float, h: float, cache: dict):
    import cairo
    gi_require()
    from gi.repository import Gdk, GdkPixbuf
    pb = cache.get("pb")
    if pb is None:
        pb = cache["pb"] = GdkPixbuf.Pixbuf.new_from_file(o.image)
    iw, ih = pb.get_width(), pb.get_height()
    target_w = w * max(1.0, min(100.0, o.size)) / 100.0
    s = target_w / iw
    tw, th = iw * s, ih * s

    def stamp(cx, cy):
        ctx.save()
        ctx.translate(cx, cy)
        ctx.rotate(-math.radians(o.angle))
        ctx.translate(-tw / 2, -th / 2)
        ctx.scale(s, s)
        Gdk.cairo_set_source_pixbuf(ctx, pb, 0, 0)
        ctx.paint_with_alpha(o.opacity)
        ctx.restore()
    _place(o, w, h, tw, th, stamp)
    _ = cairo


def _place(o: WatermarkOptions, w, h, tw, th, stamp):
    rad = math.radians(o.angle)
    bw = abs(tw * math.cos(rad)) + abs(th * math.sin(rad))     # boîte englobante après rotation
    bh = abs(tw * math.sin(rad)) + abs(th * math.cos(rad))
    m = o.margin
    pos = o.position
    if pos == "tiled":
        step_x = max(bw * 1.25, 60)
        step_y = max(bh * 1.6, 60)
        row = 0
        y = -bh / 2
        while y < h + bh:
            x = (step_x / 2 if row % 2 else 0) - bw / 2
            while x < w + bw:
                stamp(x, y)
                x += step_x
            y += step_y
            row += 1
        return
    cx = {"top-left": m + bw / 2, "bottom-left": m + bw / 2, "top-right": w - m - bw / 2,
          "bottom-right": w - m - bw / 2}.get(pos, w / 2)
    # repère PDF de Cairo : origine en haut à gauche
    cy = {"top-left": m + bh / 2, "top-right": m + bh / 2, "top": m + bh / 2,
          "bottom-left": h - m - bh / 2, "bottom-right": h - m - bh / 2, "bottom": h - m - bh / 2}.get(pos, h / 2)
    stamp(cx, cy)


def build_watermark_pdf(path: str, sizes: list[tuple[float, float]], selected: set, o: WatermarkOptions):
    """Une page de filigrane par page du document ; pages non choisies laissées vides."""
    import cairo
    if not sizes:
        raise ToolError(tr("Document vide"))
    surf = cairo.PDFSurface(path, sizes[0][0], sizes[0][1])
    ctx = cairo.Context(surf)
    cache: dict = {}
    for i, (w, h) in enumerate(sizes):
        surf.set_size(w, h)
        if i in selected:
            if o.image:
                _draw_image(ctx, o, w, h, cache)
            else:
                _draw_text(ctx, o, w, h)
        ctx.show_page()
    surf.finish()


def watermark(job: Job, src: str, dst: str, o: WatermarkOptions) -> str:
    if not o.text.strip() and not o.image:
        raise ToolError(tr("Indiquez un texte ou une image"))
    sizes = page_sizes(src)
    n = len(sizes)
    try:
        selected = set(parse_ranges(o.pages, n))
    except ValueError as e:
        raise ToolError(str(e))
    job.progress(0.05, tr("Dessin du filigrane…"))
    wm = _tmp_near(dst)
    tmp = _tmp_near(dst)
    flat = _tmp_near(dst) if o.flatten else None
    try:
        build_watermark_pdf(wm, sizes, selected, o)
        job.check_cancel()
        job.progress(0.3, tr("Application sur {0} pages…").format(n))
        if qpdf():
            _run(job, [qpdf(), src, "--overlay", wm, "--", tmp])
        elif has_pypdf():
            from pypdf import PdfReader, PdfWriter
            r, wr = PdfReader(src), PdfReader(wm)
            w = PdfWriter()
            for i, page in enumerate(r.pages):
                job.check_cancel()
                if i in selected:
                    page.merge_page(wr.pages[i])
                w.add_page(page)
            with open(tmp, "wb") as fh:
                w.write(fh)
        else:
            raise ToolError(tr("Installez qpdf pour appliquer un filigrane (sudo apt install qpdf)"))
        if o.flatten:
            exe = gs()
            if not exe:
                raise ToolError(tr("L'aplatissement demande Ghostscript (sudo apt install ghostscript)"))
            _run(job, [exe, "-sDEVICE=pdfimage24", f"-r{int(o.dpi)}", "-sCompression=JPEG", "-dJPEGQ=90",
                       "-dNOPAUSE", "-dBATCH", f"-sOutputFile={flat}", tmp], tr("Aplatissement"), n, (0.5, 0.5))
            _finish(flat, dst)
            os.remove(tmp)
        else:
            _finish(tmp, dst)
        return dst
    finally:
        for f in (wm, tmp, flat):
            if f and os.path.exists(f):
                os.remove(f)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def extract_pages(job: Job, src: str, ranges: str, dst: str) -> str:
    n = page_count(src)
    idx = parse_ranges(ranges, n)
    if not idx:
        raise ToolError(tr("Aucune page sélectionnée"))
    return _select(job, src, idx, dst)


def delete_pages(job: Job, src: str, ranges: str, dst: str) -> str:
    n = page_count(src)
    drop = set(parse_ranges(ranges, n))
    keep = [i for i in range(n) if i not in drop]
    if not keep:
        raise ToolError(tr("Impossible de supprimer toutes les pages"))
    return _select(job, src, keep, dst)


def _select(job: Job, src: str, idx: list[int], dst: str) -> str:
    tmp = _tmp_near(dst)
    try:
        if qpdf():
            _run(job, [qpdf(), "--empty", "--pages", src, to_qpdf_ranges(idx), "--", tmp])
        elif has_pypdf():
            from pypdf import PdfReader, PdfWriter
            r = PdfReader(src)
            w = PdfWriter()
            for i in idx:
                w.add_page(r.pages[i])
            with open(tmp, "wb") as fh:
                w.write(fh)
        else:
            raise ToolError(tr("Installez qpdf (sudo apt install qpdf)"))
        _finish(tmp, dst)
        return dst
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def rotate_pages(job: Job, src: str, ranges: str, angle: int, dst: str) -> str:
    n = page_count(src)
    idx = parse_ranges(ranges, n)
    if not idx:
        raise ToolError(tr("Aucune page sélectionnée"))
    tmp = _tmp_near(dst)
    try:
        if qpdf():
            _run(job, [qpdf(), src, f"--rotate={'+' if angle >= 0 else ''}{angle}:{to_qpdf_ranges(sorted(set(idx)))}",
                       "--", tmp])
        elif has_pypdf():
            from pypdf import PdfReader, PdfWriter
            r = PdfReader(src)
            w = PdfWriter()
            wanted = set(idx)
            for i, page in enumerate(r.pages):
                if i in wanted:
                    page.rotate(angle)
                w.add_page(page)
            with open(tmp, "wb") as fh:
                w.write(fh)
        else:
            raise ToolError(tr("Installez qpdf (sudo apt install qpdf)"))
        _finish(tmp, dst)
        return dst
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def split_pages(job: Job, src: str, out_dir: str, every: int = 1) -> list[str]:
    """Éclate le document : un fichier toutes les `every` pages."""
    n = page_count(src)
    base = os.path.splitext(os.path.basename(src))[0]
    width = len(str(n))
    out = []
    for start in range(0, n, every):
        job.check_cancel()
        end = min(start + every, n)
        name = f"{base}-{start + 1:0{width}d}" + (f"-{end:0{width}d}" if end - start > 1 else "") + ".pdf"
        dst = os.path.join(out_dir, name)
        job.progress(start / max(n, 1), name)
        _select(job, src, list(range(start, end)), dst)
        out.append(dst)
    return out


def suggest_output(path: str, suffix: str) -> str:
    base, ext = os.path.splitext(path)
    out = f"{base}{suffix}{ext or '.pdf'}"
    k = 2
    while os.path.exists(out):
        out = f"{base}{suffix} ({k}){ext or '.pdf'}"
        k += 1
    return out


_unused = field
