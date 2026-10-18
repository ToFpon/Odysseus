"""Dialogues des outils PDF : compresser, fusionner, filigraner, pages."""

from __future__ import annotations

import os
import threading
from typing import Optional

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango

from ..backend import pdf, thumbnails
from ..backend.fileops import unique_name
from ..backend.engine import human_size
from ..i18n import ntr, tr
from ..settings import settings
from .common import alert, choose_files, choose_folder, choose_save, combo_row, dialog_shell, path_row
from .jobs import finished_toast, manager

PDF_FILTER = [(tr("Documents PDF"), ["*.pdf", "*.PDF"])]


def _thumb_async(path: str, px: int, page: int, callback):
    def work():
        pb = thumbnails.pdf_page_pixbuf(path, px, page)
        tex = Gdk.Texture.new_for_pixbuf(pb) if pb is not None else None
        GLib.idle_add(lambda: (callback(tex), False)[1])
    threading.Thread(target=work, daemon=True).start()


def _submit(window, title, func, success, folder=None, on_done=None):
    def done(job):
        finished_toast(window, job, success(job) if job.state == "done" else "", folder)
        if on_done:
            on_done(job)
    job = manager.submit(title, func, done)
    if hasattr(window, "_watch_progress"):
        window._watch_progress(job)
    return job


# ---------------------------------------------------------------------------
# Compresser
# ---------------------------------------------------------------------------

class CompressDialog:
    def __init__(self, window, paths: list[str]):
        self.window = window
        self.paths = paths
        self.dialog, page, self.action = dialog_shell(tr("Compresser le PDF"), tr("Compresser"), 560, 600)
        g = Adw.PreferencesGroup(title=ntr("{0} fichier", "{0} fichiers", len(paths)).format(len(paths)))
        for p in paths[:12]:
            r = Adw.ActionRow(title=os.path.basename(p), subtitle=human_size(os.path.getsize(p)))
            r.add_prefix(Gtk.Image(icon_name="x-office-document-symbolic"))
            g.add(r)
        if len(paths) > 12:
            g.add(Adw.ActionRow(title=tr("… et {0} autre(s)").format(len(paths) - 12)))
        page.add(g)
        g = Adw.PreferencesGroup(title=tr("Réglages"))
        levels = [k for k, _l in pdf.GS_LEVELS]
        cur = settings["pdf_compress_level"]
        self.level = combo_row(tr("Qualité"), [l for _k, l in pdf.GS_LEVELS], levels.index(cur) if cur in levels else 1)
        g.add(self.level)
        self.gray = Adw.SwitchRow(title=tr("Convertir en niveaux de gris"), subtitle=tr("Encore plus léger"))
        g.add(self.gray)
        self.replace = Adw.SwitchRow(title=tr("Remplacer les fichiers d'origine"))
        g.add(self.replace)
        self.suffix = Adw.EntryRow(title=tr("Suffixe des fichiers produits"), text=tr("-optimisé"))
        self.replace.bind_property("active", self.suffix, "sensitive",
                                   GObject.BindingFlags.INVERT_BOOLEAN | GObject.BindingFlags.SYNC_CREATE)
        g.add(self.suffix)
        page.add(g)
        self.action.connect("clicked", self._go)

    def _go(self, *_):
        level = [k for k, _l in pdf.GS_LEVELS][self.level.get_selected()]
        settings["pdf_compress_level"] = level
        suffix = self.suffix.get_text() or "-optimisé"
        items = [(p, p if self.replace.get_active() else pdf.suggest_output(p, suffix)) for p in self.paths]
        gray = self.gray.get_active()
        self.dialog.close()
        win = self.window

        def done(job):
            if job.state != "done":
                return
            lines = []
            for r in job.result:
                if r.kept_original:
                    lines.append(tr("{0} : déjà optimal, laissé tel quel ({1})").format(
                        os.path.basename(r.src), human_size(r.before)))
                else:
                    gain = 100 - 100 * r.after / max(r.before, 1)
                    lines.append(f"{os.path.basename(r.dst)} : {human_size(r.before)} → {human_size(r.after)} "
                                 f"(−{gain:.0f} %)")
            alert(win, tr("Compression terminée"), "\n".join(lines))
        _submit(win, ntr("Compression de {0} PDF", "Compression de {0} PDF", len(items)).format(len(items)),
                lambda j: pdf.compress_many(j, items, level, gray), lambda j: "", on_done=done)

    def present(self, parent):
        self.dialog.present(parent)


