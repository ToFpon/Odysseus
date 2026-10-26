"""Outils image (reprise de nautilus-image-converter, plus la conversion) : redimensionner, pivoter,
convertir — combinables en une passe (ex. : photos HEIC de téléphone → JPEG de 1920 px)."""

from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from ..backend import images
from ..i18n import ntr, tr
from ..settings import settings
from .common import choose_folder, combo_row, dialog_shell

ROTATIONS = [(90, lambda: tr("90° à droite")), (270, lambda: tr("90° à gauche")), (180, lambda: tr("180°")),
             (0, lambda: tr("Angle libre"))]


_last = None          # pour les tests


class ImageToolsDialog:
    def __init__(self, window, paths: list[str], mode: str):
        global _last
        _last = self
        self.window = window
        self.paths = paths
        title = {"resize": tr("Redimensionner les images"), "rotate": tr("Pivoter les images"),
                 "convert": tr("Convertir les images")}[mode]
        self.dialog, page, self.action = dialog_shell(title, tr("Enregistrer"), 580, 700)

        ops = Adw.PreferencesGroup(title=tr("Modifications"),
                                   description=tr("Combinables : par exemple redimensionner et convertir en une fois"))
        # --- redimensionner
        self.resize = Adw.ExpanderRow(title=tr("Redimensionner"), show_enable_switch=True,
                                      enable_expansion=mode == "resize", expanded=mode == "resize")
        self.resize_how = combo_row(tr("Méthode"), [tr("À l'échelle"), tr("Taille maximale")],
                                    1 if settings["image_resize"] == "fit" else 0)
        self.resize.add_row(self.resize_how)
        self.scale = Adw.SpinRow.new_with_range(1, 400, 5)
        self.scale.set_title(tr("Échelle (%)"))
        self.scale.set_value(settings["image_scale"])
        self.resize.add_row(self.scale)
        self.width = Adw.SpinRow.new_with_range(1, 20000, 10)
        self.width.set_title(tr("Largeur maximale (px)"))
        self.width.set_value(settings["image_width"])
        self.height = Adw.SpinRow.new_with_range(1, 20000, 10)
        self.height.set_title(tr("Hauteur maximale (px)"))
        self.height.set_value(settings["image_height"])
        self.no_enlarge = Adw.SwitchRow(title=tr("Ne pas agrandir les petites images"),
                                        active=settings["image_no_enlarge"])
        for r in (self.width, self.height, self.no_enlarge):
            self.resize.add_row(r)
        ops.add(self.resize)
        # --- pivoter
        self.rotate = Adw.ExpanderRow(title=tr("Pivoter"), show_enable_switch=True,
                                      enable_expansion=mode == "rotate", expanded=mode == "rotate")
        angles = [a for a, _l in ROTATIONS]
        cur = settings["image_rotate"]
        self.rotate_how = combo_row(tr("Rotation"), [label() for _a, label in ROTATIONS],
                                    angles.index(cur) if cur in angles else len(angles) - 1)
        self.rotate.add_row(self.rotate_how)
        self.angle = Adw.SpinRow.new_with_range(-359, 359, 1)
        self.angle.set_title(tr("Angle (degrés, sens horaire)"))
        self.angle.set_value(cur if cur not in angles else 15)
        self.rotate.add_row(self.angle)
        ops.add(self.rotate)
        # --- convertir
        self.convert = Adw.ExpanderRow(title=tr("Convertir le format"), show_enable_switch=True,
                                       enable_expansion=mode == "convert", expanded=mode == "convert")
        fmts = [f for f, _l in images.OUTPUT_FORMATS]
        self.fmt = combo_row(tr("Format"), [label for _f, label in images.OUTPUT_FORMATS],
                             fmts.index(settings["image_format"]) if settings["image_format"] in fmts else 0)
        self.convert.add_row(self.fmt)
        ops.add(self.convert)
        page.add(ops)

        out = Adw.PreferencesGroup(title=tr("Enregistrement"))
        self.quality = Adw.SpinRow.new_with_range(1, 100, 1)
        self.quality.set_title(tr("Qualité JPEG / WebP"))
        self.quality.set_value(settings["image_quality"])
        out.add(self.quality)
        self.replace = Adw.SwitchRow(title=tr("Remplacer les originaux"), active=settings["image_replace"])
        out.add(self.replace)
        self.suffix = Adw.EntryRow(title=tr("Ajouter au nom des nouvelles images"))
        self.suffix.set_text({"resize": tr(" (redimensionnée)"), "rotate": tr(" (pivotée)"), "convert": ""}[mode])
        out.add(self.suffix)
        self.dest = ""
        self.dest_row = Adw.ActionRow(title=tr("Dossier de destination"), subtitle=tr("À côté de chaque image"))
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b.connect("clicked", lambda *_: choose_folder(self.dialog, tr("Dossier de destination"),
                                                      os.path.dirname(paths[0]), self._set_dest))
        self.dest_row.add_suffix(b)
        out.add(self.dest_row)
        self.note = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"], margin_top=8)
        out.add(self.note)
        page.add(out)

        self.files = Adw.PreferencesGroup(title=ntr("{0} image", "{0} images", len(paths)).format(len(paths)))
        self.status = []
        for p in paths:
            r = Adw.ActionRow(title=GLib.markup_escape_text(os.path.basename(p)))
            r.set_title_lines(1)
            r.add_prefix(Gtk.Image(icon_name="image-x-generic-symbolic"))
            st = Gtk.Image(icon_name="content-loading-symbolic", css_classes=["dim-label"], visible=False)
            r.add_suffix(st)
            self.status.append(st)
            self.files.add(r)
        page.add(self.files)

        for w, sig in ((self.resize, "notify::enable-expansion"), (self.rotate, "notify::enable-expansion"),
                       (self.convert, "notify::enable-expansion"), (self.resize_how, "notify::selected"),
                       (self.rotate_how, "notify::selected"), (self.fmt, "notify::selected"),
                       (self.replace, "notify::active")):
            w.connect(sig, lambda *_: self._sync())
        self.action.connect("clicked", self._go)
        self._sync()

    def _set_dest(self, path):
        self.dest = path
        self.dest_row.set_subtitle(path)

    def options(self) -> images.Options:
        fit = self.resize_how.get_selected() == 1
        angle, _l = ROTATIONS[self.rotate_how.get_selected()]
        return images.Options(
            resize=("fit" if fit else "scale") if self.resize.get_enable_expansion() else "",
            scale=int(self.scale.get_value()), width=int(self.width.get_value()),
            height=int(self.height.get_value()), no_enlarge=self.no_enlarge.get_active(),
            rotate=(angle or int(self.angle.get_value())) if self.rotate.get_enable_expansion() else 0,
            fmt=images.OUTPUT_FORMATS[self.fmt.get_selected()][0] if self.convert.get_enable_expansion() else "",
            quality=int(self.quality.get_value()), replace=self.replace.get_active(),
            suffix=self.suffix.get_text(), dest_dir=self.dest)

    def _sync(self):
        fit = self.resize_how.get_selected() == 1
        self.scale.set_visible(not fit)
        for r in (self.width, self.height, self.no_enlarge):
            r.set_visible(fit)
        self.angle.set_visible(ROTATIONS[self.rotate_how.get_selected()][0] == 0)
        o = self.options()
        outs = {images.output_format(p, o) for p in self.paths}
        self.quality.set_sensitive(bool(outs & images.LOSSY))
        self.suffix.set_visible(not o.replace)
        self.dest_row.set_visible(not o.replace)
        changes = bool(o.resize or o.rotate or o.fmt)
        self.action.set_sensitive(changes)
        if o.replace and any(images.output_format(p, o) != images.source_format(p) for p in self.paths):
            self.note.set_text(tr("Les images changent de format : les originaux iront à la corbeille "
                                  "une fois les nouvelles images enregistrées."))
        elif o.replace:
            self.note.set_text(tr("Chaque original est remplacé d'un coup, une fois la nouvelle image "
                                  "complète : jamais d'image à moitié écrite."))
        else:
            example = images.output_name(self.paths[0], o)
            self.note.set_text(tr("Exemple : {0}").format(example))
        self.note.set_visible(changes)

    def _go(self, *_):
        o = self.options()
        if not (o.resize or o.rotate or o.fmt):
            return
        for key, value in {
                "image_resize": o.resize or settings["image_resize"], "image_scale": o.scale,
                "image_width": o.width, "image_height": o.height, "image_no_enlarge": o.no_enlarge,
                "image_rotate": o.rotate or settings["image_rotate"],
                "image_format": o.fmt or settings["image_format"], "image_quality": o.quality,
                "image_replace": o.replace}.items():
            settings[key] = value
        self.action.set_sensitive(False)
        status = self.status

        def mark(i, ok):
            def do():
                status[i].set_visible(True)
                status[i].set_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
                status[i].remove_css_class("dim-label")
                status[i].add_css_class("success" if ok else "error")
                return False
            GLib.idle_add(do)
        paths = list(self.paths)
        n = len(paths)
        self.window._run(ntr("Traitement de {0} image", "Traitement de {0} images", n).format(n),
                         lambda j: images.process(j, paths, o, on_file=mark),
                         undo=(None if o.replace else lambda r: ("create", list(r.new_items)) if r.new_items else None),
                         on_done=lambda _j: self.dialog.close())

    def present(self, parent):
        self.dialog.present(parent)
