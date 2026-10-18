"""Petite réserve de fils pour les travaux paresseux : miniatures, durées, nombres d'éléments.

Les demandes viennent des lignes affichées ; la plus récente passe en premier (pile), si bien
qu'un défilement rapide ne fait pas attendre les lignes devant les yeux de l'utilisateur.
Les doublons sont ignorés. Le résultat est rendu sur la boucle principale.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable

from gi.repository import GLib


class Pool:
    STUCK = 3.0          # une tâche plus longue est considérée comme bloquée (stat sur un montage mort)

    def __init__(self, threads: int = 2, limit: int = 4000, max_threads: int = 6):
        self._tasks: "OrderedDict[object, tuple]" = OrderedDict()
        self._running: set = set()
        self._busy: dict = {}        # fil -> début de sa tâche en cours
        self._cv = threading.Condition()
        self._limit = limit
        self._threads = 0
        self._max = max_threads
        for _ in range(threads):
            self._spawn()

    def _spawn(self):
        self._threads += 1
        threading.Thread(target=self._loop, daemon=True).start()

    def _maybe_grow(self):
        """Un fil bloqué dans un appel système ne peut pas être tué : si tous les fils sont pris
        depuis trop longtemps, on en ajoute un pour que le reste continue (sous self._cv)."""
        if self._tasks and len(self._busy) >= self._threads and self._threads < self._max:
            now = time.monotonic()
            if all(now - t > self.STUCK for t in self._busy.values()):
                self._spawn()

    def submit(self, key, func: Callable[[], object], callback: Callable[[object], None]):
        with self._cv:
            if key in self._running:
                return
            if key in self._tasks:
                self._tasks.move_to_end(key)
            else:
                self._tasks[key] = (func, callback)
                while len(self._tasks) > self._limit:
                    self._tasks.popitem(last=False)
            self._maybe_grow()
            self._cv.notify()

    def discard(self, predicate: Callable[[object], bool]):
        with self._cv:
            for k in [k for k in self._tasks if predicate(k)]:
                del self._tasks[k]

    def _loop(self):
        while True:
            with self._cv:
                while not self._tasks:
                    self._cv.wait()
                key, (func, callback) = self._tasks.popitem(last=True)
                self._running.add(key)
                me = threading.get_ident()
                self._busy[me] = time.monotonic()
            try:
                result = func()
            except Exception:  # noqa: BLE001 — un fichier illisible ne doit rien casser
                result = None
            with self._cv:
                self._running.discard(key)
                self._busy.pop(me, None)
            GLib.idle_add(_deliver, callback, result)


def _deliver(callback, result):
    try:
        callback(result)
    except Exception as e:  # noqa: BLE001
        print("[odysseus] worker callback:", e)
    return False


pool = Pool(2)          # métadonnées rapides (nombre d'éléments, emblèmes, durées)
thumb_pool = Pool(2)    # miniatures (plus lentes)
