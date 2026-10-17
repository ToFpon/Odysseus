"""Panneau d'aperçu (fonctionnalité Preview Panel, touche F4).

L'extension d'origine était une fenêtre séparée recollée à Nautilus par xdotool/wmctrl ; ici
c'est un vrai panneau de la fenêtre. Images, vidéos (miniature), PDF (première page), textes,
audio, dossiers, avec les informations détaillées. Préparé hors du fil principal, mis en cache.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Optional

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from ..backend import media, scanner, thumbnails
from ..backend.engine import human_size
from ..backend.scanner import Rec
from ..i18n import ntr, tr
from . import icons
from .common import clear_box, full_date, perms_label

TEXT_LIMIT = 64 * 1024
TEXT_LINES = 200
IMAGE_PX = 900
CACHE = 15
TEXT_EXTS = {".py", ".js", ".ts", ".sh", ".md", ".txt", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini",
             ".cfg", ".conf", ".css", ".html", ".htm", ".c", ".cpp", ".h", ".rs", ".go", ".rb", ".php", ".java",
             ".kt", ".swift", ".bash", ".zsh", ".fish", ".sql", ".log", ".csv", ".desktop", ".service"}


def _exif(path: str) -> list:
    try:
        from PIL import ExifTags, Image
    except ImportError:
        return []
    rows = []
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if not ex:
                return []
            tags = {ExifTags.TAGS.get(k, k): v for k, v in ex.items()}
            try:
                sub = ex.get_ifd(0x8769)
                tags.update({ExifTags.TAGS.get(k, k): v for k, v in sub.items()})
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return []
    for key, label in (("Make", tr("Appareil")), ("Model", tr("Modèle")), ("DateTimeOriginal", tr("Date de prise")),
                       ("FocalLength", tr("Focale")), ("ISOSpeedRatings", tr("ISO")),
                       ("ExposureTime", tr("Vitesse")), ("FNumber", tr("Ouverture"))):
        v = tags.get(key)
        if v not in (None, ""):
            if key == "ExposureTime":
                try:
                    v = f"1/{round(1 / float(v))} s" if float(v) < 1 else f"{float(v):g} s"
                except (ValueError, ZeroDivisionError):
                    pass
            elif key == "FNumber":
                try:
                    v = f"f/{float(v):g}"
                except ValueError:
                    pass
            elif key == "FocalLength":
                try:
                    v = f"{float(v):g} mm"
                except ValueError:
                    pass
            rows.append((label, str(v).strip("\x00 ")))
    return rows


def build_preview(rec: Rec, show_hidden: bool) -> dict:
    """Prépare l'aperçu (fil secondaire) : {'texture', 'text', 'rows'}."""
    out: dict = {"rows": [], "texture": None, "text": None}
    rows = out["rows"]
    path = rec.path
    if rec.is_dir:
        rows.append((tr("Type"), tr("Dossier")))
        if path:
            try:
                with os.scandir(path) as it:
                    entries = list(it)
                visible = [e for e in entries if show_hidden or not e.name.startswith(".")]
                ndirs = sum(1 for e in visible if e.is_dir())
                size = 0
                for e in visible:
                    try:
                        if not e.is_dir():
                            size += e.stat().st_size
                    except OSError:
                        pass
                rows.append((tr("Contenu"), ntr("{0} élément", "{0} éléments", len(visible)).format(len(visible))
                             + (" · " + ntr("{0} dossier", "{0} dossiers", ndirs).format(ndirs) if ndirs else "")))
                rows.append((tr("Fichiers (ce niveau)"), human_size(size)))
            except OSError as e:
                rows.append((tr("Contenu"), e.strerror or str(e)))
    else:
        ctype = rec.content_type
        rows.append((tr("Type"), scanner.type_description(ctype)))
        rows.append((tr("Taille"), human_size(rec.size) if rec.stat_ok else ""))
        if rec.get("packed") is not None:
            rows.append((tr("Compressé"), human_size(rec.get("packed"))))
    if rec.stat_ok and rec.mtime:
        rows.append((tr("Modifié"), full_date(rec.mtime)))
    if rec.mode:
        rows.append((tr("Droits"), perms_label(rec.mode)))
    if rec.is_link and path:
        try:
            rows.append((tr("Lien vers"), os.readlink(path)))
        except OSError:
            pass
    if rec.is_dir or not path:
        return out

    ctype = rec.content_type
    ext = os.path.splitext(rec.name)[1].lower()
    try:
        if ctype.startswith("image/"):
            info = GdkPixbuf.Pixbuf.get_file_info(path)
            if info and info[0] is not None:
                rows.append((tr("Dimensions"), f"{info[1]} × {info[2]} px"))
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, IMAGE_PX, IMAGE_PX, True)
            pb = pb.apply_embedded_orientation() or pb
            out["texture"] = Gdk.Texture.new_for_pixbuf(pb)
            rows.extend(_exif(path))
        elif ctype.startswith(("video/", "audio/")):
            info = media.probe(path)
            fmt = info.get("format", {})
            if fmt.get("duration"):
                rows.append((tr("Durée"), media.fmt_duration(float(fmt["duration"]))))
            if fmt.get("bit_rate"):
                rows.append((tr("Débit"), f"{int(fmt['bit_rate']) // 1000} kb/s"))
            for s in info.get("streams", []):
                if s.get("codec_type") == "video" and s.get("width"):
                    rows.append((tr("Vidéo"), f"{s.get('codec_name', '')} · {s['width']} × {s['height']}"))
                elif s.get("codec_type") == "audio":
                    ch = s.get("channels")
                    rows.append((tr("Audio"), f"{s.get('codec_name', '')}" + (f" · {ch} canaux" if ch else "")))
            tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
            for key, label in (("title", tr("Titre")), ("artist", tr("Artiste")), ("album", tr("Album")),
                               ("date", tr("Date"))):
                if tags.get(key):
                    rows.append((label, tags[key]))
            if ctype.startswith("video/"):
                png = thumbnails.lookup(path, rec.mtime, 512) or thumbnails.generate(path, ctype, rec.mtime, 512)
                if png:
                    out["texture"] = Gdk.Texture.new_from_filename(png)
        elif ctype == "application/pdf":
            try:
                import gi
                gi.require_version("Poppler", "0.18")
                from gi.repository import Poppler
                doc = Poppler.Document.new_from_file(thumbnails.uri_for(path), None)
                rows.append((tr("Pages"), str(doc.get_n_pages())))
                for getter, label in ((doc.get_title, tr("Titre")), (doc.get_author, tr("Auteur")),
                                      (doc.get_subject, tr("Sujet"))):
                    v = getter()
                    if v:
                        rows.append((label, v))
            except Exception:  # noqa: BLE001
                pass
            pb = thumbnails.pdf_page_pixbuf(path, IMAGE_PX)
            if pb is not None:
                out["texture"] = Gdk.Texture.new_for_pixbuf(pb)
        else:
            png = thumbnails.lookup(path, rec.mtime, 512)
            if png:
                out["texture"] = Gdk.Texture.new_from_filename(png)
        if out["texture"] is None and (ctype.startswith("text/") or ext in TEXT_EXTS or
                                       ctype in ("application/json", "application/xml", "application/x-shellscript")):
            with open(path, "rb") as fh:
                data = fh.read(TEXT_LIMIT)
            if b"\x00" not in data[:4096]:
                text = data.decode("utf-8", "replace")
                lines = text.splitlines()
                more = len(lines) > TEXT_LINES or len(data) == TEXT_LIMIT
                out["text"] = "\n".join(lines[:TEXT_LINES]) + ("\n…" if more else "")
                if not more:
                    rows.append((tr("Lignes"), str(len(lines))))
    except Exception as e:  # noqa: BLE001
        rows.append((tr("Aperçu"), str(e)))
    return out


