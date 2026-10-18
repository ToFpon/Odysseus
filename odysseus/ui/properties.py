"""Fenêtre Propriétés (Ctrl+I) : informations, taille calculée en arrière-plan, droits, application."""

from __future__ import annotations

import os
import stat as st
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk

from ..backend import scanner, tools
from ..backend.engine import Job, human_size
from ..i18n import ntr, tr
from . import icons
from .common import DialogWindow, combo_row, copy_to_clipboard, full_date, toast

ACCESS = [tr("Aucun"), tr("Lecture seule"), tr("Lecture et écriture")]


class PropertiesDialog(DialogWindow):
    def __init__(self, window, paths: list[str]):
        super().__init__(title=tr("Propriétés"), content_width=480, content_height=640)
        self.window = window
        self.paths = paths
        self._cancel = threading.Event()
        self.connect("closed", lambda *_: self._cancel.set())
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        tv.set_content(page)
        self.set_child(tv)
        recs = [scanner.rec_for_path(p) for p in paths]
        recs = [r for r in recs if r is not None]
        if not recs:
            page.add(Adw.PreferencesGroup(title=tr("Élément introuvable")))
            return
        self.recs = recs
        one = recs[0] if len(recs) == 1 else None

        hero_g = Adw.PreferencesGroup()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["info-hero"])
        img = Gtk.Image(pixel_size=72)
        img.set_from_gicon(icons.gicon_for(one) if one else Gio.ThemedIcon.new("edit-select-all-symbolic"))
        hero.append(img)
        title = one.name if one else ntr("{0} élément", "{0} éléments", len(recs)).format(len(recs))
        hero.append(Gtk.Label(label=title, css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER,
                              selectable=True))
        if one:
            hero.append(Gtk.Label(label=tr("Dossier") if one.is_dir else scanner.type_description(one.content_type),
                                  css_classes=["dim-label"]))
        hero_g.add(hero)
        page.add(hero_g)

        g = Adw.PreferencesGroup(title=tr("Général"))
        self.size_row = Adw.ActionRow(title=tr("Taille"), subtitle=tr("Calcul…"), subtitle_selectable=True)
        g.add(self.size_row)
        parent = os.path.dirname(paths[0])
        loc = Adw.ActionRow(title=tr("Emplacement"), subtitle=parent, subtitle_selectable=True)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text=tr("Ouvrir l'emplacement"))
        btn.connect("clicked", lambda *_: (self.close(), window.open_location(parent)))
        loc.add_suffix(btn)
        g.add(loc)
        if one:
            if one.is_link:
                try:
                    g.add(Adw.ActionRow(title=tr("Lien vers"), subtitle=os.readlink(one.path), subtitle_selectable=True))
                except OSError:
                    pass
            try:
                s = os.stat(one.path)
                g.add(Adw.ActionRow(title=tr("Modifié"), subtitle=full_date(s.st_mtime)))
                g.add(Adw.ActionRow(title=tr("Dernier accès"), subtitle=full_date(s.st_atime)))
                g.add(Adw.ActionRow(title=tr("Métadonnées modifiées"), subtitle=full_date(s.st_ctime)))
            except OSError:
                pass
            if not one.is_dir:
                g.add(Adw.ActionRow(title=tr("Type MIME"), subtitle=one.content_type, subtitle_selectable=True))
        try:
            v = os.statvfs(paths[0])
            g.add(Adw.ActionRow(title=tr("Espace libre"), subtitle=tr("{0} libres sur {1}").format(
                human_size(v.f_bavail * v.f_frsize), human_size(v.f_blocks * v.f_frsize))))
        except OSError:
            pass
        page.add(g)

        if one and not one.is_dir:
            self._open_with(page, one)
        if one:
            self._permissions(page, one)
            if not one.is_dir:
                self._checksums(page, one)
        self._compute_size()

    # ------------------------------------------------------------------
    def _compute_size(self):
        paths = list(self.paths)
        cancel = self._cancel

        def work():
            total = files = dirs = 0
            last = time.monotonic()
            stack = list(paths)
            while stack and not cancel.is_set():
                p = stack.pop()
                try:
                    s = os.lstat(p)
                except OSError:
                    continue
                if st.S_ISDIR(s.st_mode):
                    if p not in paths:
                        dirs += 1
                    try:
                        with os.scandir(p) as it:
                            stack.extend(e.path for e in it)
                    except OSError:
                        pass
                else:
                    files += 1
                    total += s.st_size
                now = time.monotonic()
                if now - last > 0.3:
                    last = now
                    GLib.idle_add(self._set_size, total, files, dirs, False)
            GLib.idle_add(self._set_size, total, files, dirs, True)
        threading.Thread(target=work, daemon=True).start()

    def _set_size(self, total, files, dirs, final):
        text = f"{human_size(total)} ({total:,} ".replace(",", " ") + tr("octets") + ")"
        if dirs or files > 1:
            text += "\n" + ntr("{0} fichier", "{0} fichiers", files).format(files)
            if dirs:
                text += ", " + ntr("{0} dossier", "{0} dossiers", dirs).format(dirs)
        if not final:
            text += " …"
        self.size_row.set_subtitle(text)
        return False

    def _open_with(self, page, rec):
        g = Adw.PreferencesGroup(title=tr("Ouvrir avec"))
        app = Gio.AppInfo.get_default_for_type(rec.content_type, False)
        row = Adw.ActionRow(title=app.get_display_name() if app else tr("Aucune application"),
                            subtitle=tr("Application par défaut pour ce type"))
        if app and app.get_icon():
            row.add_prefix(Gtk.Image(gicon=app.get_icon(), pixel_size=32))
        btn = Gtk.Button(label=tr("Changer…"), valign=Gtk.Align.CENTER)

        def change(*_):
            from .dialogs import AppChooser
            d = AppChooser(self.window, rec.content_type,
                           [rec.path] if rec.path else [Gio.File.new_for_uri(rec.uri)])
            d.default_row.set_active(True)
            d.present(self)
        btn.connect("clicked", change)
        row.add_suffix(btn)
        g.add(row)
        page.add(g)

    def _permissions(self, page, rec):
        try:
            s = os.lstat(rec.path)
        except OSError:
            return
        g = Adw.PreferencesGroup(title=tr("Droits"))
        mine = s.st_uid == os.getuid()
        if not mine:
            g.set_description(tr("Vous n'êtes pas le propriétaire : les droits ne peuvent pas être modifiés."))
        g.add(Adw.ActionRow(title=tr("Propriétaire"), subtitle=scanner.owner_name(s.st_uid)))
        try:
            import grp
            gname = grp.getgrgid(s.st_gid).gr_name
        except (KeyError, ImportError):
            gname = str(s.st_gid)
        g.add(Adw.ActionRow(title=tr("Groupe"), subtitle=gname))
        mode = s.st_mode
        self._mode_path = rec.path
        self._is_dir = rec.is_dir
        rows = []
        for label, shift in ((tr("Accès du propriétaire"), 6), (tr("Accès du groupe"), 3), (tr("Accès des autres"), 0)):
            bits = (mode >> shift) & 7
            idx = 2 if bits & 2 else (1 if bits & 4 else 0)
            row = combo_row(label, ACCESS, idx)
            row.set_sensitive(mine)
            row._shift = shift
            row.connect("notify::selected", lambda *_: self._apply_mode())
            rows.append(row)
            g.add(row)
        self._access_rows = rows
        if not rec.is_dir:
            self.exec_row = Adw.SwitchRow(title=tr("Autoriser l'exécution comme programme"),
                                          active=bool(mode & 0o100), sensitive=mine)
            self.exec_row.connect("notify::active", lambda *_: self._apply_mode())
            g.add(self.exec_row)
        else:
            self.exec_row = None
        page.add(g)

    def _apply_mode(self):
        try:
            mode = st.S_IMODE(os.lstat(self._mode_path).st_mode)
        except OSError:
            return
        mode &= ~0o777
        for row in self._access_rows:
            sel = row.get_selected()
            bits = 0
            if sel >= 1:
                bits |= 4
            if sel == 2:
                bits |= 2
            if self._is_dir and sel >= 1:
                bits |= 1
            elif not self._is_dir and self.exec_row is not None and self.exec_row.get_active() and sel >= 1:
                bits |= 1
            mode |= bits << row._shift
        try:
            os.chmod(self._mode_path, mode)
        except OSError as e:
            toast(self, e.strerror or str(e))

    def _checksums(self, page, rec):
        g = Adw.PreferencesGroup(title=tr("Sommes de contrôle"))
        row = Adw.ActionRow(title="MD5 · SHA-256", subtitle=tr("Non calculées"), subtitle_selectable=True)
        btn = Gtk.Button(label=tr("Calculer"), valign=Gtk.Align.CENTER)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                          visible=False, tooltip_text=tr("Copier"))
        copy.connect("clicked", lambda *_: (copy_to_clipboard(self, row.get_subtitle()), toast(self, tr("Copié"))))

        def run(*_):
            btn.set_sensitive(False)
            row.set_subtitle(tr("Calcul…"))

            def work():
                job = Job("hash", lambda j: tools.hash_files(j, [rec.path], ["MD5", "SHA-256"])).run_sync()
                res = job.result[0] if job.state == "done" and job.result else None
                text = "\n".join(f"{k} : {res[k]}" for k in ("MD5", "SHA-256")) if res else job.error
                GLib.idle_add(lambda: (row.set_subtitle(text), copy.set_visible(True), False)[2])
            threading.Thread(target=work, daemon=True).start()
        btn.connect("clicked", run)
        row.add_suffix(copy)
        row.add_suffix(btn)
        g.add(row)
        page.add(g)
