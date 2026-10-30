"""Fenêtre principale d'Odysseus, à l'image de Nautilus."""

from __future__ import annotations

import os
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME, features
from ..backend import bookmarks
from ..i18n import tr
from ..settings import settings
from . import menus, sushi
from .actions_archives import ArchiveActions
from .actions_files import FileActions
from .actions_tools import ToolActions
from .common import bidir, popup_menu, shown, toast
from .jobs import OperationsButton, ProgressWindow, manager
from .pane import TRASH_URI, Pane
from .pathbar import PathBar
from .sidebar import Sidebar
from .views import GRID_ZOOM_LEVELS, ZOOM_LEVELS
from .workspace import Workspace

# Icône livrée avec Odysseus (data/icons) : Adwaita n'a pas d'icône de vue en colonnes
COLUMNS_ICON = "odysseus-view-columns-symbolic"


SIDEBAR_MAX = 360


class MainWindow(FileActions, ArchiveActions, ToolActions, Adw.ApplicationWindow):
    __gtype_name__ = "OdysseusWindow"
    saves_state = True           # taille, barre latérale, double panneau mémorisés à la fermeture
    is_archive_window = False

    def __init__(self, app, location: Optional[str] = None, dual: Optional[bool] = None):
        super().__init__(application=app, title=APP_NAME,
                         default_width=settings["window_width"], default_height=settings["window_height"],
                         width_request=360, height_request=360)
        self.add_css_class("odysseus-window")
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.progress_window: Optional[ProgressWindow] = None
        self._build()
        self._setup_actions()
        self._update_undo_actions()
        self.new_tab(location or os.path.expanduser("~"),
                     dual=settings["dual_panel"] and features.enabled("dual_panel") if dual is None else dual)
        settings.connect(self._on_setting)
        manager.connect("changed", lambda *_: self._update_dual_bar())
        self.connect("close-request", self._on_close)
        # boutons latéraux de la souris (8 = précédent, 9 = suivant), dans toute la fenêtre
        nav = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        nav.connect("event", self._on_mouse_nav)
        self.add_controller(nav)
        # touches qui ont un sens dans un champ de saisie : rendues au texte (voir _TEXT_KEYS)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_text_key)
        # taper pour aller au fichier : aussi en capture, avant les raccourcis (Retour arrière = Précédent)
        keys.connect("key-pressed", self._on_type_ahead)
        self.add_controller(keys)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self):
        self.toast_overlay = Adw.ToastOverlay()
        self.split = Adw.OverlaySplitView(sidebar_width_fraction=0.18, min_sidebar_width=200,
                                          max_sidebar_width=280, show_sidebar=settings["sidebar_visible"])
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())
        self.toast_overlay.set_child(self.split)
        self.set_content(self.toast_overlay)
        self.connect("map", lambda *_: self.fit_sidebar())
        # police ou mise à l'échelle du texte changée dans les Paramètres : les libellés aussi
        Gtk.Settings.get_default().connect("notify::gtk-font-name", lambda *_: self.fit_sidebar())
        Gtk.Settings.get_default().connect("notify::gtk-xft-dpi", lambda *_: self.fit_sidebar())
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _build_sidebar(self):
        tv = Adw.ToolbarView(css_classes=["sidebar-pane"])
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        # icône et nom à gauche, alignés sur les icônes et les textes de la barre latérale
        # (en titre, AdwHeaderBar les centrait)
        # (icône de 20 px centrée sur celles de 16 px des lignes, nom au niveau de leurs textes)
        brand = Gtk.Box(spacing=10, margin_start=14)
        self.brand_icon = Gtk.Image(icon_name=APP_ID, pixel_size=20)
        brand.append(self.brand_icon)
        brand.append(Gtk.Label(label=APP_NAME, css_classes=["heading"]))
        hb.pack_start(brand)
        hb.set_title_widget(Gtk.Box())
        self.main_menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                            tooltip_text=tr("Menu principal (F10)"), primary=True)
        # reconstruit à chaque ouverture : libellés « Annuler … / Rétablir … » à jour
        self.main_menu_btn.set_create_popup_func(lambda b: self._fill_main_menu())
        hb.pack_end(self.main_menu_btn)
        tv.add_top_bar(hb)
        self.sidebar = Sidebar()
        self.sidebar.connect("open", self._on_sidebar_open)
        self.sidebar.connect("drop", lambda _s, paths, target: self.drop_files(paths, target, "auto"))
        tv.set_content(self.sidebar)
        return tv

    def _build_content(self):
        tv = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT)
        # Comme Nautilus, la barre de chemin occupe toute la place entre les flèches et les boutons de
        # droite. Placée en titre, AdwHeaderBar la centrerait (vide à gauche) : toute la rangée est
        # donc le titre, avec ses propres boutons de fenêtre.
        hb = Adw.HeaderBar(css_classes=["main-header"], show_start_title_buttons=False,
                           show_end_title_buttons=False)
        bar = Gtk.Box(spacing=6, hexpand=True)
        hb.set_title_widget(bar)
        self.start_controls = Gtk.WindowControls(side=Gtk.PackType.START)
        bar.append(self.start_controls)
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=settings["sidebar_visible"],
                                            tooltip_text=tr("Barre latérale (Ctrl+F9)"))
        self.sidebar_btn.bind_property("active", self.split, "show-sidebar", bidir())
        bar.append(self.sidebar_btn)
        for icon, action, tip in (("go-previous-symbolic", "win.back", tr("Précédent (Alt+←)")),
                                  ("go-next-symbolic", "win.forward", tr("Suivant (Alt+→)"))):
            bar.append(Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip))
        self.pathbar = PathBar(menu_factory=menus.folder_menu)
        self.pathbar.connect("navigate", self._on_path_navigate)
        self.pathbar.connect("navigate-tab", lambda _b, loc: self.new_tab(loc))
        self.pathbar.connect("drop", lambda _b, paths, target: self.drop_files(paths, target, "auto"))
        bar.append(self.pathbar)

        self.ops_btn = OperationsButton()
        bar.append(self.ops_btn)
        # bouton de Column Browser, à côté du double panneau (même rôle que F9)
        self.columns_btn = Gtk.ToggleButton(icon_name=COLUMNS_ICON, tooltip_text=tr("Vue en colonnes (F9)"))
        self._columns_btn_id = self.columns_btn.connect("toggled", lambda b: self._on_columns_btn(b.get_active()))
        bar.append(self.columns_btn)
        self.dual_btn = Gtk.ToggleButton(icon_name="view-dual-symbolic", tooltip_text=tr("Double panneau (F3)"),
                                         action_name="win.dual")
        bar.append(self.dual_btn)
        self.preview_btn = Gtk.ToggleButton(icon_name="view-reveal-symbolic", tooltip_text=tr("Aperçu (F4)"),
                                            action_name="win.preview")
        bar.append(self.preview_btn)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=tr("Rechercher (Ctrl+F)"))
        bar.append(self.search_btn)
        self.view_btn = Adw.SplitButton(menu_model=menus.view_menu(), action_name="win.toggle-view",
                                        tooltip_text=tr("Changer de vue"), dropdown_tooltip=tr("Options d'affichage"))
        bar.append(self.view_btn)
        bar.append(Gtk.WindowControls(side=Gtk.PackType.END))
        # boutons de fenêtre côté gauche (selon la disposition du bureau) : déjà dans l'en-tête de la
        # barre latérale quand elle est visible
        for prop in ("show-sidebar", "collapsed"):
            self.split.connect("notify::" + prop, lambda *_: self._sync_start_controls())
        self._sync_start_controls()
        tv.add_top_bar(hb)
        # fenêtre administrateur (admin://) : avertissement permanent, comme Nautilus
        self.admin_banner = Adw.Banner(title=tr("Mode administrateur : les modifications sont faites avec les "
                                                "droits root. Soyez prudent."), css_classes=["admin-banner"])
        tv.add_top_bar(self.admin_banner)

        # recherche
        self.search_bar = Gtk.SearchBar(show_close_button=True)
        sbox = Gtk.Box(spacing=8)
        self.search_entry = Gtk.SearchEntry(placeholder_text=tr("Rechercher dans ce dossier…"), hexpand=True)
        sbox.append(self.search_entry)
        self.recursive_btn = Gtk.ToggleButton(label=tr("Sous-dossiers"), active=settings["search_recursive"],
                                              tooltip_text=tr("Chercher aussi dans les sous-dossiers"))
        self.recursive_btn.connect("toggled", self._on_recursive_toggled)
        sbox.append(self.recursive_btn)
        content_btn = Gtk.Button(icon_name="edit-find-replace-symbolic", action_name="win.content-search",
                                 tooltip_text=tr("Rechercher dans le contenu des fichiers (F8)"))
        sbox.append(content_btn)
        self.content_search_btn = content_btn
        self.search_bar.set_child(Adw.Clamp(maximum_size=720, child=sbox))
        self.search_bar.connect_entry(self.search_entry)
        # taper au clavier ouvre la recherche (comme Nautilus), sauf réglage « aller au fichier »
        self.search_bar.set_key_capture_widget(None if settings["type_ahead"] else self)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled", bidir())
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", self._on_search_activate)
        # comme Nautilus : flèche bas / haut dans le champ → la liste des résultats prend le focus, sur
        # le résultat sélectionné ; les flèches y naviguent ensuite (sans fermer la recherche)
        sk = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        sk.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(sk)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        tv.add_top_bar(self.search_bar)

        self.tab_view = Adw.TabView(vexpand=True)
        # Ctrl+Tab sert à changer de panneau en double panneau (voir _switch_pane).
        self.tab_view.remove_shortcuts(Adw.TabViewShortcuts.CONTROL_TAB | Adw.TabViewShortcuts.CONTROL_SHIFT_TAB)
        self.tab_view.connect("notify::selected-page", lambda *_: self._on_tab_switch())
        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("page-detached", lambda *_: GLib.idle_add(self._check_empty))
        self.tab_view.connect("create-window", self._on_create_window)
        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=True, expand_tabs=True)
        tv.add_top_bar(self.tab_bar)

        # aperçu à droite (fonctionnalité Preview Panel)
        self.content_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, shrink_end_child=False,
                                       resize_end_child=False, wide_handle=False)
        self.content_paned.set_start_child(self.tab_view)
        self.preview = None
        tv.set_content(self.content_paned)

        # barre d'actions du double panneau (reprise de Dual Panel)
        self.dual_bar = Gtk.ActionBar(revealed=False, css_classes=["dual-bar"])
        for icon, action, tip in (("folder-new-symbolic", "win.new-folder", tr("Nouveau dossier")),
                                  ("document-new-symbolic", "win.new-empty-file", tr("Nouveau fichier")),
                                  ("document-edit-symbolic", "win.rename", tr("Renommer (F2)")),
                                  ("user-trash-symbolic", "win.trash", tr("Mettre à la corbeille (Suppr)"))):
            self.dual_bar.pack_start(Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip))
        perm = Gtk.Button(action_name="win.delete", tooltip_text=tr("Supprimer définitivement (Maj+Suppr)"),
                          css_classes=["destructive-trash"])
        perm.set_child(Gtk.Image(icon_name="user-trash-symbolic", css_classes=["error"]))
        self.dual_bar.pack_start(perm)
        center = Gtk.Box(spacing=6)
        self.copy_other_btn = Gtk.Button(action_name="win.copy-to-other", css_classes=["suggested-action"],
                                         tooltip_text=tr("Copier vers l'autre panneau (F6)"))
        self.move_other_btn = Gtk.Button(action_name="win.move-to-other",
                                         tooltip_text=tr("Déplacer vers l'autre panneau (Maj+F6)"))
        center.append(self.copy_other_btn)
        center.append(self.move_other_btn)
        self.dual_bar.set_center_widget(center)
        self.dual_extract_btn = Gtk.Button(icon_name="folder-download-symbolic", action_name="win.extract-here",
                                           tooltip_text=tr("Extraire ici"), visible=False)
        self.dual_audio_btn = Gtk.Button(icon_name="audio-x-generic-symbolic", action_name="win.video-to-audio",
                                         tooltip_text=tr("Extraire l'audio"), visible=False)
        self.dual_bar.pack_end(Gtk.Button(icon_name="utilities-terminal-symbolic", action_name="win.terminal",
                                          tooltip_text=tr("Terminal ici")))
        self.dual_bar.pack_end(self.dual_audio_btn)
        self.dual_bar.pack_end(self.dual_extract_btn)
        tv.add_bottom_bar(self.dual_bar)
        return tv

    # ------------------------------------------------------------------
    # Onglets et panneaux
    # ------------------------------------------------------------------
    @property
    def workspace(self) -> Optional[Workspace]:
        tp = self.tab_view.get_selected_page()
        return tp.get_child() if tp else None

    @property
    def pane(self) -> Optional[Pane]:
        ws = self.workspace
        return ws.active if ws else None

    def all_panes(self):
        for i in range(self.tab_view.get_n_pages()):
            yield from self.tab_view.get_nth_page(i).get_child().panes

    def new_tab(self, location: Optional[str] = None, dual: bool = False, select: bool = True) -> Workspace:
        loc = location or (self.pane.current_dir() if self.pane else "") or os.path.expanduser("~")
        ws = Workspace(self, loc, dual=dual and features.enabled("dual_panel"),
                       view_mode=self.pane.view_mode if self.pane else None)
        ws.connect("active-changed", lambda w: w is self.workspace and self._on_location(w.active))
        tp = self.tab_view.append(ws)
        self._update_tab(ws)
        if select:
            self.tab_view.set_selected_page(tp)
            GLib.idle_add(ws.active.focus_view)
        return ws

    def _on_pane_activated(self, pane, rec):
        """Double-clic (ou Entrée) sur un fichier qui fait partie d'une sélection de plusieurs
        fichiers : comme Nautilus, toute la sélection s'ouvre (un seul lecteur pour 6 vidéos)."""
        files = [r for r in pane.selected_recs() if not r.is_dir]
        if pane is self.pane and len(files) > 1 and any(r.name == rec.name for r in files):
            self.open_selected()
        else:
            self._on_activated(pane, rec)

    def connect_pane(self, pane: Pane, ws: Workspace):
        pane.connect("location-changed", lambda p: self._on_location(p))
        pane.connect("sort-changed", lambda p: p is self.pane and self._sync_sort_actions())
        pane.connect("selection-changed", lambda p: self._on_selection(p))
        pane.connect("activated", self._on_pane_activated)
        pane.connect("context-menu", self._on_context_menu)
        pane.connect("open-in-tab", lambda p, rec: self.new_tab(rec.path or rec.uri, select=False))
        pane.connect("files-dropped", self._on_pane_drop)
        pane.connect("message", lambda p, msg: toast(self, msg))

    def _update_tab(self, ws: Workspace):
        tp = self.tab_view.get_page(ws)
        if not tp:
            return
        p = ws.active
        tp.set_title(shown(p.title()))
        tp.set_tooltip(shown(p.location_text()))
        icon = {"archive": "package-x-generic-symbolic", "search": "system-search-symbolic",
                "trash": "user-trash-symbolic", "remote": "folder-remote-symbolic"}.get(p.mode, "folder-symbolic")
        tp.set_icon(Gio.ThemedIcon.new(icon))

    def _on_close_page(self, view, tp):
        ws = tp.get_child()
        for p in ws.panes:
            p.retire()
        view.close_page_finish(tp, True)
        return True

    def _check_empty(self):
        if self.tab_view.get_n_pages() == 0:
            self.close()
        return False

    def _on_create_window(self, _view):
        win = MainWindow(self.get_application())
        tv = win.tab_view
        # la nouvelle fenêtre reçoit l'onglet détaché ; on retire son onglet initial ensuite
        first = tv.get_nth_page(0)
        GLib.idle_add(lambda: (tv.get_n_pages() > 1 and tv.close_page(first), False)[1])
        win.present()
        return tv

    def _on_tab_switch(self):
        ws = self.workspace
        if ws is None:
            return
        self.search_entry.set_text("")
        self._search_origin = None
        self._on_location(ws.active)
        self._sync_dual()

    # ------------------------------------------------------------------
    # États
    # ------------------------------------------------------------------
    def _on_location(self, pane: Pane):
        ws = self._workspace_of(pane)
        if ws is not None:
            self._update_tab(ws)
        if getattr(pane, "pathbar", None) is not None:
            self.update_pane_pathbar(pane)
        if pane is not self.pane:
            return
        origin = getattr(self, "_search_origin", None)
        if origin is not None and pane.mode != "search" and pane.location != origin \
                and self.search_bar.get_search_mode():
            # un résultat (ou un autre dossier) ouvert : la recherche est finie, comme dans Nautilus ;
            # sinon le filtre restait posé sur le nouveau dossier (« include » seul dans /usr)
            self.search_bar.set_search_mode(False)
        archive_root = pane.archive_path if pane.mode == "archive" else ""
        loc = pane.location if pane.mode not in ("archive", "search") else pane.location_text()
        self.pathbar.set_location(loc, pane.location_text() if pane.mode != "trash" else TRASH_URI, archive_root)
        admin = pane.location.startswith("admin:")
        self.admin_banner.set_revealed(admin)
        self.set_title(shown(f"{pane.title()} — {APP_NAME}") + (" — " + tr("Administrateur") if admin else ""))
        self.sidebar.select(pane.location if pane.mode != "archive" else pane.archive_path)
        self._sync_view_button()
        self._sync_sort_actions()
        self._on_selection(pane)

    def _sync_sort_actions(self):
        """Menus « Trier » : le tri du dossier du panneau actif (chaque dossier a le sien)."""
        model = self.pane.model if self.pane is not None else None
        if model is None:
            return
        a = self.lookup_action("sort-by")
        if a:
            a.set_state(GLib.Variant.new_string(model.sort_key))
        a = self.lookup_action("sort-reverse")
        if a:
            a.set_state(GLib.Variant.new_boolean(model.sort_reverse))

    def update_pane_pathbar(self, pane: Pane):
        bar = getattr(pane, "pathbar", None)
        if bar is None:
            return
        archive_root = pane.archive_path if pane.mode == "archive" else ""
        bar.set_location(pane.location, pane.location_text() if pane.mode != "trash" else TRASH_URI, archive_root)

    def _workspace_of(self, pane: Pane) -> Optional[Workspace]:
        w = pane.get_parent()
        while w is not None and not isinstance(w, Workspace):
            w = w.get_parent()
        return w

    def _on_selection(self, pane: Pane):
        if pane is not self.pane:
            return
        self._update_sensitivity()
        self._update_dual_bar()
        if self.preview is not None and self.preview.get_visible():
            self.preview.show_selection(pane)
        self._quick_look_follow(pane)

    def _sync_view_button(self):
        p = self.pane
        mode = p.view_mode if p else settings["view_mode"]
        icon = {"list": "view-list-symbolic", "grid": "view-grid-symbolic"}.get(mode, COLUMNS_ICON)
        self.view_btn.set_icon_name(icon)
        self.columns_btn.handler_block(self._columns_btn_id)
        self.columns_btn.set_active(mode == "columns")
        self.columns_btn.handler_unblock(self._columns_btn_id)
        a = self.lookup_action("view-mode")
        if a:
            a.set_state(GLib.Variant.new_string(mode))

    def _sync_dual(self):
        ws = self.workspace
        dual = bool(ws and ws.dual)
        a = self.lookup_action("dual")
        if a:
            a.set_state(GLib.Variant.new_boolean(dual))
        self.dual_bar.set_revealed(dual)
        self._update_dual_bar()

    def _update_dual_bar(self):
        ws = self.workspace
        if not ws or not ws.dual:
            return
        left = ws.active is ws.panes[0]
        self.copy_other_btn.set_label(tr("Copier →") if left else tr("← Copier"))
        self.move_other_btn.set_label(tr("Déplacer →") if left else tr("← Déplacer"))
        sel = ws.active.selected_recs()
        from ..backend import formats, media
        self.dual_extract_btn.set_visible(bool(sel) and features.available("archives") and
                                          all(not r.is_dir and formats.looks_like_archive(r.name) for r in sel))
        self.dual_audio_btn.set_visible(bool(sel) and features.available("video_to_audio") and
                                        all(not r.is_dir and media.is_video(r.name) for r in sel))

    def _on_setting(self, key, _v):
        if key == "view_mode":
            self._sync_view_button()
        elif key == "show_hidden":
            a = self.lookup_action("show-hidden")
            if a:
                a.set_state(GLib.Variant.new_boolean(settings["show_hidden"]))
        elif key == "list_columns":
            for col in ("size", "type", "date", "perms", "owner", "duration"):
                a = self.lookup_action("column-" + col)
                if a:
                    a.set_state(GLib.Variant.new_boolean(col in settings["list_columns"]))
        elif key == "features":
            self._apply_features()
        elif key == "type_ahead":
            self.search_bar.set_key_capture_widget(None if settings["type_ahead"] else self)
            for p in self.all_panes():
                p.type_ahead_cancel()
        elif key in ("sidebar_desktop", "sidebar_this_pc"):
            self.sidebar.fill_places()
            self.fit_sidebar()

    def fit_sidebar(self):
        """Barre latérale assez large pour ses emplacements fixes dans la langue choisie
        (« Autres emplacements » en français…), sans dépasser SIDEBAR_MAX."""
        need = min(self.sidebar.fixed_width() + 8, SIDEBAR_MAX)     # + barre de défilement superposée
        self.split.set_min_sidebar_width(max(200, need))
        self.split.set_max_sidebar_width(max(280, need))

    def _apply_features(self):
        self.dual_btn.set_visible(features.enabled("dual_panel"))
        self.columns_btn.set_visible(features.enabled("column_view"))
        self.preview_btn.set_visible(features.enabled("preview"))
        self.content_search_btn.set_visible(features.available("content_search"))
        if not features.enabled("dual_panel"):
            for i in range(self.tab_view.get_n_pages()):
                self.tab_view.get_nth_page(i).get_child().set_dual(False)
            self._sync_dual()
        if not features.enabled("preview") and self.preview is not None:
            self.set_preview(False)
        self.view_btn.set_menu_model(menus.view_menu())
        self._update_sensitivity()

    def _update_sensitivity(self):
        p = self.pane
        if not p:
            return
        sel = p.selected_recs()
        # emplacements GVfs (admin://, smb://…) : par le chemin local que GVfs expose, sinon par GIO
        gvfs = p.mode == "remote" and not p.location.startswith(("network:", "recent:"))
        fs = p.mode in ("fs", "search") or gvfs
        local_sel = [r for r in sel if r.path or (gvfs and r.uri)]
        has_dir = (bool(p.current_dir()) and p.mode == "fs") or gvfs
        can = {
            "back": bool(p.back_stack), "forward": bool(p.forward_stack), "up": p.can_go_up(),
            "cut": fs and bool(local_sel), "copy": fs and bool(local_sel),
            "paste": has_dir, "rename": (len(sel) == 1 and (fs or p.mode == "archive"))
            or (len(sel) > 1 and p.mode in ("fs", "search") and all(r.path for r in sel)),
            "trash": fs and bool(local_sel), "delete": bool(sel) and (p.mode in ("fs", "search", "trash") or gvfs),
            "new-folder": has_dir or p.mode == "archive", "new-file": has_dir, "new-empty-file": has_dir,
            "copy-to-other": fs and bool(local_sel), "move-to-other": fs and bool(local_sel),
            "restore": p.mode == "trash" and bool(sel), "properties": bool(sel) or bool(p.current_dir()),
            "extract-all": p.mode == "archive" and p.listing is not None,
            "archive-info": p.mode == "archive" and p.listing is not None,
            "archive-test": p.mode == "archive" and p.listing is not None,
            "archive-add": p.mode == "archive" and p.listing is not None,
            "close-archive": p.mode == "archive",
            "open-admin": len(local_sel) == 1 and not self.is_admin(),
            "open-admin-folder": bool(p.current_dir()) and p.mode == "fs",
        }
        for name, ok in can.items():
            a = self.lookup_action(name)
            if a:
                a.set_enabled(ok)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def open_location(self, location: str, where: str = "here"):
        if where == "tab":
            self.new_tab(location)
            return
        if where == "window":
            self.get_application()._new_window(location)
            return
        if where == "other":
            ws = self.workspace
            if ws and not ws.dual and features.enabled("dual_panel"):
                ws.set_dual(True)
                self._sync_dual()
            other = ws.other() if ws else None
            if other is not None:
                other.open(location)
                return
        p = self.pane
        if p:
            p.open(location)
            p.focus_view()

    def leave_location(self, root: str):
        """Quitte un périphérique dans tous les panneaux avant de l'éjecter."""
        for p in self.all_panes():
            cur = p.archive_path if p.mode == "archive" else p.location
            if cur == root or cur.startswith(root.rstrip("/") + "/"):
                p.open(os.path.expanduser("~"))

    def _on_sidebar_open(self, _s, target: str, where: str):
        self.open_location(target, where)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_path_navigate(self, _bar, loc: str):
        if loc:
            self.open_location(loc)
        elif self.pane:
            self.pane.focus_view()

    def _on_recursive_toggled(self, btn):
        settings["search_recursive"] = btn.get_active()
        self._run_search(force=True)

    def _on_search_changed(self, entry):
        if self.recursive_btn.get_active():
            if getattr(self, "_search_id", 0):
                GLib.source_remove(self._search_id)
            self._search_id = GLib.timeout_add(350, self._run_search)
        else:
            self._run_search()

    def _run_search(self, force: bool = False):
        self._search_id = 0
        p = self.pane
        if p is not None:
            text = self.search_entry.get_text()
            if text.strip() and getattr(self, "_search_origin", None) is None:
                # dossier où la recherche a commencé : en ouvrir un autre la termine (comme Nautilus)
                self._search_origin = p.search_root if p.mode == "search" else p.location
            if self.recursive_btn.get_active() and p.mode in ("fs", "search"):
                if text.strip():
                    p.start_search(text, recursive=True)
                elif p.mode == "search":
                    p.open(p.search_root, push=False)
            else:
                p.start_search(text, recursive=False)
                p.select_best(text)
        return False

    def _on_search_key(self, _c, keyval, _code, state):
        if keyval not in (Gdk.KEY_Down, Gdk.KEY_Up, Gdk.KEY_KP_Down, Gdk.KEY_KP_Up):
            return False
        p = self.pane
        if p is None or not p.model.items:
            return False
        if not p.selected_recs():
            p.select_best(self.search_entry.get_text()) if self.search_entry.get_text().strip() else None
        p.focus_view()
        return True

    def _on_search_activate(self, *_):
        """Entrée dans le champ de recherche : ouvre le résultat sélectionné (le meilleur, d'office),
        comme Nautilus. Recherche dans les sous-dossiers pas encore lancée : on la lance d'abord."""
        if getattr(self, "_search_id", 0):
            GLib.source_remove(self._search_id)
            self._run_search(force=True)
            return
        p = self.pane
        if p is not None and self.search_entry.get_text().strip() and p.selected_recs():
            self.open_selected()
        else:
            self._run_search(force=True)

    def _on_search_mode(self, bar, *_):
        if not bar.get_search_mode():
            self._search_origin = None
            p = self.pane
            self.search_entry.set_text("")
            if p is not None:
                if p.mode == "search":
                    p.open(p.search_root, push=False)
                p.set_filter("")
                p.focus_view()

    def _on_close(self, *_):
        if self.saves_state:
            w, h = self.get_default_size()
            settings.data["window_width"], settings.data["window_height"] = w, h
            settings.data["sidebar_visible"] = self.split.get_show_sidebar()
            ws = self.workspace
            settings.data["dual_panel"] = bool(ws and ws.dual)
            settings.save()
        from ..backend.media import durations
        durations.flush()
        running = [j for j in manager.running]
        if running and len(self.get_application().get_windows()) <= 1:
            from .common import alert

            def resp(r):
                if r == "quit":
                    for j in manager.running:
                        j.cancel()
                    self.destroy()
            alert(self, tr("Des opérations sont en cours"), tr("Quitter maintenant les annulera."),
                  responses=(("cancel", tr("Continuer")), ("quit", tr("Quitter"))), destructive="quit",
                  default="cancel", callback=resp)
            return True
        return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _setup_actions(self):
        simple = {
            "new-tab": lambda: self.new_tab(), "close-tab": self._close_tab,
            "new-window": lambda: MainWindow(self.get_application(),
                                             self.pane.current_dir() if self.pane else None).present(),
            "back": lambda: self.pane.go_back(), "forward": lambda: self.pane.go_forward(),
            "up": lambda: self.pane.go_up(), "refresh": lambda: self.pane.refresh(),
            "home": lambda: self.open_location(os.path.expanduser("~")),
            "location": lambda: self.pathbar.edit(), "location-root": lambda: self.pathbar.edit("/"),
            "location-home": lambda: self.pathbar.edit("~/"),
            "search": self._toggle_search, "select-all": lambda: self.pane.select_all(),
            "unselect-all": lambda: self.pane.unselect_all(),
            "invert-selection": lambda: self.pane.invert_selection(),
            "toggle-sidebar": lambda: self.sidebar_btn.set_active(not self.sidebar_btn.get_active()),
            "zoom-in": lambda: self._zoom(1), "zoom-out": lambda: self._zoom(-1), "zoom-normal": self._zoom_normal,
            "toggle-view": self._toggle_view, "switch-pane": self._switch_pane,
            "toggle-columns": self._toggle_columns,
            "menu": self._show_menu, "shortcuts": self._shortcuts,
        }
        for name, cb in simple.items():
            self._add(name, cb)
        self._add_stateful("view-mode", GLib.Variant.new_string(settings["view_mode"]), self._set_view_mode)
        self._add_stateful("sort-by", GLib.Variant.new_string(settings["sort_key"]),
                           lambda v: self.pane.set_sort(v.get_string(), self.pane.model.sort_reverse))
        self._add_toggle("sort-reverse", settings["sort_reverse"],
                         lambda v: self.pane.set_sort(self.pane.model.sort_key, v))
        self._add_toggle("show-hidden", settings["show_hidden"], lambda v: settings.__setitem__("show_hidden", v))
        self._add_toggle("dual", False, self._set_dual)
        self._add_toggle("preview", settings["preview_visible"] and features.enabled("preview"), self.set_preview)
        for col in ("size", "type", "date", "perms", "owner", "duration"):
            self._add_toggle("column-" + col, col in settings["list_columns"],
                             lambda v, c=col: self._set_column(c, v))
        self.setup_file_actions()
        self.setup_archive_actions()
        self.setup_tool_actions()

        app = self.get_application()
        accels = {
            "win.new-tab": ["<Ctrl>t"], "win.close-tab": ["<Ctrl>w"], "win.new-window": ["<Ctrl>n"],
            "win.back": ["<Alt>Left", "BackSpace", "Back"], "win.forward": ["<Alt>Right", "Forward"],
            "win.up": ["<Alt>Up"], "win.refresh": ["F5", "<Ctrl>r"], "win.home": ["<Alt>Home"],
            "win.location": ["<Ctrl>l"], "win.location-root": ["slash"], "win.location-home": ["asciitilde"],
            "win.search": ["<Ctrl>f"], "win.show-hidden": ["<Ctrl>h"],
            "win.select-all": ["<Ctrl>a"], "win.unselect-all": ["<Ctrl><Shift>a"],
            "win.invert-selection": ["<Ctrl><Shift>i"], "win.toggle-sidebar": ["<Ctrl>F9"],
            "win.zoom-in": ["<Ctrl>plus", "<Ctrl>equal", "<Ctrl>KP_Add"],
            "win.zoom-out": ["<Ctrl>minus", "<Ctrl>KP_Subtract"], "win.zoom-normal": ["<Ctrl>0", "<Ctrl>KP_0"],
            "win.view-mode::list": ["<Ctrl>1"], "win.view-mode::grid": ["<Ctrl>2"],
            "win.view-mode::columns": ["<Ctrl>3"], "win.toggle-columns": ["F9"],
            "win.dual": ["F3"], "win.preview": ["F4"], "win.switch-pane": ["<Ctrl>Tab"],
            "win.menu": ["F10"], "win.shortcuts": ["<Ctrl>question", "<Ctrl>F1"],
        }
        accels.update(self.file_accels())
        accels.update(self.archive_accels())
        accels.update(self.tool_accels())
        for action, keys in accels.items():
            app.set_accels_for_action(action, keys)
        self._apply_features()

    # Raccourcis qui ont aussi un sens dans un champ de saisie (recherche, barre d'emplacement…).
    # GTK 4 traite les raccourcis de l'application AVANT le widget qui a le focus : sans ce renvoi,
    # Suppr dans la recherche mettait le fichier sélectionné à la corbeille, « / » relançait la
    # saisie d'emplacement, Retour arrière revenait au dossier précédent, Ctrl+Z annulait une
    # opération sur les fichiers… Quand un texte a le focus, la touche lui revient.
    _TEXT_KEYS = {
        "back": ("signal", "backspace"),
        "trash": ("signal", "delete-from-cursor", Gtk.DeleteType.CHARS, 1),
        "delete": ("signal", "cut-clipboard"),                       # Maj+Suppr = couper
        "location-root": ("signal", "insert-at-cursor", "/"),
        "location-home": ("signal", "insert-at-cursor", "~"),
        "cut": ("signal", "cut-clipboard"), "copy": ("signal", "copy-clipboard"),
        "paste": ("signal", "paste-clipboard"),
        "select-all": ("action", "selection.select-all"), "unselect-all": ("unselect",),
        "undo": ("action", "text.undo"), "redo": ("action", "text.redo"),
    }

    def _text_focus(self) -> Optional[Gtk.Widget]:
        focus = self.get_focus()
        return focus if isinstance(focus, (Gtk.Text, Gtk.TextView)) and focus.is_sensitive() else None

    def _to_text(self, name: str) -> bool:
        """Renvoie la touche au champ de saisie qui a le focus ; False s'il n'y en a pas."""
        what = self._TEXT_KEYS.get(name)
        text = self._text_focus() if what else None
        if text is None:
            return False
        if what[0] == "signal":
            if what[1] == "insert-at-cursor":    # une frappe remplace la sélection
                if isinstance(text, Gtk.Text):
                    text.delete_selection()
                else:
                    text.get_buffer().delete_selection(True, True)
            text.emit(what[1], *what[2:])
        elif what[0] == "action":
            text.activate_action(what[1], None)
        elif isinstance(text, Gtk.Text):
            pos = text.get_position()
            text.select_region(pos, pos)
        return True

    # (touche, Ctrl, Maj) → action dont la touche revient au texte qui a le focus. Interceptée au clavier
    # et non dans l'action : un clic sur « Précédent » pendant que la recherche a le focus doit naviguer.
    _TEXT_KEYMAP = {
        ("BackSpace", False, False): "back", ("Delete", False, False): "trash",
        ("KP_Delete", False, False): "trash", ("Delete", False, True): "delete",
        ("slash", False, False): "location-root", ("asciitilde", False, False): "location-home",
        ("a", True, False): "select-all", ("A", True, True): "unselect-all", ("a", True, True): "unselect-all",
        ("x", True, False): "cut", ("c", True, False): "copy", ("v", True, False): "paste",
        ("z", True, False): "undo", ("Z", True, True): "redo", ("z", True, True): "redo",
    }

    def _on_type_ahead(self, _ctrl, keyval, _code, state):
        """Réglage « taper pour aller au fichier » : les lettres tapées dans la vue des fichiers
        sélectionnent le premier élément qui commence ainsi (Pane.type_ahead)."""
        if keyval == Gdk.KEY_space and self._quick_look(state):
            return True
        if not settings["type_ahead"]:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        p = self.pane
        focus = self.get_focus()
        # seulement dans la vue des fichiers : pas dans un champ, un menu, la barre latérale…
        if p is None or not (p.lost_focus(focus) or focus.is_ancestor(p) and not isinstance(focus, Gtk.Editable)) \
                or p.mode not in ("fs", "search", "trash", "remote", "archive") or p.view_mode == "columns":
            return False
        if p.type_ahead_active and keyval == Gdk.KEY_Escape:
            p.type_ahead_cancel()
            return True
        if p.type_ahead_active and keyval == Gdk.KEY_BackSpace:
            p.type_ahead_back()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            p.type_ahead_cancel()           # Entrée ouvre l'élément choisi (la vue s'en charge)
            return False
        code = Gdk.keyval_to_unicode(keyval)
        ch = chr(code) if code else ""
        if not ch or not ch.isprintable() or (ch == " " and not p.type_ahead_active):
            return False
        if ch == "/" or (ch == "~" and not p.type_ahead_active):
            return False                    # raccourcis « saisir un emplacement » (/, ~) gardés
        if focus is None or not focus.is_ancestor(p.active_view):
            # focus resté ailleurs dans le panneau (ligne d'une page Autres emplacements devenue
            # invisible…) : Entrée l'aurait réactivée au lieu d'ouvrir l'élément trouvé
            p.focus_view()
        p.type_ahead(ch)
        return True

    def _quick_look(self, state) -> bool:
        """Espace dans la vue des fichiers : aperçu rapide par Sushi, comme Nautilus (Espace referme)."""
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
                    | Gdk.ModifierType.SHIFT_MASK):
            return False
        p = self.pane
        focus = self.get_focus()
        if p is None or p.type_ahead_active or not features.available("quick_look") \
                or not (p.lost_focus(focus) or focus.is_ancestor(p) and not isinstance(focus, Gtk.Editable)):
            return False
        sel = p.selected_recs()
        if not sel:
            return False
        sushi.previewer.on_selection_event = self._on_quick_look_move
        sushi.previewer.show(self, sel[0].gfile.get_uri(), True)
        return True

    def _quick_look_follow(self, pane):
        """Aperçu ouvert par cette fenêtre : il suit la sélection, que le clavier soit dans Sushi (flèches →
        SelectionEvent) ou resté dans Odysseus (Sushi ne prend pas toujours le focus sous X11)."""
        if not features.enabled("quick_look") or sushi.previewer.owner is not self:
            return
        sel = pane.selected_recs()
        if sel and sushi.previewer.visible():
            sushi.previewer.show(self, sel[0].gfile.get_uri(), False)

    def _on_quick_look_move(self, direction: int):
        """Flèches tapées dans Sushi (GtkDirectionType) : la sélection d'Odysseus avance ou recule."""
        if sushi.previewer.owner is not self:
            return
        p = self.pane
        if p is not None:
            p.move_selection({Gtk.DirectionType.UP: "up", Gtk.DirectionType.DOWN: "down",
                              Gtk.DirectionType.LEFT: "left", Gtk.DirectionType.RIGHT: "right",
                              Gtk.DirectionType.TAB_FORWARD: "down", Gtk.DirectionType.TAB_BACKWARD: "up"}
                             .get(Gtk.DirectionType(direction), "down"))

    def _on_text_key(self, _ctrl, keyval, _code, state):
        if state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        key = Gdk.keyval_name(keyval) or ""
        # « / » et « ~ » se tapent avec Maj (ou AltGr) en AZERTY / QWERTZ, et le raccourci se déclenche
        # quand même : pour ces caractères, Maj ne compte pas (sinon « /usr » devenait « usr »)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK) and key not in ("slash", "asciitilde")
        name = self._TEXT_KEYMAP.get((key, bool(state & Gdk.ModifierType.CONTROL_MASK), shift))
        return bool(name) and self._to_text(name)

    def _add(self, name, cb, param: Optional[str] = None):
        a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
        if param:
            a.connect("activate", lambda _a, v, cb=cb: cb(v.unpack()))
        else:
            a.connect("activate", lambda _a, _p, cb=cb: cb())
        self.add_action(a)
        return a

    def _add_stateful(self, name, state: GLib.Variant, cb):
        a = Gio.SimpleAction.new_stateful(name, state.get_type(), state)

        def act(action, v):
            action.set_state(v)
            cb(v)
        a.connect("activate", act)
        self.add_action(a)

    def _add_toggle(self, name, initial: bool, cb):
        a = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(bool(initial)))

        def act(action, _p):
            v = not action.get_state().get_boolean()
            action.set_state(GLib.Variant.new_boolean(v))
            cb(v)
        a.connect("activate", act)
        self.add_action(a)

    def _toggle_search(self):
        self.search_btn.set_active(True)
        self.search_entry.grab_focus()

    def _set_view_mode(self, v: GLib.Variant):
        if self.pane:
            self.pane.set_view_mode(v.get_string())
        self._sync_view_button()

    def _toggle_view(self):
        p = self.pane
        if p:
            p.set_view_mode("grid" if p.view_mode != "grid" else "list")
            self._sync_view_button()

    def _set_column(self, col: str, visible: bool):
        cols = [c for c in settings["list_columns"] if c != col]
        if visible:
            cols.append(col)
        settings["list_columns"] = cols

    def _set_dual(self, value: bool):
        ws = self.workspace
        if ws is None:
            return
        if value and not features.enabled("dual_panel"):
            value = False
        ws.set_dual(value)
        settings["dual_panel"] = value
        self._sync_dual()
        self._on_location(ws.active)

    def _switch_pane(self):
        ws = self.workspace
        if ws and ws.dual:
            other = ws.other()
            ws.set_active(other)
            other.focus_view()
        elif self.tab_view.get_n_pages() > 1:
            if not self.tab_view.select_next_page():
                self.tab_view.set_selected_page(self.tab_view.get_nth_page(0))

    def _on_mouse_nav(self, controller, _event):
        # Événement brut (le paramètre n'est pas utilisable depuis Python : get_current_event).
        # Les boutons 8/9 ne sont jamais transmis aux widgets ; voir _release_pointer_grab.
        event = controller.get_current_event()
        if event is None or event.get_event_type() not in (Gdk.EventType.BUTTON_PRESS,
                                                            Gdk.EventType.BUTTON_RELEASE):
            return False
        button = event.get_button()
        if button not in (8, 9):
            return False
        if event.get_event_type() == Gdk.EventType.BUTTON_PRESS:
            ok, x, y = event.get_position()
            native = self.get_native()
            if native is not None:
                sx, sy = native.get_surface_transform()
                x, y = x - sx, y - sy
            self._release_pointer_grab()
            GLib.idle_add(self._mouse_nav, button, x, y)
        return True

    def _release_pointer_grab(self):
        """Contourne un bug de GTK 4.14 (gtkmain.c, handle_pointing_event) : à l'appui, GTK verrouille
        le pointeur sur le widget visé et le marque « actif », mais au relâchement il ne lève ce
        verrou que pour les boutons 1 à 5. Avec les boutons 8/9, le verrou reste accroché au widget :
        les clics suivants lui sont envoyés, et quand la page change GTK retire l'état « actif » une
        seconde fois (« Broken accounting of active state »).
        Rendre la fenêtre elle-même un instant insensible fait faire à GTK le ménage complet
        (gtk_window_update_pointer_focus_on_state_change) pendant que l'état est encore cohérent :
        état « actif » retiré une seule fois, verrou et suivi du pointeur supprimés."""
        focus = self.get_focus()
        self.set_sensitive(False)
        self.set_sensitive(True)
        if focus is not None and focus.get_root() is self:
            focus.grab_focus()

    def _mouse_nav(self, button: int, x: float, y: float):
        # en double panneau : le panneau sous le pointeur, sinon le panneau actif
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and not isinstance(w, Pane):
            w = w.get_parent()
        pane = w or self.pane
        if pane is not None:
            if button == 8:
                pane.go_back()
            else:
                pane.go_forward()
        return False

    def _fill_main_menu(self):
        self.main_menu_btn.set_menu_model(menus.main_menu(self))
        # « Taille d'icône  −  + », comme dans Nautilus
        box = Gtk.Box(spacing=6, margin_start=34, margin_end=6)
        box.append(Gtk.Label(label=tr("Taille d'icône"), xalign=0, hexpand=True))
        for icon, action, tip in (("list-remove-symbolic", "win.zoom-out", tr("Réduire (Ctrl+−)")),
                                  ("list-add-symbolic", "win.zoom-in", tr("Agrandir (Ctrl++)"))):
            box.append(Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tip, css_classes=["flat"]))
        self.main_menu_btn.get_popover().add_child(box, "zoom")

    def _sync_start_controls(self):
        self.start_controls.set_visible(not self.split.get_show_sidebar() or self.split.get_collapsed())

    def _on_columns_btn(self, active: bool):
        p = self.pane
        if p is not None and (p.view_mode == "columns") != active:
            self._toggle_columns()
        else:
            self._sync_view_button()

    def _toggle_columns(self):
        """F9 (raccourci de Column Browser) : vue en colonnes, puis retour à la vue précédente."""
        p = self.pane
        if p is None or not features.enabled("column_view"):
            return
        if p.view_mode == "columns":
            p.set_view_mode(getattr(p, "_before_columns", "list"))
        else:
            p._before_columns = p.view_mode
            p.set_view_mode("columns")
        self._sync_view_button()

    def set_preview(self, value: bool):
        if value and not features.enabled("preview"):
            value = False
        if value and self.preview is None:
            from .preview import PreviewPanel
            self.preview = PreviewPanel(self)
            self.content_paned.set_end_child(self.preview)
            GLib.idle_add(self._place_preview)
        if self.preview is not None:
            self.preview.set_visible(value)
            if value and self.pane:
                self.preview.show_selection(self.pane)
        settings["preview_visible"] = value
        a = self.lookup_action("preview")
        if a:
            a.set_state(GLib.Variant.new_boolean(value))

    def _place_preview(self):
        w = self.content_paned.get_width()
        if w > 0:
            self.content_paned.set_position(max(300, w - settings["preview_width"]))
            self.content_paned.connect("notify::position", self._on_preview_resized)
        return False

    def _on_preview_resized(self, paned, *_):
        w = paned.get_width()
        if w > 0 and self.preview is not None and self.preview.get_visible():
            settings.data["preview_width"] = max(200, w - paned.get_position())

    def _zoom(self, step: int):
        grid = self.pane is not None and self.pane.view_mode == "grid"
        key, levels = ("grid_zoom", GRID_ZOOM_LEVELS) if grid else ("list_zoom", ZOOM_LEVELS)
        cur = settings[key] if settings[key] in levels else levels[0]
        i = max(0, min(len(levels) - 1, levels.index(cur) + step))
        settings[key] = levels[i]

    def _zoom_normal(self):
        if self.pane is not None and self.pane.view_mode == "grid":
            settings["grid_zoom"] = "medium"
        else:
            settings["list_zoom"] = "small"

    def _close_tab(self):
        tp = self.tab_view.get_selected_page()
        if tp and self.tab_view.get_n_pages() > 1:
            self.tab_view.close_page(tp)
        else:
            self.close()

    def _show_menu(self):
        p = self.pane
        if p is None:
            return
        sel = p.selected_recs()
        self._on_context_menu(p, 40.0, 40.0, bool(sel))

    def _shortcuts(self):
        from .dialogs import show_shortcuts
        show_shortcuts(self)

    # ------------------------------------------------------------------
    # Menu contextuel
    # ------------------------------------------------------------------
    def _on_context_menu(self, pane: Pane, x: float, y: float, on_item: bool):
        ws = self._workspace_of(pane)
        if ws is not None:
            ws.set_active(pane)
        model = menus.context_menu(self, pane, on_item)
        popup_menu(pane, model, x, y)

    def _on_pane_drop(self, pane: Pane, paths: list, dest: str, action: str):
        if action == "archive":
            self.add_to_open_archive(pane, paths)
            return
        self.drop_files(paths, dest, action)

    def bookmark_toggle(self):
        p = self.pane
        if p is None:
            return
        target = p.bookmark_target()
        added = bookmarks.toggle(target)
        toast(self, tr("Ajouté aux signets") if added else tr("Retiré des signets"))

    def is_dual(self) -> bool:
        ws = self.workspace
        return bool(ws and ws.dual)

    def other_pane(self) -> Optional[Pane]:
        ws = self.workspace
        return ws.other() if ws else None