# ---------------------------------------------------------------------------
# Fusionner
# ---------------------------------------------------------------------------

class _MergeRow(Adw.ActionRow):
    def __init__(self, dialog: "MergeDialog", path: str):
        super().__init__(title=os.path.basename(path), subtitle=os.path.dirname(path))
        self.path = path
        self.dialog = dialog
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        handle = Gtk.Image(icon_name="list-drag-handle-symbolic", css_classes=["dim-label"])
        self.add_prefix(handle)
        self.thumb = Gtk.Picture(width_request=34, height_request=46, can_shrink=True,
                                 content_fit=Gtk.ContentFit.CONTAIN, css_classes=["pdf-thumb"])
        self.add_prefix(self.thumb)
        _thumb_async(path, 92, 0, lambda tex: tex is not None and self.thumb.set_paintable(tex))
        self.n = pdf.page_count(path)
        self.pages = Gtk.Entry(placeholder_text=tr("toutes ({0} p.)").format(self.n), width_chars=12,
                               valign=Gtk.Align.CENTER, tooltip_text=tr("Pages à garder, ex. 1-3, 5, 8-"))
        self.pages.connect("changed", lambda *_: dialog.refresh())
        self.add_suffix(self.pages)
        for icon, step, tip in (("go-up-symbolic", -1, tr("Monter")), ("go-down-symbolic", 1, tr("Descendre"))):
            b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text=tip)
            b.connect("clicked", lambda *_a, s=step: dialog.move(self, s))
            self.add_suffix(b)
        rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                        tooltip_text=tr("Retirer"))
        rm.connect("clicked", lambda *_: dialog.remove(self))
        self.add_suffix(rm)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(self.path))
        handle_area = self
        handle_area.add_controller(drag)
        drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop.connect("drop", lambda _t, v, x, y: dialog.drop_on(v, self))
        self.add_controller(drop)

    def ranges_ok(self) -> str:
        try:
            idx = pdf.parse_ranges(self.pages.get_text(), self.n)
            return "" if idx else tr("aucune page")
        except ValueError as e:
            return str(e)


class MergeDialog:
    def __init__(self, window, paths: list[str]):
        self.window = window
        self.dialog, page, self.action = dialog_shell(tr("Fusionner des PDF"), tr("Fusionner"), 640, 640)
        self.group = Adw.PreferencesGroup(title=tr("Ordre des documents"),
                                          description=tr("Glissez les lignes pour changer l'ordre. "
                                                         "Indiquez les pages à garder de chaque fichier."))
        add = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"], tooltip_text=tr("Ajouter des PDF…"))
        add.connect("clicked", self._add)
        self.group.set_header_suffix(add)
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.group.add(self.list)
        page.add(self.group)
        g = Adw.PreferencesGroup(title=tr("Résultat"))
        first = paths[0] if paths else os.path.expanduser("~/fusion.pdf")
        folder = os.path.dirname(first)
        self.out = path_row(tr("Enregistrer sous"), os.path.join(folder, unique_name(folder, tr("fusion.pdf"))),
                            lambda r: choose_save(self.dialog, tr("Enregistrer le PDF"), r.get_text(), r.set_text),
                            icon="document-save-symbolic")
        g.add(self.out)
        self.summary = Gtk.Label(xalign=0, css_classes=["dim-label"], margin_top=8, wrap=True)
        g.add(self.summary)
        page.add(g)
        self.rows: list[_MergeRow] = []
        for p in paths:
            self._append(p)
        self.action.connect("clicked", self._go)
        self.refresh()

    def _append(self, path):
        row = _MergeRow(self, path)
        self.rows.append(row)
        self.list.append(row)

    def _add(self, *_):
        start = os.path.dirname(self.rows[0].path) if self.rows else os.path.expanduser("~")
        choose_files(self.dialog, tr("Ajouter des PDF"), start,
                     lambda ps: ([self._append(p) for p in ps if pdf.is_pdf(p)], self.refresh()), filters=PDF_FILTER)

    def _rebuild(self):
        while (c := self.list.get_first_child()) is not None:
            self.list.remove(c)
        for r in self.rows:
            self.list.append(r)
        self.refresh()

    def move(self, row, step):
        i = self.rows.index(row)
        j = max(0, min(len(self.rows) - 1, i + step))
        if i != j:
            self.rows.insert(j, self.rows.pop(i))
            self._rebuild()

    def remove(self, row):
        self.rows.remove(row)
        self._rebuild()

    def drop_on(self, path: str, target: _MergeRow) -> bool:
        src = next((r for r in self.rows if r.path == path), None)
        if src is None or src is target:
            return False
        self.rows.remove(src)
        self.rows.insert(self.rows.index(target), src)
        self._rebuild()
        return True

    def refresh(self):
        problems = [f"{r.get_title()} : {r.ranges_ok()}" for r in self.rows if r.ranges_ok()]
        total = 0
        for r in self.rows:
            try:
                total += len(pdf.parse_ranges(r.pages.get_text(), r.n))
            except ValueError:
                pass
        if len(self.rows) < 2 and not any(r.pages.get_text().strip() for r in self.rows):
            problems.append(tr("Ajoutez au moins deux documents"))
        self.summary.set_text("\n".join(problems) if problems else
                              ntr("{0} page au total", "{0} pages au total", total).format(total))
        self.action.set_sensitive(not problems and bool(self.rows))

    def _go(self, *_):
        items = [(r.path, r.pages.get_text()) for r in self.rows]
        out = self.out.get_text().strip()
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        if os.path.lexists(out):       # jamais par-dessus une fusion précédente
            out = os.path.join(os.path.dirname(out), unique_name(os.path.dirname(out), os.path.basename(out)))
        self.dialog.close()
        _submit(self.window, tr("Fusion de {0} PDF").format(len(items)), lambda j: pdf.merge(j, items, out),
                lambda j: tr("{0} créé").format(os.path.basename(out)), os.path.dirname(out))

    def present(self, parent):
        self.dialog.present(parent)


