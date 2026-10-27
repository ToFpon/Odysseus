"""Fenêtre Propriétés (Ctrl+I), disposition de Nautilus 46 : icône, nom, taille et espace libre en tête ;
dossier parent ; dates ; « Permissions » sur une page à part (Adw.NavigationView), avec « Changer les
permissions des fichiers inclus… » pour un dossier."""

from __future__ import annotations

import grp
import os
import stat as st
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..backend import fileops, folder_colors, scanner
from ..backend.engine import human_size
from ..i18n import ntr, tr
from . import icons
from .common import DialogWindow, combo_row, dialog_shell, shown, toast

IMAGE_TYPES = ("image/png", "image/jpeg", "image/svg+xml", "image/gif", "image/webp", "image/bmp",
               "image/x-icon", "image/tiff")


# accès proposés (ceux de Nautilus) : (libellé, bits rwx d'une catégorie)
def folder_choices():
    return [(tr("Aucun"), 0), (tr("Lister seulement les fichiers"), 4), (tr("Accès aux fichiers"), 5),
            (tr("Création et suppression des fichiers"), 7)]


def file_choices():
    return [(tr("Aucun"), 0), (tr("Lecture seule"), 4), (tr("Lecture et écriture"), 6)]


def access_label(bits: int, is_dir: bool) -> str:
    """Libellé de Nautilus pour n'importe quelle combinaison (« Écriture seule », « Accès seul »…)."""
    r, w, x = bits & 4, bits & 2, bits & 1
    if not is_dir:
        if r:
            return tr("Lecture et écriture") if w else tr("Lecture seule")
        return tr("Écriture seule") if w else tr("Aucun")
    if r and w:
        return tr("Création et suppression des fichiers") if x else tr("Lecture et écriture, pas d'accès")
    if r:
        return tr("Accès aux fichiers") if x else tr("Lister seulement les fichiers")
    if w:
        return tr("Écriture seule") if x else tr("Écriture seule, pas d'accès")
    return tr("Accès seul") if x else tr("Aucun")


def long_date(t: float) -> str:
    """« 21 octobre 2026 à 22:27:04 », comme Nautilus."""
    d = GLib.DateTime.new_from_unix_local(int(t))
    if d is None:
        return ""
    return tr("{0} à {1}").format(d.format("%e %B %Y").strip(), d.format("%H:%M:%S"))


def info_row(title: str, value: str) -> Adw.ActionRow:
    """Petit titre au-dessus de la valeur (classe « property » de libadwaita), comme Nautilus."""
    return Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True, css_classes=["property"])


