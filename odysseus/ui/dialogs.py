"""Préférences (dont la page Fonctionnalités, qui remplace le gestionnaire d'extensions),
raccourcis clavier et choix d'une application."""

from __future__ import annotations


from gi.repository import Adw, Gio, GLib, Gtk

from .. import features
from ..i18n import LANGUAGE_NAMES, ntr, tr
from ..settings import settings
from .common import DialogWindow, combo_row, toast


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


class PreferencesDialog(Adw.PreferencesWindow):
    def __init__(self):
        # vraie fenêtre comme les préférences de Nautilus 46 : le thème du système s'y applique
        super().__init__(title=tr("Préférences"), search_enabled=True, default_width=720, default_height=720)
        self.add(self._general())
        self.add(self._features())
        self.add(self._archives())
        self.add(self._components())

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
        g.add(_switch(tr("Taper au clavier pour aller au fichier"), "type_ahead",
                      tr("Taper le début d'un nom sélectionne le fichier, comme dans Nemo ou Thunar ; "
                         "sinon, taper lance la recherche, comme dans Nautilus (Ctrl+F reste disponible)")))
        g.add(_switch(tr("Ouvrir les dossiers survolés pendant un glisser"), "dnd_hover_open",
                      tr("Tenir un élément une demi-seconde au-dessus d'un dossier l'ouvre, comme dans Nautilus")))
        page.add(g)

        g = Adw.PreferencesGroup(title=tr("Barre latérale"))
        g.add(_switch(tr("Afficher « Ce PC »"), "sidebar_this_pc",
                      tr("Dossiers et disques avec leur espace libre sur une seule page, comme sous Windows")))
        g.add(_switch(tr("Afficher le Bureau"), "sidebar_desktop"))
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
        g.add(_switch(tr("Répondre à « Afficher dans le dossier »"), "dbus_file_manager",
                      tr("Firefox, téléchargements… ouvrent Odysseus au lieu de Nautilus, même s'il est fermé")))
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
                if missing:     # la commande d'installation est dans l'onglet Composants
                    sub += "\n" + tr("⚠ Manquant : {0} (voir l'onglet Composants)").format(", ".join(missing))
                row.set_subtitle(sub)
                from .icons import first_icon
                row.add_prefix(Gtk.Image(icon_name=first_icon(f.icon, "view-dual-symbolic"
                                                              if f.id == "column_view" else "application-x-addon-symbolic")))
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
        return page

    # ------------------------------------------------------------------
    def _components(self):
        """Programmes externes, groupés par usage, et la commande qui installe tout ce qui manque."""
        from ..backend.engine import sevenzip_version
        from .icons import first_icon
        page = Adw.PreferencesPage(title=tr("Composants"), name="components",
                                   icon_name=first_icon("application-x-executable-symbolic", "system-run-symbolic"),
                                   description=tr("Programmes externes utilisés par Odysseus. S'il en manque un, "
                                                  "seules les fonctions qui en ont besoin sont touchées."))
        missing = [alts[0] for _cat, comps in features.COMPONENTS for _n, alts, _u in comps
                   if not features.component_path(alts)]
        if missing:
            cmd = features.apt_command(features.PACKAGE_OF.get(m, m) for m in missing)
            g = Adw.PreferencesGroup(title=tr("Installer ce qui manque"),
                                     description=tr("À copier dans un terminal (mot de passe administrateur demandé)"))
            row = Adw.ActionRow(title=cmd, title_selectable=True)
            row.add_css_class("monospace")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text=tr("Copier"))
            copy.connect("clicked", lambda b: (b.get_clipboard().set(cmd), self.add_toast(Adw.Toast(title=tr("Copié")))))
            row.add_suffix(copy)
            g.add(row)
            page.add(g)
            self.install_command = cmd
        for cat, comps in features.COMPONENTS:
            g = Adw.PreferencesGroup(title=cat)
            for name, alts, usage in comps:
                path = features.component_path(alts)
                ver = sevenzip_version() if name == "7-Zip" and path else ""
                state = (ver + " — " if ver else "") + path if path else \
                    tr("non installé — paquet {0}").format(features.PACKAGE_OF.get(alts[0], alts[0]))
                r = Adw.ActionRow(title=name, subtitle=usage + "\n" + state)
                img = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if path else "dialog-warning-symbolic")
                img.add_css_class("success" if path else "warning")
                r.add_suffix(img)
                g.add(r)
            page.add(g)
        return page


