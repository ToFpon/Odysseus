"""Signets GTK (~/.config/gtk-3.0/bookmarks), partagés avec Nautilus et les sélecteurs de fichiers."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from typing import Optional

BOOKMARKS_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                              "gtk-3.0", "bookmarks")


@dataclass
class Bookmark:
    uri: str
    label: str = ""

    @property
    def path(self) -> Optional[str]:
        if self.uri.startswith("file://"):
            return urllib.parse.unquote(self.uri[7:])
        return None

    @property
    def title(self) -> str:
        if self.label:
            return self.label
        p = self.path
        if p:
            return os.path.basename(p.rstrip("/")) or p
        return urllib.parse.unquote(self.uri.split("://", 1)[-1].rstrip("/").rsplit("/", 1)[-1]) or self.uri


def to_uri(path_or_uri: str) -> str:
    if "://" in path_or_uri:
        return path_or_uri
    return "file://" + urllib.parse.quote(os.path.abspath(path_or_uri))


def load(path: str = BOOKMARKS_FILE) -> list[Bookmark]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                uri, _, label = line.partition(" ")
                out.append(Bookmark(uri, label.strip()))
    except OSError:
        pass
    return out


def save(items: list[Bookmark], path: str = BOOKMARKS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for b in items:
            fh.write(b.uri + (" " + b.label if b.label else "") + "\n")
    os.replace(tmp, path)


def contains(target: str, path: str = BOOKMARKS_FILE) -> bool:
    uri = to_uri(target)
    return any(b.uri == uri for b in load(path))


def toggle(target: str, path: str = BOOKMARKS_FILE) -> bool:
    """Ajoute ou retire un signet ; renvoie True s'il a été ajouté."""
    uri = to_uri(target)
    items = load(path)
    if any(b.uri == uri for b in items):
        save([b for b in items if b.uri != uri], path)
        return False
    items.append(Bookmark(uri))
    save(items, path)
    return True


def rename(target: str, label: str, path: str = BOOKMARKS_FILE):
    uri = to_uri(target)
    items = load(path)
    for b in items:
        if b.uri == uri:
            b.label = label.strip()
    save(items, path)


def move(target: str, index: int, path: str = BOOKMARKS_FILE):
    uri = to_uri(target)
    items = load(path)
    cur = [b for b in items if b.uri == uri]
    if not cur:
        return
    items = [b for b in items if b.uri != uri]
    items.insert(max(0, min(index, len(items))), cur[0])
    save(items, path)
