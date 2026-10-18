"""Menus : contextuel (comme Nautilus, avec les outils des extensions), vue, menu principal."""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Gio, GLib

from .. import features
from ..backend import folder_colors, formats, media
from ..i18n import tr
from ..settings import settings

TEXT_PREFIX = "text/"


def main_menu(win=None) -> Gio.Menu:
    """Menu principal, sur le modèle de Nautilus. Ce que les boutons de l'en-tête font déjà
    (double panneau, aperçu, recherche) n'y est pas répété."""
    m = Gio.Menu()
    s = Gio.Menu()
    s.append(tr("Nouvelle fenêtre"), "win.new-window")
    s.append(tr("Nouvel onglet"), "win.new-tab")
    m.append_section(None, s)
    s = Gio.Menu()
    zoom = Gio.MenuItem.new(None, None)
    zoom.set_attribute_value("custom", GLib.Variant.new_string("zoom"))
    s.append_item(zoom)
    m.append_section(None, s)
    s = Gio.Menu()
    undo = win.undo_stack[-1] if win is not None and win.undo_stack else None
    redo = win.redo_stack[-1] if win is not None and win.redo_stack else None
    label = win.undo_label if win is not None else (lambda e, redo=False: tr("Rétablir") if redo else tr("Annuler la dernière opération"))
    s.append(label(undo), "win.undo")
    s.append(label(redo, redo=True), "win.redo")
    m.append_section(None, s)
    s = Gio.Menu()
    s.append(tr("Afficher les fichiers cachés"), "win.show-hidden")
    m.append_section(None, s)
    s = Gio.Menu()
    s.append(tr("Préférences"), "app.preferences")
    s.append(tr("Raccourcis clavier"), "win.shortcuts")
    s.append(tr("À propos d'Odysseus"), "app.about")
    m.append_section(None, s)
    return m


def view_menu() -> Gio.Menu:
    m = Gio.Menu()
    views = Gio.Menu()
    views.append(tr("Liste"), "win.view-mode::list")
    views.append(tr("Grille"), "win.view-mode::grid")
    if features.enabled("column_view"):
        views.append(tr("Colonnes"), "win.view-mode::columns")
    m.append_section(None, views)
    sort = Gio.Menu()
    for key, label in (("name", tr("Nom")), ("size", tr("Taille")), ("type", tr("Type")),
                       ("date", tr("Date de modification"))):
        sort.append(label, f"win.sort-by::{key}")
    if features.available("duration_column"):
        sort.append(tr("Durée"), "win.sort-by::duration")
    sort.append(tr("Ordre inverse"), "win.sort-reverse")
    m.append_section(tr("Trier"), sort)
    cols = Gio.Menu()
    cols.append(tr("Taille"), "win.column-size")
    cols.append(tr("Type"), "win.column-type")
    cols.append(tr("Modifié"), "win.column-date")
    cols.append(tr("Droits"), "win.column-perms")
    cols.append(tr("Propriétaire"), "win.column-owner")
    if features.available("duration_column"):
        cols.append(tr("Durée"), "win.column-duration")
    s = Gio.Menu()
    s.append_submenu(tr("Colonnes visibles"), cols)
    s.append(tr("Agrandir les icônes"), "win.zoom-in")
    s.append(tr("Réduire les icônes"), "win.zoom-out")
    s.append(tr("Taille par défaut"), "win.zoom-normal")
    s.append(tr("Afficher les fichiers cachés"), "win.show-hidden")
    m.append_section(None, s)
    return m


def _is_text(rec) -> bool:
    if rec.is_dir:
        return False
    exts = {"." + e.strip().lstrip(".").lower() for e in settings["text_editor_exts"].split(",") if e.strip()}
    ext = os.path.splitext(rec.name)[1].lower()
    return ext in exts or rec.content_type.startswith(TEXT_PREFIX)


def _default_app(rec) -> Optional[Gio.AppInfo]:
    try:
        return Gio.AppInfo.get_default_for_type(rec.content_type, False) if rec.content_type else None
    except GLib.Error:
        return None


def _colors_menu(paths: list[str], all_dirs: bool) -> Gio.Menu:
    sub = Gio.Menu()
    if all_dirs:
        colors = Gio.Menu()
        for key, label, _hex in folder_colors.COLORS:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.folder-color", GLib.Variant.new_string(key))
            item.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(folder_colors.folder_icon_file(key))))
            colors.append_item(item)
        sub.append_section(None, colors)
    emblems = Gio.Menu()
    for icon, label in folder_colors.EMBLEMS:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value("win.emblem", GLib.Variant.new_string(icon))
        item.set_icon(Gio.ThemedIcon.new(icon))
        emblems.append_item(item)
    sub.append_section(None, emblems)
    reset = Gio.Menu()
    reset.append(tr("Par défaut"), "win.folder-restore")
    sub.append_section(None, reset)
    return sub


