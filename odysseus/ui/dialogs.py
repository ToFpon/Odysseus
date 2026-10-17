"""Préférences (dont la page Fonctionnalités, qui remplace le gestionnaire d'extensions),
raccourcis clavier et choix d'une application."""

from __future__ import annotations

import shutil

from gi.repository import Adw, Gio, GLib, Gtk

from .. import features
from ..i18n import LANGUAGE_NAMES, tr
from ..settings import settings
from .common import combo_row, toast


def apply_theme():
    Adw.StyleManager.get_default().set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(settings["theme"], Adw.ColorScheme.DEFAULT))


def _switch(title: str, key: str, subtitle: str = "") -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, active=bool(settings[key]))
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::active", lambda r, *_: settings.__setitem__(key, r.get_active()))
    return row


def _choice(title: str, key: str, labels: list[str], values: list, subtitle: str = "") -> Adw.ComboRow:
    cur = settings[key]
    row = combo_row(title, labels, values.index(cur) if cur in values else 0, subtitle)
    row.connect("notify::selected", lambda r, *_: settings.__setitem__(key, values[r.get_selected()]))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self):
        super().__init__(title=tr("Préférences"), search_enabled=True)
        self.add(self._general())
        self.add(self._features())
        self.add(self._archives())

    # ------------------------------------------------------------------
    def _general(self):
        page = Adw.PreferencesPage(title=tr("Général"), icon_name="preferences-system-symbolic", name="general")
        g = Adw.PreferencesGroup(title=tr("Apparence"))
        theme = combo_row(tr("Thème"), [tr("Suivre le système"), tr("Clair"), tr("Sombre")],
                          ["system", "light", "dark"].index(settings["theme"]) if settings["theme"]
                          in ("system", "light", "dark") else 0)

        def set_theme(r, *_):
            settings["theme"] = ["system", "light", "dark"][r.get_selected()]
            apply_theme()
        theme.connect("notify::selected", set_theme)
        g.add(theme)
        langs = list(LANGUAGE_NAMES)
        lang = combo_row(tr("Langue"), [tr("Langue du système")] + [LANGUAGE_NAMES[k] for k in langs[1:]],
                         langs.index(settings["language"]) if settings["language"] in langs else 0,
                         subtitle=tr("Prend effet au prochain démarrage d'Odysseus"))

        def set_lang(r, *_):
            settings["language"] = langs[r.get_selected()]
            toast(self, tr("Redémarrez Odysseus pour changer de langue"))
        lang.connect("notify::selected", set_lang)
        g.add(lang)
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Affichage des dossiers"))
        g.add(_choice(tr("Vue par défaut"), "view_mode", [tr("Liste"), tr("Grille"), tr("Colonnes")],
                      ["list", "grid", "columns"]))
        g.add(_switch(tr("Dossiers en premier"), "folders_first"))
        g.add(_switch(tr("Afficher les fichiers cachés"), "show_hidden"))
        g.add(_choice(tr("Taille des icônes de la liste"), "list_zoom",
                      [tr("Petite (compacte)"), tr("Moyenne"), tr("Grande")], ["small", "medium", "large"]))
        g.add(_choice(tr("Miniatures"), "thumbnails",
                      [tr("Fichiers locaux seulement"), tr("Toujours"), tr("Jamais")], ["local", "always", "never"]))
        g.add(_switch(tr("Ouvrir d'un simple clic"), "single_click",
                      tr("Sinon, double-clic comme dans Nautilus")))
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Comportement"))
        g.add(_switch(tr("Confirmer la mise à la corbeille"), "confirm_trash"))
        g.add(_switch(tr("Confirmer la suppression définitive"), "confirm_delete"))
        g.add(_switch(tr("Rechercher dans les sous-dossiers par défaut"), "search_recursive"))
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Applications"))
        from .actions_files import find_editor, find_terminal
        term = Adw.EntryRow(title=tr("Terminal (vide = automatique : {0})").format(find_terminal() or "—"),
                            text=settings["terminal"])
        term.connect("changed", lambda r: settings.__setitem__("terminal", r.get_text().strip()))
        g.add(term)
        ed = Adw.EntryRow(title=tr("Éditeur de texte (vide = automatique : {0})").format(find_editor() or "—"),
                          text=settings["text_editor"])
        ed.connect("changed", lambda r: settings.__setitem__("text_editor", r.get_text().strip()))
        g.add(ed)
        exts = Adw.EntryRow(title=tr("Extensions ouvertes dans l'éditeur"), text=settings["text_editor_exts"])
        exts.connect("changed", lambda r: settings.__setitem__("text_editor_exts", r.get_text()))
        g.add(exts)
        page.add(g)
        return page

    # ------------------------------------------------------------------
    def _features(self):
        page = Adw.PreferencesPage(title=tr("Fonctionnalités"), icon_name="application-x-addon-symbolic",
                                   name="features",
                                   description=tr("Toutes les extensions Nautilus de Tof sont intégrées. "
                                                  "Désactivez celles dont vous n'avez pas besoin : "
                                                  "le changement est immédiat."))
        from ..features import CATEGORIES, FEATURES
        for cat, label in CATEGORIES:
            g = Adw.PreferencesGroup(title=label)
            for f in [f for f in FEATURES if f.category == cat]:
                row = Adw.SwitchRow(title=f.title, active=features.enabled(f.id))
                sub = f.description
                if f.shortcut:
                    sub += "  ·  " + f.shortcut
                missing = features.missing_tools(f.id)
                if missing:
                    sub += "\n" + tr("⚠ Manquant : {0} (sudo apt install {1})").format(", ".join(missing), f.packages)
                row.set_subtitle(sub)
                row.add_prefix(Gtk.Image(icon_name=f.icon))
                row.connect("notify::active", lambda r, *_a, fid=f.id: features.set_enabled(fid, r.get_active()))
                g.add(row)
                if f.id == "hidden_dim":
                    g.add(_choice(tr("Atténuation des fichiers cachés"), "hidden_dim_mode",
                                  [tr("Icône seulement"), tr("Icône et nom")], ["icon", "all"]))
                if f.id == "progress_window":
                    delay = Adw.SpinRow.new_with_range(0, 10, 0.5)
                    delay.set_title(tr("Délai avant affichage (secondes)"))
                    delay.set_digits(1)
                    delay.set_value(float(settings["progress_window_delay"]))
                    delay.connect("notify::value", lambda r, *_: settings.__setitem__("progress_window_delay",
                                                                                     r.get_value()))
                    g.add(delay)
            page.add(g)
        return page

    # ------------------------------------------------------------------
    def _archives(self):
        from .archive_dialogs import OVERWRITE_LABELS, OVERWRITE_VALUES, SUBFOLDER_LABELS, SUBFOLDER_VALUES
        page = Adw.PreferencesPage(title=tr("Archives"), icon_name="package-x-generic-symbolic", name="archives")
        g = Adw.PreferencesGroup(title=tr("Extraction"))
        g.add(_choice(tr("Sous-dossier par défaut"), "extract_subfolder", SUBFOLDER_LABELS, SUBFOLDER_VALUES))
        g.add(_choice(tr("Fichiers déjà présents"), "extract_overwrite", OVERWRITE_LABELS, OVERWRITE_VALUES))
        g.add(_switch(tr("Ouvrir le dossier après extraction"), "open_after_extract"))
        g.add(_switch(tr("Mémoriser les mots de passe pendant la session"), "remember_passwords",
                      tr("Jamais enregistrés sur le disque")))
        page.add(g)
        g = Adw.PreferencesGroup(title=tr("Programmes utilisés"))
        from ..backend.engine import sevenzip_path, sevenzip_version
        rows = [("7-Zip", sevenzip_path(), sevenzip_version())]
        for prog, label in (("gs", "Ghostscript"), ("qpdf", "qpdf"), ("ffmpeg", "FFmpeg"), ("ffprobe", "ffprobe"),
                            ("ffmpegthumbnailer", "ffmpegthumbnailer"), ("rg", "ripgrep"), ("pdftoppm", "Poppler"),
                            ("zstd", "Zstandard"), ("brotli", "Brotli"), ("lz4", "LZ4")):
            rows.append((label, shutil.which(prog), ""))
        for label, path, ver in rows:
            r = Adw.ActionRow(title=label, subtitle=(ver + " — " if ver else "") + (path or tr("non installé")))
            img = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if path else "dialog-warning-symbolic")
            img.add_css_class("success" if path else "warning")
            r.add_suffix(img)
            g.add(r)
        page.add(g)
        return page


