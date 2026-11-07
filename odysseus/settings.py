"""Préférences persistantes (JSON dans ~/.config/odysseus)."""

from __future__ import annotations

import json
import os
from typing import Any

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "odysseus")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "odysseus")

DEFAULTS: dict[str, Any] = {
    "theme": "system",              # system, light, dark
    "language": "system",           # system, fr, en, de (appliqué au prochain démarrage)
    # Affichage
    "show_hidden": False,
    "folders_first": True,
    "view_mode": "list",            # list, grid ou columns (vue par défaut des nouveaux panneaux)
    "list_zoom": "small",           # small (16 px, compact), medium (32 px), large (48 px) — comme Nautilus
    "grid_zoom": "medium",          # small 64, medium 96, large 128, larger 256 px
    "sort_key": "name",             # name, size, type, date, duration
    "sort_reverse": False,
    "list_columns": ["size", "type", "date"],   # colonnes facultatives visibles (+ perms, owner, duration)
    "column_order": ["size", "type", "date", "perms", "owner", "duration"],   # ordre (sélecteur de colonnes)
    "single_click": False,
    "dnd_hover_open": True,         # glisser au-dessus d'un dossier l'ouvre après un temps (Nautilus)
    "type_ahead": False,            # taper au clavier : aller au fichier (Nemo, Thunar) au lieu de rechercher
    "thumbnails": "local",          # always, local, never
    "search_recursive": False,
    # Fenêtre
    "window_width": 1180,
    "window_height": 760,
    "sidebar_visible": True,
    "preview_visible": False,
    "preview_width": 340,
    "dual_panel": False,
    "archive_window": True,          # double-clic sur une archive : fenêtre à part (Archive Browser)
    "sidebar_desktop": False,       # Bureau dans la barre latérale
    "sidebar_this_pc": False,       # « Ce PC » dans la barre latérale (dossiers et disques, comme Windows)
    "this_pc_folders": True,        # « Ce PC » : dossiers de l'utilisateur au-dessus des disques
    "sidebar_trash_count": True,    # nombre d'éléments à côté de « Corbeille »
    # Comportement
    "startup_location": "home",     # ouverture sans dossier précis : home, last (dernier emplacement), folder
    "startup_folder": "",           # dossier choisi (startup_location = folder)
    "last_locations": [],           # dernier emplacement : [gauche, droite] (droite en double panneau)
    "last_active": 0,               # panneau actif de ce dernier emplacement
    "confirm_trash": False,
    "confirm_delete": True,
    "terminal": "",                 # vide = détection automatique
    "text_editor": "",              # vide = détection automatique
    "dbus_file_manager": False,     # répondre à « Afficher dans le dossier » (org.freedesktop.FileManager1)
    "text_editor_exts": "py,sh,txt,md,json,yml,yaml,conf,cfg,ini,toml,xml,css,js,log",
    "hidden_dim_mode": "icon",      # icon (icône seule) ou all (icône et nom)
    "progress_window_delay": 0.5,   # secondes avant d'afficher la fenêtre de progression
    "features": {},                 # fonctionnalités activées / désactivées (voir features.py)
    # Archives
    "create_format": "7z",
    "create_level": 3,
    "create_solid": True,
    "create_encrypt_names": True,
    "extract_subfolder": "smart",
    "extract_overwrite": "rename",
    "open_after_extract": False,
    "remember_passwords": True,
    # Outils
    "pdf_compress_level": "/ebook",
    "audio_format": "mp3",
    "audio_quality": "high",
    # outils image : derniers réglages
    "image_resize": "scale", "image_scale": 50, "image_width": 1920, "image_height": 1080,
    "image_no_enlarge": True, "image_rotate": 90, "image_format": "jpg", "image_quality": 90,
    "image_replace": False,
}


def _fits(key, value) -> bool:
    """Valeur lue du fichier gardée seulement si elle a le type de la valeur par défaut : un
    « "features": null » ou « "window_width": "1180" » (fichier retouché, vieille version)
    empêchait la fenêtre de s'ouvrir. Clé inconnue : gardée telle quelle."""
    if key not in DEFAULTS:
        return True
    default = DEFAULTS[key]
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list) and default and all(isinstance(x, str) for x in default):
        # listes de noms (colonnes…) : des éléments d'un autre type empêchaient l'ouverture
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self.data = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if _fits(k, v)})
        except (OSError, ValueError):
            pass
        self._listeners = []

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()
        for cb in list(self._listeners):
            cb(key, value)

    def connect(self, cb):
        self._listeners.append(cb)

    def disconnect(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


settings = Settings()

# Mots de passe d'archives mémorisés pendant la session uniquement (jamais écrits sur disque).
session_passwords: dict[str, str] = {}
