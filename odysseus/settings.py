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
    "single_click": False,
    "thumbnails": "local",          # always, local, never
    "search_recursive": False,
    # Fenêtre
    "window_width": 1180,
    "window_height": 760,
    "sidebar_visible": True,
    "preview_visible": False,
    "preview_width": 340,
    "dual_panel": False,
    "sidebar_desktop": False,       # Bureau dans la barre latérale
    "sidebar_this_pc": False,       # « Ce PC » dans la barre latérale (dossiers et disques, comme Windows)
    # Comportement
    "confirm_trash": False,
    "confirm_delete": True,
    "terminal": "",                 # vide = détection automatique
    "text_editor": "",              # vide = détection automatique
    "dbus_file_manager": False,     # répondre à « Afficher dans le dossier » (org.freedesktop.FileManager1)
    "text_editor_exts": "py,sh,txt,md,json,yml,yaml,conf,cfg,ini,toml,xml,css,js,log",
    "hidden_dim_mode": "icon",      # icon (icône seule) ou all (icône et nom)
    "progress_window_delay": 0.5,   # secondes avant d'afficher la fenêtre de progression
    "features": {},                 # fonctionnalités activées / désactivées (voir features.py)
    # Archives (moteur Pressoir)
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
}


class Settings:
    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self.data = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
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