def context_menu(win, pane, on_item: bool) -> Gio.Menu:
    sel = pane.selected_recs() if on_item else []
    m = Gio.Menu()
    dual = win.is_dual()
    if pane.mode == "archive":
        return _archive_menu(pane, sel)
    if pane.mode == "trash":
        s = Gio.Menu()
        if sel:
            s.append(tr("Restaurer"), "win.restore")
            s.append(tr("Supprimer définitivement"), "win.delete")
        else:
            s.append(tr("Vider la corbeille…"), "win.empty-trash")
        m.append_section(None, s)
        return m
    if not sel:
        return _background_menu(win, pane)

    one = sel[0] if len(sel) == 1 else None
    paths = [r.path for r in sel if r.path]
    dirs = [r for r in sel if r.is_dir]
    files = [r for r in sel if not r.is_dir]

    s = Gio.Menu()
    default = _default_app(one) if one is not None and not one.is_dir else None
    # comme Nautilus : « Ouvrir avec gThumb » puis « Ouvrir avec une autre application »
    s.append(tr("Ouvrir avec {0}").format(default.get_display_name()) if default else tr("Ouvrir"),
             "win.open")
    if files and not dirs:
        s.append(tr("Ouvrir avec une autre application"), "win.open-with")
    if dirs:
        s.append(tr("Ouvrir dans un nouvel onglet"), "win.open-tab")
        if features.enabled("dual_panel"):
            s.append(tr("Ouvrir dans l'autre panneau"), "win.open-other")
    if pane.mode in ("search", "remote") and one is not None and one.path:
        s.append(tr("Ouvrir l'emplacement de l'élément"), "win.open-location")
    if one is not None and one.is_dir and features.enabled("terminal"):
        s.append(tr("Ouvrir dans un terminal"), "win.terminal-selected")
    if one is not None and one.path and features.available("admin") and not win.is_admin():
        s.append(tr("Ouvrir en tant qu'administrateur"), "win.open-admin")
    m.append_section(None, s)

    s = Gio.Menu()
    s.append(tr("Couper"), "win.cut")
    s.append(tr("Copier"), "win.copy")
    if one is not None and one.is_dir:
        s.append(tr("Coller dans le dossier"), "win.paste-into")
    s.append(tr("Déplacer vers…"), "win.move-to")
    s.append(tr("Copier vers…"), "win.copy-to")
    if dual:
        s.append(tr("Copier vers l'autre panneau"), "win.copy-to-other")
        s.append(tr("Déplacer vers l'autre panneau"), "win.move-to-other")
    m.append_section(None, s)

    s = Gio.Menu()
    if one is not None:
        s.append(tr("Renommer…"), "win.rename")
    s.append(tr("Créer un lien"), "win.make-link")
    if features.available("archives"):
        archives = [r for r in files if formats.looks_like_archive(r.name)]
        if archives and len(archives) == len(sel):
            s.append(tr("Extraire ici"), "win.extract-here")
            s.append(tr("Extraire vers…"), "win.extract")
        if one is not None and not one.is_dir and formats.readable(one.name):
            s.append(tr("Parcourir l'archive"), "win.browse-archive")
        s.append(tr("Compresser…"), "win.compress")
    m.append_section(None, s)

    # Outils issus des extensions
    t = Gio.Menu()
    pdfs = [r for r in files if r.name.lower().endswith(".pdf")]
    if pdfs and len(pdfs) == len(sel):
        pdf = Gio.Menu()
        if features.available("pdf_compress"):
            pdf.append(tr("Compresser le PDF…"), "win.pdf-compress")
        if features.available("pdf_merge") and len(pdfs) >= 1:
            pdf.append(tr("Fusionner des PDF…"), "win.pdf-merge")
        if features.available("pdf_watermark"):
            pdf.append(tr("Ajouter un filigrane…"), "win.pdf-watermark")
        if features.available("pdf_pages") and len(pdfs) == 1:
            pdf.append(tr("Pages : extraire, pivoter, éclater…"), "win.pdf-pages")
        if pdf.get_n_items():
            t.append_submenu(tr("Outils PDF"), pdf)
    if files and len(files) == len(sel) and all(media.is_video(r.name) for r in files) \
            and features.available("video_to_audio"):
        t.append(tr("Extraire l'audio…"), "win.video-to-audio")
    if one is not None and not one.is_dir and one.content_type.startswith("image/") and features.available("annotate"):
        t.append(tr("Annoter l'image…"), "win.annotate")
    if one is not None and one.name.lower().endswith(".deb") and features.available("deb_installer"):
        t.append(tr("Installer le paquet…"), "win.deb-install")
    if files and len(files) == len(sel) and all(_is_text(r) for r in files) and features.enabled("text_editor"):
        t.append(tr("Ouvrir avec l'éditeur de texte"), "win.edit-text")
    if one is not None and one.is_dir and features.available("content_search"):
        t.append(tr("Rechercher dans les fichiers…"), "win.content-search-selected")
    if features.enabled("folder_colors") and paths:
        t.append_submenu(tr("Couleur et emblème") if dirs and len(dirs) == len(sel) else tr("Emblème"),
                         _colors_menu(paths, bool(dirs) and len(dirs) == len(sel)))
    if t.get_n_items():
        m.append_section(None, t)

    s = Gio.Menu()
    s.append(tr("Mettre à la corbeille"), "win.trash")
    s.append(tr("Supprimer définitivement…"), "win.delete")
    m.append_section(None, s)
    s = Gio.Menu()
    s.append(tr("Copier le chemin"), "win.copy-path")
    s.append(tr("Propriétés"), "win.properties")
    m.append_section(None, s)
    return m