class PreviewPanel(Gtk.Box):
    __gtype_name__ = "OdysseusPreviewPanel"

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["preview-panel"], width_request=240)
        self.window = window
        head = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=6, margin_bottom=6)
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE,
                               css_classes=["preview-title"])
        head.append(self.title)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           tooltip_text=tr("Fermer l'aperçu (F4)"), action_name="win.preview")
        head.append(close)
        self.append(head)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                               margin_bottom=12)
        self.append(Gtk.ScrolledWindow(child=self.content, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self._token = 0
        self._debounce = 0
        self._current = None
        self._cache: "OrderedDict[tuple, dict]" = OrderedDict()

    def show_selection(self, pane):
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(180, self._update, pane)

    def _update(self, pane):
        self._debounce = 0
        sel = pane.selected_recs()
        if len(sel) == 1:
            self._show_rec(sel[0])
        elif sel:
            self._show_many(sel)
        else:
            self._show_folder(pane)
        return False

    def _show_folder(self, pane):
        d = pane.current_dir()
        rec = scanner.rec_for_path(d) if d and pane.mode == "fs" else None
        if rec is None:
            self._token += 1
            self._current = None
            self.title.set_text(pane.title())
            clear_box(self.content)
            self._big_icon("folder")
            n = len(pane.model.items)
            self._rows([(tr("Contenu"), ntr("{0} élément", "{0} éléments", n).format(n))])
            return
        rec.name = pane.title()
        self._show_rec(rec)

    def _show_many(self, sel):
        self._token += 1
        self._current = None
        n = len(sel)
        self.title.set_text(ntr("{0} élément sélectionné", "{0} éléments sélectionnés", n).format(n))
        clear_box(self.content)
        self._big_icon("edit-select-all-symbolic")
        size = sum(r.size for r in sel if not r.is_dir)
        dirs = sum(1 for r in sel if r.is_dir)
        rows = [(tr("Fichiers"), str(n - dirs)), (tr("Dossiers"), str(dirs)), (tr("Taille des fichiers"), human_size(size))]
        self._rows(rows)

    def _show_rec(self, rec: Rec):
        key = (rec.path or rec.uri or rec.name, rec.mtime, rec.size)
        if key == self._current:
            return
        self._current = key
        self._token += 1
        token = self._token
        self.title.set_text(rec.name)
        self.title.set_tooltip_text(rec.path or rec.name)
        clear_box(self.content)
        if key in self._cache:
            self._apply(token, rec, self._cache[key])
            return
        icon = Gtk.Image(gicon=icons.gicon_for(rec), pixel_size=96, margin_top=24, margin_bottom=12)
        self.content.append(icon)
        spinner = Gtk.Spinner(spinning=True)
        self.content.append(spinner)
        from ..settings import settings
        hidden = settings["show_hidden"]

        def work():
            data = build_preview(rec, hidden)
            GLib.idle_add(self._done, token, rec, key, data)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, token, rec, key, data):
        self._cache[key] = data
        while len(self._cache) > CACHE:
            self._cache.popitem(last=False)
        if token == self._token:
            clear_box(self.content)
            self._apply(token, rec, data)
        return False

    def _apply(self, token, rec, data):
        tex = data.get("texture")
        if tex is not None:
            pic = Gtk.Picture(paintable=tex, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                              css_classes=["preview-image"], margin_top=6)
            w, h = tex.get_width(), tex.get_height()
            avail = max(200, self.get_width() - 24)
            pic.set_size_request(-1, int(min(520, avail * h / max(w, 1))))
            self.content.append(pic)
        elif data.get("text"):
            tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE,
                              top_margin=8, bottom_margin=8, left_margin=8, right_margin=8,
                              css_classes=["preview-text"])
            tv.get_buffer().set_text(data["text"])
            sw = Gtk.ScrolledWindow(child=tv, height_request=320, css_classes=["card"])
            self.content.append(sw)
        else:
            self._big_icon(None, rec)
        self._rows(data.get("rows", []))

    def _big_icon(self, name: Optional[str], rec: Optional[Rec] = None):
        img = Gtk.Image(pixel_size=96, margin_top=24, margin_bottom=12)
        if rec is not None:
            img.set_from_gicon(icons.gicon_for(rec))
        else:
            img.set_from_icon_name(name)
        self.content.append(img)

    def _rows(self, rows):
        grid = Gtk.Grid(column_spacing=12, row_spacing=4, margin_top=8)
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=1, valign=Gtk.Align.START, css_classes=["info-key"])
            vl = Gtk.Label(label=str(v), xalign=0, wrap=True, selectable=True, hexpand=True,
                           wrap_mode=Pango.WrapMode.WORD_CHAR)
            grid.attach(kl, 0, i, 1, 1)
            grid.attach(vl, 1, i, 1, 1)
        self.content.append(grid)
