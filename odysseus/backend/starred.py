"""Favoris (fichiers marqués d'une étoile), partagés avec Nautilus 46.

Nautilus range ses étoiles dans une petite base TinySPARQL (ex-Tracker) privée :
$XDG_DATA_HOME/nautilus/tags, décrite par son ontologie (/usr/share/nautilus/ontology :
classe nautilus:File, propriété nautilus:starred). On ouvre la MÊME base avec la même ontologie
(nautilus-tag-manager.c, mêmes requêtes) : une étoile posée dans l'un se voit dans l'autre.
Sans Nautilus (Debian sous KDE…) : copie de son ontologie dans data/nautilus-ontology, même base —
compatible s'il est installé plus tard.

Nautilus ouvert en même temps garde ses étoiles en mémoire depuis son démarrage : il voit les
nôtres à sa prochaine ouverture. Nous relisons la base dès qu'elle change (date du fichier).

Comme Nautilus : seulement les éléments du dossier personnel (nautilus_tag_manager_can_star_contents).
Sujets = URI GIO (« file:///…%20… »), comme g_file_get_uri.

Bibliothèque : gir « Tracker 3.0 » (Ubuntu 24.04, gir1.2-tracker-3.0) ou « Tsparql 3.0»
(TinySPARQL ≥ 3.8 : Ubuntu 26.04, Debian 13)."""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from gi.repository import Gio, GLib

NAUTILUS_ONTOLOGY = "/usr/share/nautilus/ontology"
# data/ à côté du paquet odysseus (comme app.DATA_DIR) : copie de l'ontologie de Nautilus 46
BUNDLED_ONTOLOGY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "data", "nautilus-ontology")

URI = "starred:///"            # page « Favoris » (adresse de Nautilus)

_lock = threading.RLock()
_db = None
_lib = None
_failed = False
_uris: set[str] = set()
_stamp = None                 # (taille, date) des fichiers de la base au dernier chargement
listeners: list[Callable[[], None]] = []      # appelés (fil de l'interface) quand les étoiles changent


_store_override: Optional[str] = None


def store_dir() -> str:
    return _store_override or os.path.join(GLib.get_user_data_dir(), "nautilus", "tags")


def use_store(path: Optional[str]):
    """Tests : autre base (GLib garde le dossier de données du premier appel)."""
    global _store_override, _db, _failed, _uris, _stamp
    with _lock:
        if _db is not None:
            _db.close()
        _store_override, _db, _failed, _uris, _stamp = path, None, False, set(), None


def ontology_dir() -> str:
    return NAUTILUS_ONTOLOGY if os.path.isdir(NAUTILUS_ONTOLOGY) else BUNDLED_ONTOLOGY


def _library():
    """Module gi de TinySPARQL (Tsparql ou Tracker), ou None."""
    global _lib
    if _lib is not None:
        return _lib or None
    import gi
    for name in ("Tsparql", "Tracker"):
        try:
            gi.require_version(name, "3.0")
            _lib = __import__("gi.repository", fromlist=[name]).__dict__[name]
            return _lib
        except (ValueError, ImportError, KeyError):
            continue
    _lib = False
    return None


def available() -> bool:
    return _library() is not None and os.path.isdir(ontology_dir())


def _connection():
    global _db, _failed
    if _db is not None or _failed:
        return _db
    lib = _library()
    if lib is None or not os.path.isdir(ontology_dir()):
        _failed = True
        return None
    try:
        os.makedirs(store_dir(), exist_ok=True)
        _db = lib.SparqlConnection.new(lib.SparqlConnectionFlags.NONE,
                                       Gio.File.new_for_path(store_dir()),
                                       Gio.File.new_for_path(ontology_dir()), None)
    except (GLib.Error, OSError) as e:
        print("odysseus: favoris indisponibles :", e)
        _failed = True
        _db = None
    return _db


