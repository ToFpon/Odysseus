"""Petite réserve de fils pour les travaux paresseux : miniatures, durées, nombres d'éléments.

Les demandes viennent des lignes affichées ; la plus récente passe en premier (pile), si bien
qu'un défilement rapide ne fait pas attendre les lignes devant les yeux de l'utilisateur.
Les doublons sont ignorés. Le résultat est rendu sur la boucle principale.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable

from gi.repository import GLib


class Pool:
    def __init__(self, threads: int = 2, limit: int = 4000):
        self._tasks: "OrderedDict[object, tuple]" = OrderedDict()
        self._running: set = set()
        self._cv = threading.Condition()
        self._limit = limit
        for _ in range(threads):
            threading.Thread(target=self._loop, daemon=True).start()

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
            try:
                result = func()
            except Exception:  # noqa: BLE001 — un fichier illisible ne doit rien casser
                result = None
            with self._cv:
                self._running.discard(key)
            GLib.idle_add(_deliver, callback, result)


def _deliver(callback, result):
    try:
        callback(result)
    except Exception as e:  # noqa: BLE001
        print("[odysseus] worker callback:", e)
    return False


pool = Pool(2)          # métadonnées rapides (nombre d'éléments, emblèmes, durées)
thumb_pool = Pool(2)    # miniatures (plus lentes)
