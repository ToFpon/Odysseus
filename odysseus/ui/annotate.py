"""Annoter une image (reprise d'Annotate Image).

Nouveautés : toute image lisible par GdkPixbuf (PNG, JPEG, WebP…), trait, main levée,
surligneur, pixellisation (pour masquer une information sur une capture), formes pleines,
choix de la police, enregistrement en PNG ou JPEG. Fenêtre indépendante (non transitoire) pour
garder réduire / agrandir sous Mutter.
"""

from __future__ import annotations

import math
import os

import cairo
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from ..i18n import tr
from .common import alert, choose_save, toast

TOOLS = [("rect", "rectangle", tr("Rectangle")), ("ellipse", "ellipse", tr("Ellipse")),
         ("arrow", "arrow", tr("Flèche")), ("line", "line", tr("Trait")), ("pen", "pen", tr("Main levée")),
         ("highlight", "highlight", tr("Surligneur")), ("pixelate", "pixelate", tr("Pixelliser (masquer)")),
         ("text", "text", tr("Texte"))]
TOOL_ICONS = {"rect": "checkbox-symbolic", "ellipse": "radio-symbolic", "arrow": "go-up-symbolic",
              "line": "list-remove-symbolic", "pen": "document-edit-symbolic", "highlight": "format-text-strikethrough-symbolic",
              "pixelate": "view-grid-symbolic", "text": "format-text-bold-symbolic"}


class Shape:
    def __init__(self, tool, color, opacity, width, fill=False, text="", font="Sans Bold 24"):
        self.tool = tool
        self.color = color
        self.opacity = opacity
        self.width = width
        self.fill = fill
        self.text = text
        self.font = font
        self.points: list[tuple[float, float]] = []

    @property
    def box(self):
        (x1, y1), (x2, y2) = self.points[0], self.points[-1]
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)


def draw_shape(ctx, s: Shape, base: cairo.ImageSurface):
    r, g, b = s.color
    ctx.save()
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_line_width(s.width)
    if s.tool == "pixelate":
        x, y, w, h = (int(v) for v in s.box)
        if w > 2 and h > 2:
            block = max(6, int(s.width * 3))
            small = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, w // block), max(1, h // block))
            sc = cairo.Context(small)
            sc.scale(small.get_width() / w, small.get_height() / h)
            sc.set_source_surface(base, -x, -y)
            sc.paint()
            ctx.rectangle(x, y, w, h)
            ctx.clip()
            ctx.translate(x, y)
            ctx.scale(w / small.get_width(), h / small.get_height())
            ctx.set_source_surface(small, 0, 0)
            ctx.get_source().set_filter(cairo.FILTER_NEAREST)
            ctx.paint()
        ctx.restore()
        return
    ctx.set_source_rgba(r, g, b, s.opacity)
    if s.tool in ("rect", "highlight"):
        x, y, w, h = s.box
        ctx.rectangle(x, y, w, h)
        if s.tool == "highlight":
            ctx.set_source_rgba(r, g, b, min(s.opacity, 0.4))
            ctx.fill()
        elif s.fill:
            ctx.fill()
        else:
            ctx.stroke()
    elif s.tool == "ellipse":
        x, y, w, h = s.box
        if w > 0 and h > 0:
            ctx.save()
            ctx.translate(x + w / 2, y + h / 2)
            ctx.scale(w / 2, h / 2)
            ctx.arc(0, 0, 1, 0, 2 * math.pi)
            ctx.restore()
            ctx.fill() if s.fill else ctx.stroke()
    elif s.tool in ("line", "arrow"):
        (x1, y1), (x2, y2) = s.points[0], s.points[-1]
        ctx.move_to(x1, y1)
        ctx.line_to(x2, y2)
        ctx.stroke()
        if s.tool == "arrow":
            ang = math.atan2(y2 - y1, x2 - x1)
            ln = max(18, s.width * 4.5)
            ctx.move_to(x2, y2)
            for side in (-1, 1):
                ctx.line_to(x2 - ln * math.cos(ang - side * math.pi / 7), y2 - ln * math.sin(ang - side * math.pi / 7))
            ctx.close_path()
            ctx.fill()
    elif s.tool == "pen":
        if len(s.points) > 1:
            ctx.move_to(*s.points[0])
            for p in s.points[1:]:
                ctx.line_to(*p)
            ctx.stroke()
    elif s.tool == "text":
        try:
            import gi
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import PangoCairo
            layout = PangoCairo.create_layout(ctx)
            layout.set_font_description(Pango.FontDescription.from_string(s.font))
            layout.set_text(s.text, -1)
            ctx.move_to(*s.points[0])
            PangoCairo.show_layout(ctx, layout)
        except Exception:  # noqa: BLE001
            ctx.set_font_size(24)
            ctx.move_to(s.points[0][0], s.points[0][1] + 24)
            ctx.show_text(s.text)
    ctx.restore()