def _db_stamp():
    out = []
    for name in ("meta.db", "meta.db-wal"):
        try:
            s = os.stat(os.path.join(store_dir(), name))
            out.append((s.st_size, s.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def _escape(uri: str) -> str:
    # une URI GIO n'a ni « > » ni espace ; on refuse tout ce qui casserait la requête
    return uri if all(c not in uri for c in '<>"{}|\\^` \n') else ""


def uri_of(path: str) -> str:
    return Gio.File.new_for_path(path).get_uri()


def path_of(uri: str) -> Optional[str]:
    return Gio.File.new_for_uri(uri).get_path()


def can_star(path: Optional[str]) -> bool:
    """Comme Nautilus : seulement dans le dossier personnel (le dossier lui-même exclu)."""
    if not path:
        return False
    home = os.path.realpath(os.path.expanduser("~"))
    p = os.path.abspath(path)
    return p != home and (p.startswith(home + os.sep) or os.path.realpath(p).startswith(home + os.sep))


def reload(force: bool = False) -> set[str]:
    """Relit les étoiles si la base a changé (Nautilus a pu en poser). Renvoie l'ensemble des URI."""
    global _uris, _stamp
    with _lock:
        stamp = _db_stamp()
        if not force and stamp == _stamp and _db is not None:
            return _uris
        db = _connection()
        if db is None:
            return _uris
        uris = set()
        try:
            cur = db.query("SELECT ?file { ?file a nautilus:File ; nautilus:starred true . }", None)
            while cur.next(None):
                uris.add(cur.get_string(0)[0])
            cur.close()
        except GLib.Error as e:
            print("odysseus: lecture des favoris :", e)
            return _uris
        _uris = uris
        _stamp = _db_stamp()
        return _uris


def starred_uris() -> set[str]:
    return set(reload())


def starred_paths() -> list[str]:
    """Chemins des favoris qui existent encore, dans le dossier personnel."""
    out = []
    for uri in reload():
        p = path_of(uri)
        if p and can_star(p) and os.path.lexists(p):
            out.append(p)
    return out


def is_starred(path: Optional[str]) -> bool:
    return bool(path) and uri_of(path) in _uris


def cached_starred(path: Optional[str]) -> bool:
    """Pour le dessin des lignes : sans relire la base."""
    return bool(path) and uri_of(path) in _uris


def _update(sparql: str) -> bool:
    global _stamp
    with _lock:
        db = _connection()
        if db is None:
            return False
        try:
            db.update(sparql, None)
        except GLib.Error as e:
            print("odysseus: favoris :", e)
            return False
        _stamp = None            # relu au prochain accès
        reload(force=True)
    return True


def _triples(uris) -> str:
    return " ".join(f"<{u}> a nautilus:File ; nautilus:starred true ." for u in uris if _escape(u))


def star(paths: list[str]) -> bool:
    uris = [uri_of(p) for p in paths if can_star(p)]
    if not uris:
        return False
    ok = _update("INSERT DATA { " + _triples(uris) + " }")
    _notify()
    return ok


def unstar(paths: list[str]) -> bool:
    uris = [uri_of(p) for p in paths]
    uris = [u for u in uris if u in _uris] or uris
    if not uris:
        return False
    ok = _update("DELETE DATA { " + _triples(uris) + " }")
    _notify()
    return ok


def unstar_uris(uris: list[str]) -> bool:
    ok = _update("DELETE DATA { " + _triples(uris) + " }") if uris else False
    _notify()
    return ok


def moved(pairs: list[tuple[str, str]]) -> bool:
    """Éléments déplacés / renommés : leurs étoiles (et celles de leur contenu) suivent, comme
    nautilus_tag_manager_update_moved_uris."""
    if not pairs:
        return False
    with _lock:
        current = reload()
        if not current:
            return False
        old, new = [], []
        for src, dst in pairs:
            s, d = Gio.File.new_for_path(src), Gio.File.new_for_path(dst)
            for uri in current:
                f = Gio.File.new_for_uri(uri)
                if f.equal(s):
                    target = d
                else:
                    rel = s.get_relative_path(f)
                    if rel is None:
                        continue
                    target = d.resolve_relative_path(rel)
                old.append(uri)
                new.append(target.get_uri())
        if not old:
            return False
        ok = _update("DELETE DATA { " + _triples(old) + " } ; INSERT DATA { " + _triples(new) + " }")
    _notify()
    return ok


def _notify():
    def run():
        for cb in list(listeners):
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                print("odysseus: favoris :", e)
        return False
    GLib.idle_add(run)


_monitor = None


def watch():
    """Suit la base : étoiles posées par Nautilus → vues rafraîchies."""
    global _monitor
    if _monitor is not None or not available():
        return
    try:
        os.makedirs(store_dir(), exist_ok=True)
        _monitor = Gio.File.new_for_path(store_dir()).monitor_directory(Gio.FileMonitorFlags.NONE, None)
    except (GLib.Error, OSError):
        return
    _monitor.set_rate_limit(1000)
    pending = {"id": 0}

    def check():
        pending["id"] = 0
        with _lock:
            before = set(_uris)
            after = reload()
        if after != before:
            _notify()
        return False

    def changed(*_):
        if not pending["id"]:
            pending["id"] = GLib.timeout_add(500, check)
    _monitor.connect("changed", changed)


_started = False


def start():
    """Première lecture en fond (la base s'ouvre en quelques dizaines de ms), puis suivi des
    changements. Appelé depuis le fil de l'interface quand la fonctionnalité est active."""
    global _started
    if _started or not available():
        return
    _started = True
    watch()

    def work():
        reload()
        _notify()
    threading.Thread(target=work, daemon=True).start()