# ---------------------------------------------------------------------------
# Filigrane
# ---------------------------------------------------------------------------

POSITION_LABELS = [tr("Centre"), tr("Mosaïque (répété)"), tr("En haut à gauche"), tr("En haut à droite"),
                   tr("En bas à gauche"), tr("En bas à droite"), tr("En haut"), tr("En bas")]
COLORS = [(tr("Rouge"), (0.8, 0.0, 0.0)), (tr("Gris"), (0.5, 0.5, 0.5)), (tr("Bleu"), (0.0, 0.2, 0.8)),
          (tr("Noir"), (0.0, 0.0, 0.0))]


class WatermarkDialog:
    def __init__(self, window, paths: list[str]):
        self.window = window
        self.paths = paths
        self.dialog, page, self.action = dialog_shell(tr("Filigrane PDF"), tr("Appliquer"), 760, 720)
        # aperçu à droite
        box = Gtk.Box(spacing=12)
        prefs = Adw.PreferencesPage(hexpand=True)
        tv = self.dialog.get_child()
        tv.set_content(box)
        box.append(prefs)
        self.preview = Gtk.Picture(width_request=260, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                                   margin_top=18, margin_end=18, margin_bottom=18, valign=Gtk.Align.START,
                                   css_classes=["pdf-thumb"])
        box.append(self.preview)
        page = prefs

        g = Adw.PreferencesGroup(title=tr("Filigrane"))
        self.kind = combo_row(tr("Type"), [tr("Texte"), tr("Image")])
        g.add(self.kind)
        self.text = Adw.EntryRow(title=tr("Texte"), text=tr("CONFIDENTIEL"))
        g.add(self.text)
        self.image = path_row(tr("Image (PNG, JPEG, SVG…)"), "",
                              lambda r: choose_files(self.dialog, tr("Choisir une image"), os.path.expanduser("~"),
                                                     lambda ps: r.set_text(ps[0]), multiple=False,
                                                     filters=[(tr("Images"), ["*.png", "*.jpg", "*.jpeg", "*.svg",
                                                                              "*.webp", "*.PNG", "*.JPG"])]))
        g.add(self.image)
        self.font = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER,
                                         font_desc=Pango.FontDescription.from_string("Sans Bold 60"),
                                         use_size=False, use_font=True)
        self.font_row = Adw.ActionRow(title=tr("Police"))
        self.font_row.add_suffix(self.font)
        g.add(self.font_row)
        self.size = Adw.SpinRow.new_with_range(6, 400, 2)
        self.size.set_title(tr("Taille (points)"))
        self.size.set_value(60)
        g.add(self.size)
        self.color = combo_row(tr("Couleur"), [c for c, _v in COLORS] + [tr("Personnalisée…")])
        g.add(self.color)
        self.custom = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse("#935cd8")
        self.custom.set_rgba(rgba)
        self.color.add_suffix(self.custom)
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Disposition"))
        self.opacity = Adw.SpinRow.new_with_range(5, 100, 5)
        self.opacity.set_title(tr("Opacité (%)"))
        self.opacity.set_value(30)
        g.add(self.opacity)
        self.angle = Adw.SpinRow.new_with_range(-180, 180, 5)
        self.angle.set_title(tr("Angle (degrés)"))
        self.angle.set_value(45)
        g.add(self.angle)
        self.position = combo_row(tr("Position"), POSITION_LABELS)
        g.add(self.position)
        self.pages = Adw.EntryRow(title=tr("Pages (vide = toutes, ex. 1, 3-5)"))
        g.add(self.pages)
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Sécurité"))
        self.flatten = Adw.SwitchRow(title=tr("Aplatir (pages converties en images)"),
                                     subtitle=tr("Le filigrane ne peut plus être retiré ; texte non sélectionnable"))
        g.add(self.flatten)
        self.dpi = Adw.SpinRow.new_with_range(72, 600, 25)
        self.dpi.set_title(tr("Résolution de l'aplatissement (ppp)"))
        self.dpi.set_value(200)
        g.add(self.dpi)
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Résultat"))
        if len(paths) == 1:
            self.out = path_row(tr("Enregistrer sous"), pdf.suggest_output(paths[0], tr("-filigrane")),
                                lambda r: choose_save(self.dialog, tr("Enregistrer le PDF"), r.get_text(), r.set_text),
                                icon="document-save-symbolic")
        else:
            self.out = Adw.EntryRow(title=tr("Suffixe des fichiers produits"), text=tr("-filigrane"))
        g.add(self.out)
        page.add(g)

        for w, sig in ((self.kind, "notify::selected"), (self.text, "changed"), (self.image, "changed"),
                       (self.font, "notify::font-desc"), (self.size, "notify::value"), (self.color, "notify::selected"),
                       (self.custom, "notify::rgba"), (self.opacity, "notify::value"), (self.angle, "notify::value"),
                       (self.position, "notify::selected"), (self.flatten, "notify::active")):
            w.connect(sig, lambda *_: self._changed())
        self.action.connect("clicked", self._go)
        self._page_tex: Optional[GdkPixbuf.Pixbuf] = None
        self._preview_id = 0
        self._load_page()
        self._changed()

    def options(self) -> pdf.WatermarkOptions:
        img = self.kind.get_selected() == 1
        ci = self.color.get_selected()
        if ci < len(COLORS):
            color = COLORS[ci][1]
        else:
            c = self.custom.get_rgba()
            color = (c.red, c.green, c.blue)
        desc = self.font.get_font_desc()
        font = desc.to_string() if desc else "Sans Bold"
        # la taille vient de la ligne « Taille », pas de la police
        font = " ".join(p for p in font.split() if not p.replace(".", "").isdigit()) or "Sans Bold"
        return pdf.WatermarkOptions(
            text="" if img else self.text.get_text(), image=self.image.get_text().strip() if img else "",
            font=font, size=self.size.get_value() if not img else max(5, min(100, self.size.get_value())),
            color=color, opacity=self.opacity.get_value() / 100, angle=self.angle.get_value(),
            position=pdf.POSITIONS[self.position.get_selected()], pages=self.pages.get_text(),
            flatten=self.flatten.get_active(), dpi=int(self.dpi.get_value()))

    def _changed(self):
        img = self.kind.get_selected() == 1
        self.text.set_visible(not img)
        self.font_row.set_visible(not img)
        self.image.set_visible(img)
        self.size.set_title(tr("Largeur (% de la page)") if img else tr("Taille (points)"))
        self.custom.set_visible(self.color.get_selected() == len(COLORS))
        self.color.set_visible(not img)
        self.dpi.set_sensitive(self.flatten.get_active())
        o = self.options()
        ok = bool(o.text.strip()) or (bool(o.image) and os.path.isfile(o.image))
        self.action.set_sensitive(ok)
        if self._preview_id:
            GLib.source_remove(self._preview_id)
        self._preview_id = GLib.timeout_add(150, self._render_preview)

    def _load_page(self):
        if not self.paths:
            return

        def work():
            pb = thumbnails.pdf_page_pixbuf(self.paths[0], 520, 0)
            sizes = pdf.page_sizes(self.paths[0])[:1]
            GLib.idle_add(lambda: (self._set_page(pb, sizes[0] if sizes else (595, 842)), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _set_page(self, pb, size):
        self._page_tex = pb
        self._page_size = size
        self._render_preview()

    def _render_preview(self):
        self._preview_id = 0
        pb = self._page_tex
        if pb is None:
            return False
        import cairo
        w, h = pb.get_width(), pb.get_height()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        ctx = cairo.Context(surf)
        Gdk.cairo_set_source_pixbuf(ctx, pb, 0, 0)
        ctx.paint()
        pw, ph = self._page_size
        ctx.scale(w / pw, h / ph)
        o = self.options()
        try:
            if o.image and os.path.isfile(o.image):
                pdf._draw_image(ctx, o, pw, ph, {})
            elif o.text.strip():
                pdf._draw_text(ctx, o, pw, ph)
        except Exception:  # noqa: BLE001
            pass
        surf.flush()
        tex = Gdk.Texture.new_for_pixbuf(Gdk.pixbuf_get_from_surface(surf, 0, 0, w, h))
        self.preview.set_paintable(tex)
        return False

    def _go(self, *_):
        o = self.options()
        if o.pages.strip():
            # pages vérifiées tout de suite (« page 9 » sur un PDF de 2 pages échouait pendant le travail)
            for p in self.paths:
                try:
                    pdf.parse_ranges(o.pages, pdf.page_count(p))
                except ValueError as e:
                    alert(self.dialog, tr("Pages invalides"), f"{os.path.basename(p)} : {e}")
                    return
        if len(self.paths) == 1:
            items = [(self.paths[0], self.out.get_text().strip())]
        else:
            suffix = self.out.get_text() or tr("-filigrane")
            items = [(p, pdf.suggest_output(p, suffix)) for p in self.paths]
        self.dialog.close()

        def work(j):
            out = []
            for i, (src, dst) in enumerate(items):
                j.check_cancel()
                j.progress(i / len(items), os.path.basename(src))
                out.append(pdf.watermark(j, src, dst, o))
            return out
        _submit(self.window, tr("Filigrane sur {0} PDF").format(len(items)), work,
                lambda j: ntr("{0} PDF filigrané", "{0} PDF filigranés", len(items)).format(len(items)),
                os.path.dirname(items[0][1]))

    def present(self, parent):
        self.dialog.present(parent)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

OPERATIONS = [("extract", tr("Extraire les pages choisies")), ("delete", tr("Supprimer les pages choisies")),
              ("rotate_right", tr("Pivoter de 90° vers la droite")), ("rotate_left", tr("Pivoter de 90° vers la gauche")),
              ("rotate_180", tr("Pivoter de 180°")), ("split", tr("Éclater en fichiers séparés"))]


class PagesDialog:
    def __init__(self, window, paths: list[str]):
        self.window = window
        self.path = paths[0]
        self.n = pdf.page_count(self.path)
        self.dialog, page, self.action = dialog_shell(tr("Pages PDF"), tr("Appliquer"), 720, 720)
        g = Adw.PreferencesGroup(title=os.path.basename(self.path),
                                 description=ntr("{0} page", "{0} pages", self.n).format(self.n) +
                                 " · " + tr("cliquez sur les vignettes pour choisir les pages"))
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.MULTIPLE, max_children_per_line=8,
                                min_children_per_line=3, homogeneous=True, row_spacing=8, column_spacing=8,
                                activate_on_single_click=True)
        self.flow.connect("selected-children-changed", self._from_flow)
        sw = Gtk.ScrolledWindow(child=self.flow, min_content_height=240, max_content_height=320,
                                propagate_natural_height=True)
        g.add(sw)
        page.add(g)
        g = Adw.PreferencesGroup(title=tr("Opération"))
        self.op = combo_row(tr("Action"), [l for _k, l in OPERATIONS])
        self.op.connect("notify::selected", lambda *_: self._refresh())
        g.add(self.op)
        self.ranges = Adw.EntryRow(title=tr("Pages (ex. 1-3, 5, 8-)"))
        self.ranges.connect("changed", lambda *_: self._refresh())
        g.add(self.ranges)
        self.every = Adw.SpinRow.new_with_range(1, max(1, self.n), 1)
        self.every.set_title(tr("Pages par fichier"))
        g.add(self.every)
        self.out = path_row(tr("Enregistrer sous"), pdf.suggest_output(self.path, tr("-pages")),
                            lambda r: choose_save(self.dialog, tr("Enregistrer le PDF"), r.get_text(), r.set_text),
                            icon="document-save-symbolic")
        g.add(self.out)
        self.out_dir = path_row(tr("Dossier de destination"), os.path.dirname(self.path),
                                lambda r: choose_folder(self.dialog, tr("Dossier de destination"), r.get_text(),
                                                        r.set_text))
        g.add(self.out_dir)
        self.msg = Gtk.Label(xalign=0, css_classes=["dim-label"], margin_top=8, wrap=True)
        g.add(self.msg)
        page.add(g)
        self._syncing = False
        self._fill_thumbs()
        self.action.connect("clicked", self._go)
        self._refresh()

    def _fill_thumbs(self):
        limit = min(self.n, 200)
        pics = []
        for i in range(limit):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pic = Gtk.Picture(width_request=72, height_request=96, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                              css_classes=["pdf-thumb"])
            box.append(pic)
            box.append(Gtk.Label(label=str(i + 1), css_classes=["caption"]))
            self.flow.append(box)
            pics.append(pic)
        path = self.path

        def work():
            for i, pic in enumerate(pics):
                pb = thumbnails.pdf_page_pixbuf(path, 140, i)
                if pb is None:
                    break
                tex = Gdk.Texture.new_for_pixbuf(pb)
                GLib.idle_add(lambda p=pic, t=tex: (p.set_paintable(t), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _from_flow(self, *_):
        if self._syncing:
            return
        idx = sorted(c.get_index() for c in self.flow.get_selected_children())
        self._syncing = True
        self.ranges.set_text(pdf.to_qpdf_ranges(idx).replace(",", ", "))
        self._syncing = False

    def _refresh(self):
        key = OPERATIONS[self.op.get_selected()][0]
        split = key == "split"
        self.ranges.set_visible(not split)
        self.every.set_visible(split)
        self.out.set_visible(not split)
        self.out_dir.set_visible(split)
        problem = ""
        if not split:
            try:
                idx = pdf.parse_ranges(self.ranges.get_text(), self.n) if self.ranges.get_text().strip() else []
            except ValueError as e:
                idx, problem = [], str(e)
            if not idx and not problem:
                problem = tr("Choisissez des pages")
            if not self._syncing and not problem:
                self._syncing = True
                self.flow.unselect_all()
                for i in set(idx):
                    child = self.flow.get_child_at_index(i)
                    if child is not None:
                        self.flow.select_child(child)
                self._syncing = False
            if not problem:
                self.msg.set_text(ntr("{0} page choisie", "{0} pages choisies", len(set(idx))).format(len(set(idx))))
        else:
            n = int(self.every.get_value())
            self.msg.set_text(ntr("{0} fichier sera créé", "{0} fichiers seront créés", -(-self.n // n)).format(
                -(-self.n // n)))
        if problem:
            self.msg.set_text(problem)
        self.action.set_sensitive(not problem)

    def _go(self, *_):
        key = OPERATIONS[self.op.get_selected()][0]
        src, ranges = self.path, self.ranges.get_text()
        out = self.out.get_text().strip()
        self.dialog.close()
        if key == "split":
            folder, every = self.out_dir.get_text().strip(), int(self.every.get_value())
            _submit(self.window, tr("Éclatement de {0}").format(os.path.basename(src)),
                    lambda j: pdf.split_pages(j, src, folder, every),
                    lambda j: ntr("{0} fichier créé", "{0} fichiers créés", len(j.result)).format(len(j.result)), folder)
            return
        funcs = {
            "extract": lambda j: pdf.extract_pages(j, src, ranges, out),
            "delete": lambda j: pdf.delete_pages(j, src, ranges, out),
            "rotate_right": lambda j: pdf.rotate_pages(j, src, ranges, 90, out),
            "rotate_left": lambda j: pdf.rotate_pages(j, src, ranges, -90, out),
            "rotate_180": lambda j: pdf.rotate_pages(j, src, ranges, 180, out),
        }
        _submit(self.window, tr("Pages de {0}").format(os.path.basename(src)), funcs[key],
                lambda j: tr("{0} créé").format(os.path.basename(out)), os.path.dirname(out))

    def present(self, parent):
        self.dialog.present(parent)
