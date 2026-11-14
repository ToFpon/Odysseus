"""« Modifier les étiquettes… » des fichiers audio (fonctionnalité `tag_editor`, moteur backend/tags.py).

Un ou plusieurs fichiers : un champ identique partout s'affiche, un champ qui diffère montre « Valeurs
différentes » et reste tel quel tant qu'on n'y tape rien. Seuls les champs modifiés sont écrits (un album
entier : corriger l'année sans toucher aux titres). Pochette : voir, remplacer (image choisie ou glissée),
retirer. « Numéroter dans l'ordre des noms » : pistes 1 à N, comme le renommage multiple. Lecture et
écriture dans un fil (partage réseau, gros FLAC) ; Ctrl+Z remet les anciennes étiquettes.
"""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from ..backend import scanner, tags
from ..i18n import ntr, tr
from .common import dialog_shell, shown

_last = None          # pour les tests

COVER_SIZE = 160


def field_labels():
    return [("title", tr("Titre")), ("artist", tr("Artiste")), ("album", tr("Album")),
            ("albumartist", tr("Artiste de l'album")), ("date", tr("Année")), ("genre", tr("Genre")),
            ("comment", tr("Commentaire"))]


def name_order(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda p: scanner.collate_key(os.path.basename(p)))


class TagDialog:
    def __init__(self, window, paths: list[str]):
        global _last
        _last = self
        self.window = window
        self.paths = name_order(paths)
        self.values: list[dict] = []
        self.initial: dict = {}
        self.entries: dict = {}
        self.cover = tags.KEEP                   # KEEP, None (retirée) ou (octets, type)
        self.auto_number = False
        self.from_names = False
        n = len(self.paths)
        self.dialog, page, self.action = dialog_shell(tr("Modifier les étiquettes"), tr("Enregistrer"), 560, 760)
        self.action.set_sensitive(False)
        self.action.connect("clicked", lambda *_: self._save())

        # --- en-tête : pochette et nom
        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=COVER_SIZE,
                                   height_request=COVER_SIZE, can_shrink=True, css_classes=["card"])
        self.no_cover = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=64, width_request=COVER_SIZE,
                                  height_request=COVER_SIZE, css_classes=["card", "dim-label"])
        self.cover_stack = Gtk.Stack()
        self.cover_stack.add_named(self.no_cover, "none")
        self.cover_stack.add_named(self.picture, "image")
        box.append(self.cover_stack)
        self.cover_note = Gtk.Label(css_classes=["dim-label", "caption"])
        box.append(self.cover_note)
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, css_classes=["linked"])
        self.choose_btn = Gtk.Button(label=tr("Choisir une image…"))
        self.choose_btn.connect("clicked", lambda *_: self._choose_cover())
        self.remove_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=tr("Retirer la pochette"))
        self.remove_btn.connect("clicked", lambda *_: self._set_cover(None))
        btns.append(self.choose_btn)
        btns.append(self.remove_btn)
        box.append(btns)
        title = os.path.basename(self.paths[0]) if n == 1 else ntr("{0} fichier", "{0} fichiers", n).format(n)
        box.append(Gtk.Label(label=shown(title), css_classes=["title-3"], wrap=True,
                             justify=Gtk.Justification.CENTER, max_width_chars=40))
        head.add(box)
        page.add(head)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        box.add_controller(drop)

        # --- champs
        group = Adw.PreferencesGroup(title=tr("Étiquettes"))
        for key, label in field_labels():
            group.add(self._row(key, label))
        page.add(group)
        nums = Adw.PreferencesGroup(title=tr("Numérotation"))
        nums.add(self._pair_row(tr("Piste"), "tracknumber", "tracktotal"))
        nums.add(self._pair_row(tr("Disque"), "discnumber", "disctotal"))
        names = Adw.ActionRow(title=tr("Titres et pistes d'après les noms de fichiers"),
                              subtitle=tr("« 03 - Monospace.mp3 » → piste 3, titre « Monospace »"))
        btn = Gtk.Button(label=tr("Remplir"), valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._fill_from_names())
        names.add_suffix(btn)
        nums.add(names)
        if n > 1:
            auto = Adw.ActionRow(title=tr("Numéroter dans l'ordre des noms"),
                                 subtitle=tr("Pistes 1 à {0}, dans l'ordre des noms de fichiers").format(n))
            btn = Gtk.Button(label=tr("Numéroter"), valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda *_: self._number())
            auto.add_suffix(btn)
            nums.add(auto)
        page.add(nums)
        self.status = Adw.PreferencesGroup()
        self.spinner = Gtk.Spinner(spinning=True)
        self.status.add(self.spinner)
        page.add(self.status)
        self._set_sensitive(False)
        threading.Thread(target=self._load, daemon=True).start()

    def present(self, parent):
        self.dialog.present(parent)

    # ------------------------------------------------------------------
    def _row(self, key, label):
        # Adw.EntryRow : le libellé est dans le champ (comme dans les Paramètres de GNOME), tous alignés
        row = Adw.EntryRow(title=label)
        row.connect("changed", lambda *_: self._sync())
        row._label = label
        self.entries[key] = row
        return row

    def _pair_row(self, label, num, tot):
        row = Adw.ActionRow(title=label)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for key in (num, tot):
            entry = Gtk.Entry(width_chars=5, max_width_chars=5, input_purpose=Gtk.InputPurpose.DIGITS,
                              xalign=0.5)
            entry.connect("changed", lambda *_: self._sync())
            self.entries[key] = entry
            box.append(entry)
            if key == num:
                box.append(Gtk.Label(label=tr("sur"), css_classes=["dim-label"]))
        row.add_suffix(box)
        return row

    def _set_sensitive(self, on: bool):
        for e in self.entries.values():
            e.set_sensitive(on)
        self.choose_btn.set_sensitive(on)
        self.remove_btn.set_sensitive(on)

    # ------------------------------------------------------------------
    def _load(self):
        values, errors = [], []
        for p in self.paths:
            try:
                values.append(tags.read(p))
            except Exception as e:  # noqa: BLE001 — fichier abîmé, format inattendu : signalé
                errors.append((p, str(e)))
                values.append({})
        GLib.idle_add(self._loaded, values, errors)

    def _loaded(self, values, errors):
        self.values = values
        self.status.remove(self.spinner)
        if errors:
            self.status.set_description(tr("Illisible : {0}").format(
                ", ".join(shown(os.path.basename(p)) for p, _e in errors[:3])))
        for key, entry in self.entries.items():
            common = tags.merged(values, key)
            self.initial[key] = common or ""
            entry.set_text(common or "")
            if isinstance(entry, Adw.EntryRow):
                entry.set_title(entry._label if common is not None
                                else tr("{0} (valeurs différentes)").format(entry._label))
            else:
                entry.set_placeholder_text("" if common is not None else tr("Valeurs différentes"))
        covers = {v.get("cover", (None, None))[0] for v in values}
        if len(covers) == 1:
            data = covers.pop()
            self._show_cover(data)
            self.cover_note.set_label("")
        else:
            self._show_cover(None)
            self.cover_note.set_label(tr("Pochettes différentes"))
        self._set_sensitive(True)
        self._sync()
        return False

    def _show_cover(self, data):
        if data:
            try:
                self.picture.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                self.cover_stack.set_visible_child_name("image")
                return
            except GLib.Error:
                pass
        self.picture.set_paintable(None)
        self.cover_stack.set_visible_child_name("none")

    # ------------------------------------------------------------------
    def changes(self) -> dict:
        """Champs modifiés : {champ: texte} (texte vide = effacer)."""
        skip = ({"tracknumber"} if self.auto_number else set()) | ({"title", "tracknumber"} if self.from_names else set())
        return {k: e.get_text().strip() for k, e in self.entries.items()
                if k not in skip and e.get_text().strip() != self.initial.get(k, "")}

    def _sync(self):
        dirty = bool(self.changes()) or self.cover is not tags.KEEP or self.auto_number or self.from_names
        self.action.set_sensitive(dirty and bool(self.values))

    def _number(self):
        self.auto_number = True
        n = len(self.paths)
        e = self.entries["tracknumber"]
        e.set_text("")
        e.set_placeholder_text(tr("1 à {0}").format(n))
        e.set_sensitive(False)
        self.entries["tracktotal"].set_text(str(n))
        self._sync()

    def _fill_from_names(self):
        """Titre et numéro de piste de chaque fichier d'après son nom (calculés à l'écriture)."""
        self.from_names = True
        self.auto_number = False
        t = self.entries["title"]
        t.set_text("")
        t.set_title(tr("{0} (d'après les noms de fichiers)").format(t._label))
        t.set_sensitive(False)
        e = self.entries["tracknumber"]
        e.set_text("")
        e.set_placeholder_text(tr("noms"))
        e.set_sensitive(False)
        if len(self.paths) == 1:              # un seul fichier : on montre tout de suite le résultat
            found = tags.from_filename(self.paths[0])
            t.set_text((found.get("title") or [""])[0])
            e.set_text((found.get("tracknumber") or [""])[0])
        self._sync()

    # --- pochette
    def _set_cover(self, cover):
        self.cover = cover
        self._show_cover(cover[0] if cover else None)
        self.cover_note.set_label("")
        self._sync()

    def _load_image(self, path: str):
        """Image choisie → (octets, type) en JPEG ou PNG (les lecteurs ne lisent que ces deux-là)."""
        with open(path, "rb") as f:
            data = f.read()
        mime = tags.image_mime(data)
        if data[:3] == b"\xff\xd8\xff" or mime == "image/png":
            return data, mime
        pix = GdkPixbuf.Pixbuf.new_from_file(path)          # WebP, GIF… : converti en PNG
        ok, out = pix.save_to_bufferv("png", [], [])
        if not ok:
            raise GLib.Error(tr("Image illisible"))
        return bytes(out), "image/png"

    def _use_image(self, path: str):
        try:
            self._set_cover(self._load_image(path))
        except (OSError, GLib.Error) as e:
            self.cover_note.set_label(tr("Image illisible : {0}").format(getattr(e, "message", None) or str(e)))

    def _choose_cover(self):
        dlg = Gtk.FileDialog(title=tr("Choisir une pochette"), accept_label=tr("_Sélectionner"))
        flt = Gtk.FileFilter(name=tr("Images"))
        flt.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dlg.set_filters(filters)
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.paths[0])))

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self._use_image(f.get_path())
        dlg.open(self.dialog, None, done)

    def _on_drop(self, _t, value, _x, _y):
        files = [f.get_path() for f in value.get_files() if f.get_path()]
        if files:
            self._use_image(files[0])
            return True
        return False

    # ------------------------------------------------------------------
    def _save(self):
        texts = self.changes()
        common = {k: ([v] if v else []) for k, v in texts.items()}
        cover = self.cover
        paths = list(self.paths)
        numbered = self.auto_number
        from_names = self.from_names
        self.dialog.close()
        self.window.write_tags(paths, common, cover, numbered, from_names)