SHORTCUTS = [
    (tr("Navigation"), [
        ("Ctrl+L", tr("Saisir un emplacement")), ("Alt+← / Retour arrière", tr("Précédent")),
        ("Alt+→", tr("Suivant")), ("Alt+↑", tr("Dossier parent")), ("Alt+Origine", tr("Dossier personnel")),
        ("F5 / Ctrl+R", tr("Actualiser")), ("Ctrl+F", tr("Rechercher")), ("Ctrl+H", tr("Fichiers cachés")),
        ("Ctrl+T / Ctrl+W", tr("Ouvrir / fermer un onglet")), ("Ctrl+N", tr("Nouvelle fenêtre")),
        ("Ctrl+D", tr("Ajouter aux signets")), ("Ctrl+F9", tr("Barre latérale"))]),
    (tr("Affichage"), [
        ("Ctrl+1 / Ctrl+2 / Ctrl+3", tr("Liste / grille / colonnes")), ("Ctrl++ / Ctrl+− / Ctrl+0", tr("Taille des icônes")),
        ("F3", tr("Double panneau")), ("Ctrl+Tab", tr("Changer de panneau (ou d'onglet)")), ("F4", tr("Panneau d'aperçu")),
        ("F9", tr("Basculer la vue en colonnes"))]),
    (tr("Fichiers"), [
        ("Ctrl+C / Ctrl+X / Ctrl+V", tr("Copier / couper / coller")), ("F2", tr("Renommer")),
        ("Suppr", tr("Mettre à la corbeille")), ("Maj+Suppr", tr("Supprimer définitivement")),
        ("Ctrl+Maj+N", tr("Nouveau dossier")), ("Ctrl+I", tr("Propriétés")), ("Ctrl+M", tr("Créer un lien")),
        ("Ctrl+Z", tr("Annuler")), ("Ctrl+A", tr("Tout sélectionner")), ("Ctrl+Maj+I", tr("Inverser la sélection")),
        ("F6 / Maj+F6", tr("Copier / déplacer vers l'autre panneau")), ("Ctrl+Maj+C", tr("Copier le chemin")),
        ("Ctrl+Alt+T", tr("Terminal ici"))]),
    (tr("Outils"), [
        ("F7", tr("Parcourir l'archive sélectionnée")), ("Ctrl+E", tr("Extraire vers…")),
        ("Ctrl+Maj+E", tr("Extraire ici")), ("F8", tr("Rechercher dans le contenu des fichiers"))]),
]