class AnnotateWindow(Adw.Window):
    __gtype_name__ = "OdysseusAnnotate"

    def __init__(self, main_window, path: str):
        super().__init__(title=tr("Annoter — {0}").format(os.path.basename(path)), default_width=1100,
                         default_height=760)
        self.main = main_window
        self.path = path
        self.shapes: list[Shape] = []
        self.redo: list[Shape] = []
        self.current = None
        self.tool = "rect"
        self.zoom = None          # None = ajusté à la fenêtre
        self.scale = 1.0
        self.ox = self.oy = 0.0
        self.dirty = False
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            pb = pb.apply_embedded_orientation() or pb
        except GLib.Error as e:
            alert(main_window, tr("Image illisible"), e.message)
            GLib.idle_add(self.destroy)
            return
        self.img_w, self.img_h = pb.get_width(), pb.get_height()
        self.base = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.img_w, self.img_h)
        ctx = cairo.Context(self.base)
        Gdk.cairo_set_source_pixbuf(ctx, pb, 0, 0)
        ctx.paint()
        self.toast_overlay = Adw.ToastOverlay()
        self._build()
        self.connect("close-request", self._on_close)

    def _build(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(decoration_layout=":minimize,maximize,close")
        for icon, tip, cb in (("edit-undo-symbolic", tr("Annuler (Ctrl+Z)"), self.undo),
                              ("edit-redo-symbolic", tr("Rétablir (Ctrl+Maj+Z)"), self.redo_one)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", lambda *_a, cb=cb: cb())
            hb.pack_start(b)
        save = Gtk.Button(label=tr("Enregistrer"), css_classes=["suggested-action"], tooltip_text=tr("Ctrl+S"))
        save.connect("clicked", lambda *_: self.save(self.path))
        hb.pack_end(save)
        sa = Gtk.Button(icon_name="document-save-as-symbolic", tooltip_text=tr("Enregistrer sous… (Ctrl+Maj+S)"))
        sa.connect("clicked", lambda *_: self.save_as())
        hb.pack_end(sa)
        tv.add_top_bar(hb)

        bar = Gtk.Box(spacing=6, margin_start=10, margin_end=10, margin_top=6, margin_bottom=6)
        group = None
        for key, _icon, label in TOOLS:
            b = Gtk.ToggleButton(icon_name=TOOL_ICONS[key], tooltip_text=label, group=group, active=group is None)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and setattr(self, "tool", k))
            bar.append(b)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), tooltip_text=tr("Couleur"))
        rgba = Gdk.RGBA()
        rgba.parse("#e01b24")
        self.color.set_rgba(rgba)
        bar.append(self.color)
        bar.append(Gtk.Label(label=tr("Épaisseur")))
        self.width = Gtk.SpinButton.new_with_range(1, 40, 1)
        self.width.set_value(4)
        bar.append(self.width)
        bar.append(Gtk.Label(label=tr("Opacité")))
        self.opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
        self.opacity.set_value(0.9)
        self.opacity.set_size_request(110, -1)
        bar.append(self.opacity)
        self.fill = Gtk.ToggleButton(icon_name="color-select-symbolic", tooltip_text=tr("Formes pleines"))
        bar.append(self.fill)
        self.font = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), tooltip_text=tr("Police du texte"),
                                         font_desc=Pango.FontDescription.from_string("Sans Bold 28"))
        bar.append(self.font)
        bar.append(Gtk.Box(hexpand=True))
        for icon, tip, cb in (("zoom-out-symbolic", tr("Zoom arrière"), lambda: self.zoom_step(1 / 1.25)),
                              ("zoom-in-symbolic", tr("Zoom avant"), lambda: self.zoom_step(1.25)),
                              ("zoom-fit-best-symbolic", tr("Ajuster à la fenêtre"), lambda: self.set_zoom(None))):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", lambda *_a, cb=cb: cb())
            bar.append(b)
        self.zoom_lbl = Gtk.Button(label="100 %", css_classes=["flat"], tooltip_text=tr("Taille réelle (100 %)"))
        self.zoom_lbl.connect("clicked", lambda *_: self.set_zoom(1.0))
        bar.append(self.zoom_lbl)
        tv.add_top_bar(bar)

        self.canvas = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.canvas.set_draw_func(self._draw)
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("crosshair", None))
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.canvas.add_controller(drag)
        sc = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self._scroll)
        self.canvas.add_controller(sc)
        self.canvas.connect("resize", lambda *_: self.zoom is None and self.canvas.queue_draw())
        self.scroller = Gtk.ScrolledWindow(child=self.canvas)
        tv.set_content(self.scroller)
        self.toast_overlay.set_child(tv)
        self.set_content(self.toast_overlay)
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        for trig, cb in (("<Ctrl>z", self.undo), ("<Ctrl><Shift>z", self.redo_one), ("<Ctrl>s", lambda: self.save(self.path)),
                         ("<Ctrl><Shift>s", self.save_as), ("<Ctrl>plus", lambda: self.zoom_step(1.25)),
                         ("<Ctrl>minus", lambda: self.zoom_step(1 / 1.25)), ("<Ctrl>0", lambda: self.set_zoom(1.0))):
            keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trig),
                                               Gtk.CallbackAction.new(lambda *_a, cb=cb: cb() or True)))
        self.add_controller(keys)

    # -- zoom ----------------------------------------------------------------
    def _fit_scale(self, w, h):
        return min(w / self.img_w, h / self.img_h, 2.0)

    def set_zoom(self, z):
        self.zoom = z
        if z is None:
            self.canvas.set_content_width(0)
            self.canvas.set_content_height(0)
        else:
            self.canvas.set_content_width(int(self.img_w * z) + 40)
            self.canvas.set_content_height(int(self.img_h * z) + 40)
        self.canvas.queue_draw()

    def zoom_step(self, f):
        self.set_zoom(max(0.05, min(8.0, self.scale * f)))

    def _scroll(self, ctrl, dx, dy):
        ev = ctrl.get_current_event()
        if ev and ev.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK:
            self.zoom_step(1 / 1.15 if dy > 0 else 1.15)
            return True
        return False

    # -- dessin --------------------------------------------------------------
    def _draw(self, area, ctx, w, h):
        self.scale = self._fit_scale(w, h) if self.zoom is None else self.zoom
        self.zoom_lbl.set_label(f"{round(self.scale * 100)} %")
        s = self.scale
        self.ox = (w - self.img_w * s) / 2
        self.oy = (h - self.img_h * s) / 2
        ctx.set_source_rgb(0.13, 0.13, 0.13)
        ctx.paint()
        ctx.translate(self.ox, self.oy)
        ctx.scale(s, s)
        ctx.set_source_surface(self.base, 0, 0)
        ctx.paint()
        for shp in self.shapes + ([self.current] if self.current else []):
            draw_shape(ctx, shp, self._composite_for(shp))

    def _composite_for(self, shp):
        """Pour la pixellisation : l'image avec les formes précédentes."""
        if shp.tool != "pixelate":
            return self.base
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.img_w, self.img_h)
        c = cairo.Context(surf)
        c.set_source_surface(self.base, 0, 0)
        c.paint()
        for s in self.shapes:
            if s is shp:
                break
            draw_shape(c, s, self.base)
        return surf

    def _to_img(self, x, y):
        return (x - self.ox) / self.scale, (y - self.oy) / self.scale

    def _new_shape(self):
        c = self.color.get_rgba()
        desc = self.font.get_font_desc()
        return Shape(self.tool, (c.red, c.green, c.blue), self.opacity.get_value(), self.width.get_value(),
                     self.fill.get_active(), font=desc.to_string() if desc else "Sans Bold 28")

    def _begin(self, g, x, y):
        self._start = (x, y)
        p = self._to_img(x, y)
        if self.tool == "text":
            self._ask_text(p)
            return
        self.current = self._new_shape()
        self.current.points = [p, p]

    def _update(self, g, dx, dy):
        if not self.current:
            return
        p = self._to_img(self._start[0] + dx, self._start[1] + dy)
        if self.current.tool == "pen":
            self.current.points.append(p)
        else:
            self.current.points[-1] = p
        self.canvas.queue_draw()

    def _end(self, g, dx, dy):
        s = self.current
        self.current = None
        if s is None:
            return
        (x1, y1), (x2, y2) = s.points[0], s.points[-1]
        if s.tool == "pen" and len(s.points) > 2 or abs(x2 - x1) > 2 or abs(y2 - y1) > 2:
            self.shapes.append(s)
            self.redo.clear()
            self.dirty = True
        self.canvas.queue_draw()

    def _ask_text(self, p):
        row = Adw.EntryRow(title=tr("Texte"))
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(row)
        d = Adw.AlertDialog.new(tr("Ajouter un texte"), None)
        d.add_response("cancel", tr("Annuler"))
        d.add_response("ok", tr("Ajouter"))
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        d.set_extra_child(box)

        def add(r):
            if r == "ok" and row.get_text().strip():
                s = self._new_shape()
                s.text = row.get_text()
                s.points = [p]
                self.shapes.append(s)
                self.redo.clear()
                self.dirty = True
                self.canvas.queue_draw()
        d.connect("response", lambda _d, r: add(r))
        row.connect("entry-activated", lambda *_: (d.close(), add("ok")))
        d.present(self)
        GLib.idle_add(row.grab_focus)

    def undo(self):
        if self.shapes:
            self.redo.append(self.shapes.pop())
            self.dirty = True
            self.canvas.queue_draw()

    def redo_one(self):
        if self.redo:
            self.shapes.append(self.redo.pop())
            self.dirty = True
            self.canvas.queue_draw()

    # -- enregistrement --------------------------------------------------------
    def render(self) -> cairo.ImageSurface:
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.img_w, self.img_h)
        ctx = cairo.Context(out)
        ctx.set_source_surface(self.base, 0, 0)
        ctx.paint()
        for s in self.shapes:
            draw_shape(ctx, s, self._composite_for(s))
        out.flush()
        return out

    def save(self, path: str):
        surf = self.render()
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".png":
                surf.write_to_png(path)
            else:
                pb = Gdk.pixbuf_get_from_surface(surf, 0, 0, self.img_w, self.img_h)
                fmt = {".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".bmp": "bmp", ".tif": "tiff",
                       ".tiff": "tiff"}.get(ext)
                if fmt is None:
                    self.save_as()
                    return
                if fmt == "jpeg":
                    pb.savev(path, "jpeg", ["quality"], ["92"])
                else:
                    pb.savev(path, fmt, [], [])
        except (GLib.Error, OSError) as e:
            alert(self, tr("Enregistrement impossible"), getattr(e, "message", None) or str(e))
            return
        self.dirty = False
        toast(self, tr("Image enregistrée : {0}").format(os.path.basename(path)))

    def save_as(self):
        base = os.path.splitext(self.path)[0]
        choose_save(self, tr("Enregistrer l'image"), base + tr("-annoté") + ".png", self.save)

    def _on_close(self, *_):
        if not self.dirty:
            return False

        def resp(r):
            if r == "save":
                self.save(self.path)
                if not self.dirty:
                    self.destroy()
            elif r == "discard":
                self.dirty = False
                self.destroy()
        alert(self, tr("Enregistrer les annotations ?"), tr("Les modifications seront perdues sinon."),
              responses=(("cancel", tr("Annuler")), ("discard", tr("Ne pas enregistrer")), ("save", tr("Enregistrer"))),
              destructive="discard", suggested="save", default="save", callback=resp)
        return True


_ = Gio
