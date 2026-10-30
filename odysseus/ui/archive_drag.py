"""Glisser hors d'une archive (comme Archive Browser) : les éléments sélectionnés sont extraits d'avance,
en arrière-plan, dans un cache. Au début d'un glisser, GTK veut les fichiers tout de suite : une
extraction à ce moment figeait la fenêtre (et au-delà de 300 Mo, rien ne se passait, sans message).

Pas encore prêt : une petite sélection est extraite sur le moment ; une grosse est préparée et un message
le dit. Les fichiers déjà glissés restent (Nautilus peut encore copier depuis eux après la fermeture de
l'archive) jusqu'au ménage du démarrage suivant (archive.sweep_drag_caches)."""

from __future__ import annotations

import os
import shutil
import threading
from typing import Callable, Optional

from gi.repository import GLib

from ..backend import archive
from ..backend.engine import Job
from ..i18n import tr

# (chemin dans l'archive, dossier ?, taille connue)
Item = tuple[str, bool, int]


class DragCache:
    SYNC_MAX = 64 * 1024 * 1024          # pas encore prêt : extrait sur le moment jusqu'à cette taille
    PREFETCH_MAX = 4 * 1024 ** 3         # au-delà : « Extraire la sélection »

    def __init__(self, source: Callable[[], tuple], message: Callable[[str], None]):
        """source() → (chemin de l'archive, mot de passe, ArchiveListing) ; message(texte) : un toast."""
        self.source = source
        self.message = message
        self.root: Optional[str] = None
        self.cache: dict[str, str] = {}      # chemin dans l'archive → chemin extrait
        self.dirs: dict[str, list] = {}      # dossier d'extraction → éléments qu'il contient
        self.given: set[str] = set()         # déjà glissés : leur dossier reste
        self.token = 0
        self.job: Optional[Job] = None
        self.waiting = False
        self._timer = 0
        self._pending: Callable[[], list] = list

    # ------------------------------------------------------------------
    def size_of(self, item: Item) -> int:
        inner, is_dir, size = item
        if not is_dir:
            return size or 0
        listing = self.source()[2]
        return sum(e.size or 0 for e in listing.walk(inner) if not e.is_dir) if listing else 0

    def cached(self, inner: str) -> Optional[str]:
        p = self.cache.get(inner)
        return p if p and os.path.lexists(p) else None

    def _extract(self, job, inners: list[str], path: str, password: str, root: str) -> dict:
        tmp = archive.extract_to_temp(job, path, inners, password, parent=root)
        out = {i: os.path.join(tmp, os.path.basename(i.rstrip("/"))) for i in inners}
        return {i: p for i, p in out.items() if os.path.lexists(p)}

    def _store(self, found: dict):
        for inner, path in found.items():
            self.cache[inner] = path
            self.dirs.setdefault(os.path.dirname(path), []).append(path)

    def _ensure_root(self) -> str:
        if self.root is None:
            self.root = archive.drag_cache_root()
        return self.root

    # ------------------------------------------------------------------
    def queue(self, items: Callable[[], list]):
        """Sélection changée : préparer ses éléments un peu plus tard (pas à chaque clic)."""
        self._pending = items
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(350, self._prefetch_now)

    def _prefetch_now(self):
        self._timer = 0
        self.prefetch(self._pending())
        return False

    def prefetch(self, items: list):
        path, password, listing = self.source()
        if not path or listing is None:
            return
        missing = [it for it in items if not self.cached(it[0])]
        if self.job is not None:
            if set(self.job.inners) >= {it[0] for it in missing}:
                return                       # déjà en cours pour ces éléments
            self.job.cancel()
            self.job = None
        if not missing or sum(self.size_of(it) for it in missing) > self.PREFETCH_MAX:
            return
        inners = [it[0] for it in missing]
        token, root = self.token, self._ensure_root()
        job = Job("drag", lambda j: self._extract(j, inners, path, password, root))
        job.inners = inners
        self.job = job

        def work():
            job.run_sync()
            GLib.idle_add(self._done, job, token, items)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, job, token, items):
        if self.job is job:
            self.job = None
        if token != self.token or job.state != "done" or not job.result:
            return False
        self._store(job.result)
        if self.waiting and all(self.cached(it[0]) for it in items):
            self.waiting = False
            self.message(tr("Sélection prête : vous pouvez la glisser"))
        return False

    def files_for(self, items: list) -> list[str]:
        """Au début d'un glisser : les fichiers extraits ([] : pas prêt, un message le dit)."""
        if not items:
            return []
        path, password, listing = self.source()
        if not path or listing is None:
            return []
        missing = [it for it in items if not self.cached(it[0])]
        if missing:
            size = sum(self.size_of(it) for it in missing)
            if size > self.PREFETCH_MAX:
                self.message(tr("Sélection trop grosse pour un glisser : utilisez « Extraire la sélection… »"))
                return []
            if size > self.SYNC_MAX or self.job is not None:
                self.waiting = True
                self.prefetch(items)
                self.message(tr("Préparation de la sélection… Glissez-la de nouveau dans un instant"))
                return []
            inners, root = [it[0] for it in missing], self._ensure_root()
            job = Job("drag", lambda j: self._extract(j, inners, path, password, root)).run_sync()
            if job.state != "done" or not job.result:
                return []
            self._store(job.result)
        out = [p for p in (self.cached(it[0]) for it in items) if p]
        self.given.update(out)
        return out

    def forget(self):
        """Archive quittée : les extractions jamais glissées sont retirées tout de suite."""
        self.token += 1
        if self.job is not None:
            self.job.cancel()
            self.job = None
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        for folder, paths in self.dirs.items():
            if not any(p in self.given for p in paths):
                shutil.rmtree(folder, ignore_errors=True)
        if self.root and not self.given:
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None
        self.cache, self.dirs, self.given = {}, {}, set()
        self.waiting = False