def show_shortcuts(parent):
    dlg = Adw.Dialog(title=tr("Raccourcis clavier"), content_width=560, content_height=680)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()
    for title, rows in SHORTCUTS:
        g = Adw.PreferencesGroup(title=title)
        for keys, label in rows:
            r = Adw.ActionRow(title=label)
            r.add_suffix(Gtk.Label(label=keys, css_classes=["keycap"]))
            g.add(r)
        page.add(g)
    tv.set_content(page)
    dlg.set_child(tv)
    dlg.present(parent)


class AppChooser(Adw.Dialog):
    """« Ouvrir avec… » : applications recommandées, puis toutes les autres."""

    def __init__(self, parent, ctype: str, paths: list[str]):
        super().__init__(title=tr("Ouvrir avec"), content_width=440, content_height=560)
        self.paths = paths
        self.ctype = ctype
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=tr("Annuler"))
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.open_btn = Gtk.Button(label=tr("Ouvrir"), css_classes=["suggested-action"], sensitive=False)
        self.open_btn.connect("clicked", lambda *_: self._open())
        hb.pack_end(self.open_btn)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        self.search = Gtk.SearchEntry(placeholder_text=tr("Rechercher une application"))
        box.append(self.search)
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", lambda *_: self.open_btn.set_sensitive(self.list.get_selected_row()
                                                                                 is not None))
        self.list.connect("row-activated", lambda *_: self._open())
        self.list.set_filter_func(self._filter)
        self.search.connect("search-changed", lambda *_: self.list.invalidate_filter())
        box.append(Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.default_row = Adw.SwitchRow(title=tr("Toujours utiliser pour ce type de fichier"))
        g = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        g.append(self.default_row)
        box.append(g)
        tv.set_content(box)
        self.set_child(tv)
        seen = set()
        rec = Gio.AppInfo.get_recommended_for_type(ctype)
        others = sorted((a for a in Gio.AppInfo.get_all() if a.should_show()),
                        key=lambda a: a.get_display_name().casefold())
        for app in list(rec) + others:
            if app.get_id() in seen:
                continue
            seen.add(app.get_id())
            row = Adw.ActionRow(title=app.get_display_name(), subtitle=app.get_description() or "")
            if app.get_icon():
                img = Gtk.Image(gicon=app.get_icon(), pixel_size=32)
                row.add_prefix(img)
            row.app = app
            row.recommended = app in rec
            if row.recommended:
                row.add_suffix(Gtk.Label(label=tr("recommandée"), css_classes=["dim-label", "caption"]))
            self.list.append(row)

    def _filter(self, row):
        t = self.search.get_text().casefold()
        return not t or t in row.app.get_display_name().casefold()

    def _open(self):
        row = self.list.get_selected_row()
        if row is None:
            return
        app = row.get_child().app if not hasattr(row, "app") else row.app
        if self.default_row.get_active():
            try:
                app.set_as_default_for_type(self.ctype)
            except GLib.Error:
                pass
        try:
            app.launch([Gio.File.new_for_path(p) for p in self.paths], None)
        except GLib.Error as e:
            toast(self, e.message)
        self.close()
