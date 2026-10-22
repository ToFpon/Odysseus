"""Fenêtre Propriétés (Ctrl+I) : informations, taille calculée en arrière-plan, droits, application."""

from __future__ import annotations

import os
import stat as st
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .. import features
from ..backend import scanner, tools
from ..backend.engine import Job, human_size
from ..i18n import ntr, tr
from . import icons
from .common import DialogWindow, combo_row, copy_to_clipboard, full_date, shown, toast

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
        hero.append(Gtk.Label(label=shown(title), css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER,
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
        loc = Adw.ActionRow(title=tr("Emplacement"), subtitle=shown(parent), subtitle_selectable=True)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text=tr("Ouvrir l'emplacement"))
        btn.connect("clicked", lambda *_: (self.close(), window.open_location(parent)))
        loc.add_suffix(btn)
        g.add(loc)
        if one:
            if one.is_link:
                try:
                    g.add(Adw.ActionRow(title=tr("Lien vers"), subtitle=shown(os.readlink(one.path)), subtitle_selectable=True))
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
        link = st.S_ISLNK(s.st_mode)
        if link:
            # chmod suit le lien et changerait la cible, alors que les droits lus sont ceux du lien (777)
            g.set_description(tr("C'est un lien : ses droits sont ceux de sa cible ; ouvrez les propriétés de "
                                 "la cible pour les modifier."))
            mine = False
        elif not mine:
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
            row.connect("notify::selected", lambda r, _p: self._apply_mode(changed=r))
            rows.append(row)
            g.add(row)
        self._access_rows = rows
        if not rec.is_dir:
            self.exec_row = Adw.SwitchRow(title=tr("Autoriser l'exécution comme programme"),
                                          active=bool(mode & 0o100), sensitive=mine)
            self.exec_row.connect("notify::active", lambda *_: self._apply_mode(exec_changed=True))
            g.add(self.exec_row)
        else:
            self.exec_row = None
        page.add(g)

    def _apply_mode(self, changed=None, exec_changed: bool = False):
        """Ne touche que la catégorie modifiée (ou le bit d'exécution) : les autres gardent leurs bits
        exacts, même ceux que la liste ne sait pas montrer (« écriture seule », « -wx »). Avant, tout
        était recalculé depuis les listes : dossier 0754, propriétaire → lecture donnait 0555."""
        try:
            s = os.lstat(self._mode_path)
        except OSError:
            return
        if st.S_ISLNK(s.st_mode):
            return                      # jamais de chmod à travers un lien
        old = st.S_IMODE(s.st_mode)
        mode = old
        for row in self._access_rows:
            shift = row._shift
            if row is changed:
                sel = row.get_selected()
                bits = (4 if sel >= 1 else 0) | (2 if sel == 2 else 0)
                if sel >= 1:
                    # dossier : lecture = lecture + traverser ; fichier : le bit d'exécution reste
                    # celui de la catégorie, seul l'interrupteur « Autoriser l'exécution » le change
                    bits |= 1 if self._is_dir else (old >> shift) & 1
                mode = (mode & ~(7 << shift)) | (bits << shift)
            elif exec_changed and self.exec_row is not None and (old >> shift) & 4:
                x = 1 << shift
                mode = (mode | x) if self.exec_row.get_active() else (mode & ~x)
        if mode == old:
            return
        try:
            os.chmod(self._mode_path, mode)
        except OSError as e:
            toast(self, e.strerror or str(e))

    def _checksums(self, page, rec):
        if not features.enabled("checksums"):
            return
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
        self._compare_row(g, rec)
        page.add(g)

    def _compare_row(self, group, rec):
        """Comparer à l'empreinte publiée (site de téléchargement…) : l'algorithme se déduit de la
        longueur, le fichier n'est lu qu'une fois même si plusieurs algorithmes sont possibles."""
        entry = Adw.EntryRow(title=tr("Comparer avec l'empreinte publiée"))
        # notre propre bouton (celui d'Adw.EntryRow ne se colore pas) : le verdict se lit dessus,
        # sans faire défiler jusqu'au détail — gris pendant le calcul, vert / rouge / orange ensuite
        # focus_on_click=False : le champ garde le focus (sa sortie de focus réveille la méthode de
        # saisie — IBus sous Zorin —, qui pouvait émettre « changed » et effacer le verdict)
        button = Gtk.Button(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER, sensitive=False,
                            css_classes=["circular", "suggested-action", "compare-button"],
                            tooltip_text=tr("Comparer"), focus_on_click=False)
        entry.add_suffix(button)
        self.compare_button = button
        result = Adw.ActionRow(visible=False, subtitle_selectable=True)
        icon = Gtk.Image()
        result.add_prefix(icon)
        bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True, visible=False)
        result.add_suffix(bar)
        self.compare_entry, self.compare_result = entry, result
        state = {"job": None, "text": None}

        verdicts = {None: ("object-select-symbolic", "suggested-action"), "busy": ("content-loading-symbolic", "busy"),
                    True: ("object-select-symbolic", "ok"), False: ("window-close-symbolic", "ko"),
                    "unknown": ("dialog-question-symbolic", "unknown")}

        def paint(verdict):
            icon_name, cls = verdicts[verdict]
            for c in ("suggested-action", "busy", "ok", "ko", "unknown"):
                button.remove_css_class(c)
            button.add_css_class(cls)
            button.set_icon_name(icon_name)
            button.set_sensitive(verdict != "busy" and bool(entry.get_text().strip()))

        def show(title, subtitle="", ok=None):
            result.set_visible(True)
            result.set_title(title)
            result.set_subtitle(subtitle)
            for c in ("success", "error"):
                result.remove_css_class(c)
            icon.set_visible(ok is not None)
            if ok is not None:
                icon.set_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
                result.add_css_class("success" if ok else "error")
            self.compare_ok = ok
            if ok is not None:
                paint(ok)

        def apply(*_):
            expected = tools.expected_digest(entry.get_text())
            if expected is None:
                show(tr("Empreinte non reconnue"),
                     tr("Collez l'empreinte publiée (MD5, SHA-1, SHA-256, SHA-512…), seule ou avec le nom du fichier"))
                paint("unknown")
                return
            if state["job"] is not None:
                state["job"].cancel()
            state["text"] = entry.get_text()
            show(tr("Calcul…"))
            paint("busy")
            bar.set_visible(True)
            bar.set_fraction(0)
            job = Job("hash", lambda j: tools.compare_digest(j, rec.path, expected))
            state["job"] = job
            job.subscribe(lambda j: GLib.idle_add(lambda: (bar.set_fraction(j.fraction), False)[1]))

            def work():
                job.run_sync()

                def done():
                    if state["job"] is not job:
                        return False
                    bar.set_visible(False)
                    if job.state != "done":
                        if job.state != "cancelled":
                            show(tr("Calcul impossible"), job.error or "", False)
                        else:
                            paint(None)
                        return False
                    match, res = job.result
                    if match:
                        show(tr("Correspond ({0})").format(match), expected, True)
                    else:
                        algo = [k for k in res if k not in ("path", "size")][0]     # le plus courant
                        show(tr("Ne correspond pas : le fichier est différent ou abîmé"),
                             tr("{0} calculé : {1}").format(algo, res[algo]), False)
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()
        def edited(*_):
            # texte modifié : le verdict précédent ne vaut plus, un calcul en cours non plus. Un
            # « changed » sans vrai changement (méthode de saisie, focus) ne touche à rien.
            if entry.get_text() == state["text"]:
                return
            state["text"] = None
            if state["job"] is not None:
                state["job"].cancel()
                state["job"] = None
                bar.set_visible(False)
            result.set_visible(False)
            self.compare_ok = None
            paint(None)
        button.connect("clicked", apply)
        entry.connect("entry-activated", apply)
        entry.connect("changed", edited)
        self.connect("closed", lambda *_: state["job"] and state["job"].cancel())
        group.add(entry)
        group.add(result)