def value_row(title: str, value: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    row.add_suffix(Gtk.Label(label=value, selectable=True))
    return row


class PropertiesDialog(DialogWindow):
    def __init__(self, window, paths: list[str]):
        super().__init__(title=tr("Propriétés"), content_width=480, content_height=640)
        self.window = window
        self.paths = paths
        self._cancel = threading.Event()
        self.connect("closed", lambda *_: self._cancel.set())
        self.nav = Adw.NavigationView()
        self.toast_overlay = Adw.ToastOverlay(child=self.nav)       # « Icône retirée — Annuler »
        self.set_child(self.toast_overlay)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        page = Adw.PreferencesPage()
        tv.set_content(page)
        self.nav.add(Adw.NavigationPage(title=tr("Propriétés"), child=tv, tag="main"))
        recs = [scanner.rec_for_path(p) for p in paths]
        recs = [r for r in recs if r is not None]
        if not recs:
            page.add(Adw.PreferencesGroup(title=tr("Élément introuvable")))
            return
        self.recs = recs
        one = recs[0] if len(recs) == 1 else None
        self._one = one

        # --- en-tête : icône, nom, taille, espace libre
        hero_g = Adw.PreferencesGroup()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["info-hero"])
        img = Gtk.Image(pixel_size=72)
        self.hero_img = img
        if one and one.is_dir and one.path and one.path != "/":
            # comme Nautilus 46 : crayon pour choisir une image, croix pour revenir à l'icône du dossier
            for side in ("start", "end", "top", "bottom"):
                getattr(img, "set_margin_" + side)(12)           # boutons dans les coins, pas sur le dessin
            ov = Gtk.Overlay(halign=Gtk.Align.CENTER, child=img)
            self.icon_edit = Gtk.Button(icon_name="document-edit-symbolic", halign=Gtk.Align.END,
                                        valign=Gtk.Align.END, css_classes=["circular"],
                                        tooltip_text=tr("Définir une icône personnalisée"))
            self.icon_edit.connect("clicked", lambda *_: self._choose_icon(one))
            self.icon_reset = Gtk.Button(icon_name="edit-clear-symbolic", halign=Gtk.Align.END,
                                         valign=Gtk.Align.START, css_classes=["circular"],
                                         tooltip_text=tr("Supprimer l'icône personnalisée"))
            self.icon_reset.connect("clicked", lambda *_: self._reset_icon(one))
            ov.add_overlay(self.icon_edit)
            ov.add_overlay(self.icon_reset)
            hero.append(ov)
            self._show_icon(one)
        else:
            img.set_from_gicon(icons.gicon_for(one) if one else Gio.ThemedIcon.new("edit-select-all-symbolic"))
            hero.append(img)
        title = one.name if one else ntr("{0} élément", "{0} éléments", len(recs)).format(len(recs))
        hero.append(Gtk.Label(label=shown(title), css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER,
                              selectable=True))
        self.summary = Gtk.Label(label=tr("Calcul…"), wrap=True, justify=Gtk.Justification.CENTER)
        hero.append(self.summary)
        try:
            v = os.statvfs(paths[0])
            hero.append(Gtk.Label(label=tr("{0} libre").format(human_size(v.f_bavail * v.f_frsize)),
                                  css_classes=["caption"]))
        except OSError:
            pass
        hero_g.add(hero)
        page.add(hero_g)

        # --- emplacement
        g = Adw.PreferencesGroup()
        parent = os.path.dirname(paths[0])
        loc = info_row(tr("Dossier parent"), shown(parent.rstrip("/") + "/"))
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text=tr("Ouvrir le dossier parent"))
        btn.connect("clicked", lambda *_: (self.close(), window.open_location(parent)))
        loc.add_suffix(btn)
        g.add(loc)
        if one and one.is_link:
            try:
                target = os.readlink(one.path)
                row = info_row(tr("Cible du lien"), shown(target))
                full = os.path.normpath(os.path.join(parent, target))
                b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text=tr("Ouvrir la cible du lien"))
                b.connect("clicked", lambda *_: (self.close(), window.open_location(os.path.dirname(full))))
                row.add_suffix(b)
                g.add(row)
            except OSError:
                pass
        page.add(g)

        # --- dates
        if one:
            try:
                s = os.stat(one.path)
            except OSError:
                s = None
            if s is not None:
                g = Adw.PreferencesGroup()
                g.add(info_row(tr("Dernière modification"), long_date(s.st_mtime)))
                created = self._created(one.path)
                if created:
                    g.add(info_row(tr("Date de création"), long_date(created)))
                if not one.is_dir:
                    g.add(info_row(tr("Dernier accès"), long_date(s.st_atime)))
                page.add(g)

        if one and not one.is_dir:
            self._open_with(page, one)
        if one:
            self._permissions_row(page, one)
        self._compute_size()

    @staticmethod
    def _created(path: str) -> float:
        try:
            info = Gio.File.new_for_path(path).query_info("time::created", Gio.FileQueryInfoFlags.NONE, None)
            if info.has_attribute("time::created"):
                return float(info.get_attribute_uint64("time::created"))
        except GLib.Error:
            pass
        return 0.0

    # ------------------------------------------------------------------
    def _compute_size(self):
        paths = list(self.paths)
        cancel = self._cancel

        def work():
            total = items = 0
            unreadable = False
            last = time.monotonic()
            stack = list(paths)
            while stack and not cancel.is_set():
                p = stack.pop()
                try:
                    s = os.lstat(p)
                except OSError:
                    continue
                if p not in paths or len(paths) > 1:
                    items += 1
                if st.S_ISDIR(s.st_mode):
                    try:
                        with os.scandir(p) as it:
                            stack.extend(e.path for e in it)
                    except OSError:
                        unreadable = True
                else:
                    total += s.st_size
                now = time.monotonic()
                if now - last > 0.3:
                    last = now
                    GLib.idle_add(self._set_size, total, items, unreadable, False)
            GLib.idle_add(self._set_size, total, items, unreadable, True)
        threading.Thread(target=work, daemon=True).start()

    def _set_size(self, total, items, unreadable, final):
        """Comme Nautilus : « 43 éléments, totalisant 4,9 Mo » (dossier), « Type, 4,9 Mo » (fichier)."""
        one = self._one
        if one is not None and not one.is_dir:
            text = human_size(total)
            kind = scanner.type_description(one.content_type)
            if kind:
                text = tr("{0}, {1}").format(kind, text)
        elif items == 0 and unreadable:
            text = tr("Contenus illisibles")
        elif items == 0 and final:
            text = tr("Dossier vide")
        else:
            text = ntr("{0} élément, de taille {1}", "{0} éléments, totalisant {1}", items).format(
                f"{items:,}".replace(",", " "), human_size(total))
            if unreadable:
                text += " " + tr("(certains contenus illisibles)")
        if not final:
            text += " …"
        self.summary.set_label(text)
        return False

    def _open_with(self, page, rec):
        g = Adw.PreferencesGroup()
        app = Gio.AppInfo.get_default_for_type(rec.content_type, False)
        row = Adw.ActionRow(title=tr("Ouvrir avec"),
                            subtitle=app.get_display_name() if app else tr("Aucune application"),
                            css_classes=["property"])
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

    # ------------------------------------------------------------------
    # Permissions : ligne de résumé, réglages sur une page à part
    def _permissions_row(self, page, rec):
        try:
            s = os.lstat(rec.path)
        except OSError:
            return
        g = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=tr("Permissions"), activatable=True)
        self.perm_summary = Gtk.Label(css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END, xalign=1)
        row.add_suffix(self.perm_summary)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self.nav.push_by_tag("permissions"))
        g.add(row)
        page.add(g)
        self._mode_path = rec.path
        self._is_dir = rec.is_dir
        self.nav.add(self._permissions_page(rec, s))
        self._sync_summary()

    def _sync_summary(self):
        try:
            mode = os.lstat(self._mode_path).st_mode
        except OSError:
            return
        self.perm_summary.set_label(access_label((mode >> 6) & 7, self._is_dir))

    def _permissions_page(self, rec, s) -> Adw.NavigationPage:
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        tv.set_content(page)
        mine = s.st_uid == os.getuid()
        link = st.S_ISLNK(s.st_mode)
        if link:
            # chmod suit le lien et changerait la cible, alors que les droits lus sont ceux du lien (777)
            page.add(Adw.PreferencesGroup(description=tr(
                "C'est un lien : ses droits sont ceux de sa cible ; ouvrez les propriétés de la cible pour les "
                "modifier.")))
            mine = False
        elif not mine:
            page.add(Adw.PreferencesGroup(description=tr(
                "Vous n'êtes pas le propriétaire de cet élément, vous ne pouvez donc pas en changer les "
                "permissions.")))
        mode = s.st_mode
        choices = folder_choices() if rec.is_dir else file_choices()
        self._access_rows = []

        def access_row(shift):
            bits = (mode >> shift) & 7
            key = bits if rec.is_dir else bits & 6
            opts = list(choices)
            idx = next((i for i, (_l, b) in enumerate(opts) if b == key), -1)
            if idx < 0:                                   # « -wx », « écriture seule »… : montré tel quel
                opts.append((access_label(bits, rec.is_dir), key))
                idx = len(opts) - 1
            row = combo_row(tr("Accès"), [label for label, _b in opts], idx)
            row._shift, row._opts = shift, opts
            row.set_sensitive(mine)
            row.connect("notify::selected", lambda r, _p: self._apply_mode(changed=r))
            self._access_rows.append(row)
            return row

        owner = Adw.PreferencesGroup()
        name = scanner.owner_name(s.st_uid)
        owner.add(value_row(tr("Propriétaire"), tr("{0} (vous)").format(name) if s.st_uid == os.getuid() else name))
        owner.add(access_row(6))
        page.add(owner)

        group = Adw.PreferencesGroup()
        gids = sorted(set(os.getgroups()) | {os.getgid(), s.st_gid})
        names = []
        for gid in gids:
            try:
                names.append(grp.getgrgid(gid).gr_name)
            except KeyError:
                names.append(str(gid))
        if mine and len(gids) > 1:
            # le propriétaire peut donner l'élément à un de ses groupes (chown sans root)
            self.group_row = combo_row(tr("Groupe"), names, gids.index(s.st_gid))
            self.group_row._gids = gids
            self.group_row.connect("notify::selected", lambda r, _p: self._apply_group(r))
            group.add(self.group_row)
        else:
            self.group_row = None
            group.add(value_row(tr("Groupe"), names[gids.index(s.st_gid)]))
        group.add(access_row(3))
        page.add(group)

        others = Adw.PreferencesGroup()
        others.add(Adw.ActionRow(title=tr("Autres"), sensitive=False))
        others.add(access_row(0))
        page.add(others)

        self.exec_row = None
        if not rec.is_dir:
            g = Adw.PreferencesGroup()
            self.exec_row = Adw.SwitchRow(title=tr("Exécutable comme programme"), active=bool(mode & 0o100),
                                          sensitive=mine)
            self.exec_row.connect("notify::active", lambda *_: self._apply_mode(exec_changed=True))
            g.add(self.exec_row)
            page.add(g)
        elif mine:
            g = Adw.PreferencesGroup()
            self.enclosed_button = Gtk.Button(label=tr("Changer les permissions des fichiers _inclus…"),
                                              use_underline=True, halign=Gtk.Align.START)
            self.enclosed_button.connect("clicked", lambda *_: self._enclosed(rec.path))
            g.add(self.enclosed_button)
            page.add(g)
        return Adw.NavigationPage(title=tr("Définir des permissions personnalisées"), child=tv, tag="permissions")

    def _apply_group(self, row):
        gid = row._gids[row.get_selected()]
        try:
            if st.S_ISLNK(os.lstat(self._mode_path).st_mode):
                return                  # jamais à travers un lien
            os.chown(self._mode_path, -1, gid)
        except OSError as e:
            toast(self, e.strerror or str(e))

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
                bits = row._opts[row.get_selected()][1]
                if not self._is_dir:
                    # fichier : le bit d'exécution reste celui de la catégorie, seul l'interrupteur
                    # « Exécutable comme programme » le change
                    bits |= (old >> shift) & 1
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
        self._sync_summary()

    # ------------------------------------------------------------------
    # « Changer les permissions des fichiers inclus » (nautilus-file-properties-change-permissions.ui)
    def _enclosed(self, folder: str):
        dialog, page, action = dialog_shell(tr("Changer les permissions des fichiers inclus"), tr("_Changer"),
                                            540, 560)
        action.set_use_underline(True)
        dialog.set_transient_for(self)
        umask = os.umask(0)                              # valeurs proposées : celles d'un élément neuf
        os.umask(umask)
        rows = {}
        for kind, title, choices, default in (("files", tr("Fichiers"), file_choices(), 0o666 & ~umask),
                                              ("dirs", tr("Dossiers"), folder_choices(), 0o777 & ~umask)):
            g = Adw.PreferencesGroup(title=title)
            for who, shift in ((tr("Propriétaire"), 6), (tr("Groupe"), 3), (tr("Autres"), 0)):
                want = (default >> shift) & (7 if kind == "dirs" else 6)
                idx = next((i for i, (_l, b) in enumerate(choices) if b == want), 0)
                r = combo_row(who, [label for label, _b in choices], idx)
                rows[(kind, shift)] = (r, choices)
                g.add(r)
            page.add(g)
        self._enclosed_rows = rows

        def go(*_):
            fperm = fmask = dperm = dmask = 0
            for (kind, shift), (r, choices) in rows.items():
                bits = choices[r.get_selected()][1]
                if kind == "files":
                    fperm |= bits << shift
                    fmask |= 6 << shift                  # lecture / écriture ; l'exécution reste
                else:
                    dperm |= bits << shift
                    dmask |= 7 << shift
            dialog.close()
            self.window._run(tr("Changement des permissions"),
                             lambda j: chmod_tree(j, folder, fperm, fmask, dperm, dmask),
                             on_done=lambda _j: self._sync_summary())
        action.connect("clicked", go)
        self._enclosed_go = go
        dialog.present(self)

    # ------------------------------------------------------------------
    # Icône personnalisée d'un dossier (Nautilus : « Définir une icône personnalisée »)
    def _show_icon(self, rec):
        meta = folder_colors.read(rec.path)
        if rec.extra:
            rec.extra.pop("meta", None)
        if meta:
            rec.set("meta", meta)
        self.hero_img.set_from_gicon(icons.gicon_for(rec))
        self.icon_reset.set_visible(bool(meta.get("icon_uri") or meta.get("icon_name")))

    def _changed(self, rec):
        self._show_icon(rec)
        refresh = getattr(self.window, "_refresh_meta", None)
        if refresh:
            refresh([rec.path])

    def _choose_icon(self, rec):
        dlg = Gtk.FileDialog(title=tr("Sélectionnez une icône personnalisée"), accept_label=tr("_Sélectionner"),
                             modal=True, initial_folder=Gio.File.new_for_path(rec.path))
        flt = Gtk.FileFilter(name=tr("Images"))
        for t in IMAGE_TYPES:
            flt.add_mime_type(t)
        dlg.set_default_filter(flt)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f is None or not f.get_path():
                toast(self, tr("Seules les images locales peuvent servir d'icône."))
                return
            folder_colors.set_custom_icon(rec.path, f.get_uri())
            self._changed(rec)
        dlg.open(self, None, done)

    def _reset_icon(self, rec):
        old = folder_colors.reset_icon(rec.path)
        self._changed(rec)

        def undo():
            folder_colors.restore_icon(rec.path, old)
            self._changed(rec)
        toast(self, tr("Icône personnalisée retirée"), tr("Annuler"), undo)


def chmod_tree(job, folder: str, fperm: int, fmask: int, dperm: int, dmask: int) -> fileops.Result:
    """Comme Nautilus (nautilus_file_set_permissions_recursive) : le dossier et tout ce qu'il contient ;
    fichiers : lecture / écriture (l'exécution reste), dossiers : lecture / écriture / accès. Les liens
    symboliques ne sont ni suivis ni modifiés."""
    res = fileops.Result("chmod")
    todo = [folder]
    done = 0
    while todo:
        job.check_cancel()
        d = todo.pop()
        try:
            s = os.lstat(d)
            if not st.S_ISDIR(s.st_mode):
                continue
            os.chmod(d, (st.S_IMODE(s.st_mode) & ~dmask) | dperm)
            done += 1
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as err:
            res.errors.append((d, err.strerror or str(err)))
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    todo.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    m = st.S_IMODE(e.stat(follow_symlinks=False).st_mode)
                    os.chmod(e.path, (m & ~fmask) | fperm)
                    done += 1
            except OSError as err:
                res.errors.append((e.path, err.strerror or str(err)))
        job.progress(None, ntr("{0} élément", "{0} éléments", done).format(done))
    res.count = done
    return res