SHORTCUTS = [
    (tr("Navigation"), [
        (tr("Ctrl+L"), tr("Saisir un emplacement")), (tr("Alt+← / Retour arrière"), tr("Précédent")),
        (tr("Alt+→"), tr("Suivant")), (tr("Boutons latéraux de la souris"), tr("Précédent / suivant")),
        (tr("Alt+↑"), tr("Dossier parent")), (tr("Alt+Origine"), tr("Dossier personnel")),
        (tr("F5 / Ctrl+R"), tr("Actualiser")), (tr("Ctrl+F"), tr("Rechercher")), (tr("Ctrl+H"), tr("Fichiers cachés")),
        (tr("Ctrl+T / Ctrl+W"), tr("Ouvrir / fermer un onglet")), (tr("Ctrl+N"), tr("Nouvelle fenêtre")),
        (tr("Ctrl+D"), tr("Ajouter aux signets")), (tr("Ctrl+F9"), tr("Barre latérale"))]),
    (tr("Affichage"), [
        (tr("Ctrl+1 / Ctrl+2 / Ctrl+3"), tr("Liste / grille / colonnes")), (tr("Ctrl++ / Ctrl+− / Ctrl+0"), tr("Taille des icônes")),
        (tr("F3"), tr("Double panneau")), (tr("Ctrl+Tab"), tr("Changer de panneau (ou d'onglet)")), (tr("F4"), tr("Panneau d'aperçu")),
        (tr("F9"), tr("Basculer la vue en colonnes"))]),
    (tr("Fichiers"), [
        (tr("Ctrl+C / Ctrl+X / Ctrl+V"), tr("Copier / couper / coller")), (tr("F2"), tr("Renommer")),
        (tr("Suppr"), tr("Mettre à la corbeille")), (tr("Maj+Suppr"), tr("Supprimer définitivement")),
        (tr("Ctrl+Maj+N"), tr("Nouveau dossier")), (tr("Ctrl+I"), tr("Propriétés")), (tr("Ctrl+M"), tr("Créer un lien")),
        (tr("Ctrl+Z"), tr("Annuler")), (tr("Ctrl+A"), tr("Tout sélectionner")), (tr("Ctrl+Maj+I"), tr("Inverser la sélection")),
        (tr("F6 / Maj+F6"), tr("Copier / déplacer vers l'autre panneau")), (tr("Ctrl+Maj+C"), tr("Copier le chemin")),
        (tr("Ctrl+Alt+T"), tr("Terminal ici"))]),
    (tr("Outils"), [
        (tr("F7"), tr("Parcourir l'archive sélectionnée")), (tr("Ctrl+E"), tr("Extraire vers…")),
        (tr("Ctrl+Maj+E"), tr("Extraire ici")), (tr("F8"), tr("Rechercher dans le contenu des fichiers"))]),
]


def show_shortcuts(parent):
    dlg = DialogWindow(title=tr("Raccourcis clavier"), content_width=560, content_height=680)
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


def launch_app(app: Gio.AppInfo, files: list, context=None):
    """Lance une application sur des fichiers : par leur chemin, ou par leur URI quand ils n'ont
    pas de chemin local (admin://, smb://… de GVfs). Lève GLib.Error."""
    if all(f.get_path() for f in files):
        app.launch(files, context)
    elif app.supports_uris():
        app.launch_uris([f.get_uri() for f in files], context)
    else:
        raise GLib.Error(tr("Cette application ne sait pas ouvrir les fichiers distants."))


class AppChooser(DialogWindow):
    """« Ouvrir avec une autre application », comme Nautilus 46 : application par défaut, applications
    recommandées puis autres (Gtk.AppChooserWidget, le même composant), et « Toujours utiliser pour
    ce type de fichiers »."""

    def __init__(self, parent, ctype: str, files: list):
        """files : chemins ou Gio.File (fichiers d'admin://, smb://… sans chemin local)."""
        self.files = [f if isinstance(f, Gio.File) else Gio.File.new_for_path(f) for f in files]
        n = len(self.files)
        super().__init__(title=tr("Ouvrir le fichier") if n == 1 else tr("Ouvrir les fichiers"), content_width=460,
                         content_height=600)
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        name = GLib.markup_escape_text(self.files[0].get_basename() or "") if n == 1 else ""
        label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, use_markup=True)
        label.set_markup(tr("Choisir une application pour ouvrir <b>{0}</b>").format(name) if n == 1 else
                         ntr("Choisir une application pour ouvrir le fichier",
                             "Choisir une application pour ouvrir les {0} fichiers", n).format(n))
        box.append(label)
        self.chooser = Gtk.AppChooserWidget(content_type=ctype, show_default=True, show_recommended=True,
                                            show_fallback=True, show_other=True, vexpand=True)
        self.chooser.connect("application-selected", self._selected)
        self.chooser.connect("application-activated", lambda *_: self._open())
        # liste sans fond propre (comme Nautilus) : celui de la fenêtre, donc du thème, transparaît
        box.append(Gtk.Frame(child=self.chooser, vexpand=True, css_classes=["odysseus-app-chooser"]))
        tv.set_content(box)
        self.default_row = Adw.SwitchRow(title=tr("Toujours utiliser pour ce type de fichiers"),
                                         subtitle=Gio.content_type_get_description(ctype), sensitive=False)
        bottom = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                             margin_start=12, margin_end=12, margin_bottom=12)
        bottom.append(self.default_row)
        tv.add_bottom_bar(bottom)
        self.set_child(tv)
        app = self.chooser.get_app_info()
        if app is not None:
            self._selected(self.chooser, app)

    def _selected(self, _chooser, app):
        self.open_btn.set_sensitive(app is not None)
        default = Gio.AppInfo.get_default_for_type(self.ctype, False)
        # comme Nautilus : application par défaut choisie → interrupteur allumé mais grisé ;
        # une autre → éteint, à activer si on veut en faire l'application par défaut
        is_default = app is not None and default is not None and app.equal(default)
        self.default_row.set_active(is_default)
        self.default_row.set_sensitive(app is not None and not is_default)

    def _open(self):
        app = self.chooser.get_app_info()
        if app is None:
            return
        if self.default_row.get_active() and self.default_row.get_sensitive():
            try:
                app.set_as_default_for_type(self.ctype)
            except GLib.Error:
                pass
        try:
            launch_app(app, self.files, self.get_display().get_app_launch_context())
        except GLib.Error as e:
            toast(self, e.message)
        self.close()