def _background_menu(win, pane) -> Gio.Menu:
    m = Gio.Menu()
    if pane.mode == "fs":
        s = Gio.Menu()
        s.append(tr("Nouveau dossier…"), "win.new-folder")
        s.append_submenu(tr("Nouveau document"), new_document_menu())
        m.append_section(None, s)
        s = Gio.Menu()
        s.append(tr("Coller"), "win.paste")
        s.append(tr("Tout sélectionner"), "win.select-all")
        m.append_section(None, s)
        s = Gio.Menu()
        if features.enabled("terminal"):
            s.append(tr("Ouvrir dans un terminal"), "win.terminal")
        if features.available("content_search"):
            s.append(tr("Rechercher dans les fichiers…"), "win.content-search")
        if features.available("archives"):
            s.append(tr("Compresser des fichiers…"), "win.compress")
        if s.get_n_items():
            m.append_section(None, s)
    s = Gio.Menu()
    s.append(tr("Ajouter aux signets"), "win.bookmark")
    s.append(tr("Copier l'emplacement"), "win.copy-location")
    s.append(tr("Propriétés"), "win.properties-folder")
    m.append_section(None, s)
    return m


def folder_menu() -> Gio.Menu:
    """Menu « ⋮ » de la barre de chemin (dossier courant), sur le modèle de Nautilus."""
    m = Gio.Menu()
    s = Gio.Menu()
    s.append(tr("Nouveau dossier…"), "win.new-folder")
    s.append_submenu(tr("Nouveau document"), new_document_menu())
    m.append_section(None, s)
    s = Gio.Menu()
    s.append(tr("Actualiser"), "win.refresh")
    s.append(tr("Ajouter aux signets"), "win.bookmark")
    s.append(tr("Saisir l'emplacement"), "win.location")
    s.append(tr("Copier l'emplacement"), "win.copy-location")
    m.append_section(None, s)
    s = Gio.Menu()
    if features.enabled("terminal"):
        s.append(tr("Ouvrir dans un terminal"), "win.terminal")
    if features.enabled("dual_panel"):
        s.append(tr("Double panneau"), "win.dual")
    if features.available("content_search"):
        s.append(tr("Rechercher dans les fichiers…"), "win.content-search")
    s.append(tr("Fonctionnalités"), "app.features")
    m.append_section(None, s)
    s = Gio.Menu()
    if features.available("admin"):
        s.append(tr("Ouvrir en tant qu'administrateur"), "win.open-admin-folder")
    s.append(tr("Propriétés"), "win.properties-folder")
    m.append_section(None, s)
    return m


def new_document_menu() -> Gio.Menu:
    sub = Gio.Menu()
    sub.append(tr("Document vide"), "win.new-empty-file")
    tdir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
    templates = Gio.Menu()
    if tdir and os.path.isdir(tdir) and os.path.realpath(tdir) != os.path.realpath(os.path.expanduser("~")):
        try:
            names = sorted(e.name for e in os.scandir(tdir) if e.is_file() and not e.name.startswith("."))
        except OSError:
            names = []
        for name in names[:40]:
            item = Gio.MenuItem.new(os.path.splitext(name)[0], None)
            item.set_action_and_target_value("win.new-from-template",
                                             GLib.Variant.new_string(os.path.join(tdir, name)))
            templates.append_item(item)
    if templates.get_n_items():
        sub.append_section(None, templates)
    return sub


def _archive_menu(pane, sel) -> Gio.Menu:
    m = Gio.Menu()
    if sel:
        s = Gio.Menu()
        s.append(tr("Ouvrir"), "win.open")
        if len(sel) == 1 and not sel[0].is_dir:
            s.append(tr("Ouvrir avec l'application par défaut"), "win.open-default")
        m.append_section(None, s)
        s = Gio.Menu()
        s.append(tr("Extraire la sélection…"), "win.extract-selected")
        s.append(tr("Extraire la sélection à côté de l'archive"), "win.extract-here")
        m.append_section(None, s)
    else:
        s = Gio.Menu()
        s.append(tr("Tout extraire…"), "win.extract-all")
        s.append(tr("Tester l'intégrité"), "win.archive-test")
        s.append(tr("Propriétés de l'archive"), "win.archive-info")
        m.append_section(None, s)
    s = Gio.Menu()
    if formats.is_modifiable(pane.archive_path):
        s.append(tr("Ajouter des fichiers…"), "win.archive-add")
        s.append(tr("Nouveau dossier…"), "win.new-folder")
        if len(sel) == 1:
            s.append(tr("Renommer…"), "win.rename")
        if sel:
            s.append(tr("Supprimer de l'archive"), "win.delete")
    s.append(tr("Copier le chemin"), "win.copy-path")
    s.append(tr("Fermer l'archive"), "win.close-archive")
    m.append_section(None, s)
    return m
