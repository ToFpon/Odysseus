"""Audit de novembre 2026 (annuler / rétablir) : vraie classe FileActions sans interface, opérations
synchrones, HOME jetable. La corbeille par URI (trash:///) demande GVfs : lancer sous dbus-run-session,
gvfsd est démarré ici sur le HOME jetable (sans bus, les tests qui en dépendent sont sautés).

    dbus-run-session -- python3.12 -m unittest tests.test_undo_audit   (lance ce fichier à part)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BASE = tempfile.mkdtemp(prefix="odysseus-undo-")
HOME = os.path.join(BASE, "home")
os.makedirs(HOME)
os.environ["HOME"] = HOME
os.environ["XDG_DATA_HOME"] = os.path.join(HOME, ".local/share")
os.environ["XDG_CONFIG_HOME"] = os.path.join(HOME, ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(HOME, ".cache")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from odysseus.backend import fileops  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402
from odysseus.ui import actions_files as af  # noqa: E402

GVFSD = None
LOG: list = []


def setUpModule():
    global GVFSD
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists("/usr/libexec/gvfsd"):
        GVFSD = subprocess.Popen(["/usr/libexec/gvfsd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)


def tearDownModule():
    if GVFSD:
        GVFSD.terminate()
    shutil.rmtree(BASE, ignore_errors=True)


def gvfs() -> bool:
    return GVFSD is not None and GVFSD.poll() is None


class _Mgr:
    def __init__(self):
        self.current = None
        self.cancel_after = None        # annule le job au N-ième déplacement (voir test_cancel)

    def submit(self, title, func, done):
        j = Job(title, func)
        self.current = j
        j.run_sync()
        LOG.append(("job", j.state, j.error))
        done(j)
        return j


class _Sidebar:
    def refresh_trash_count(self):
        pass


class Win(af.FileActions):
    def __init__(self, answer="replace"):
        self.undo_stack, self.redo_stack = [], []
        self.sidebar = _Sidebar()
        self.answer = answer
        self.pane = None

    def lookup_action(self, name):
        return None

    def all_panes(self):
        return []

    def _watch_progress(self, job):
        pass

    def _ask_conflict(self, c):
        return (self.answer, True)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data)


def read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


class UndoAuditTest(unittest.TestCase):
    def setUp(self):
        self.mgr = _Mgr()
        self._saved = (af.manager, af.toast, af.alert, af.finished_toast)
        af.manager = self.mgr
        af.toast = lambda win, msg, *a, **k: LOG.append(("toast", msg))
        af.alert = lambda win, title, text="", **k: LOG.append(("alert", title, text))
        af.finished_toast = lambda win, job, msg: LOG.append(("finished", job.state))
        LOG.clear()
        self.d = tempfile.mkdtemp(dir=HOME)          # même disque que la corbeille de HOME

    def tearDown(self):
        af.manager, af.toast, af.alert, af.finished_toast = self._saved

    def test_replace_then_undo_puts_original_back(self):
        if not gvfs():
            self.skipTest("corbeille GVfs (dbus-run-session)")
        src, dst = f"{self.d}/src", f"{self.d}/dst"
        write(f"{src}/a.txt", "NOUVEAU")
        write(f"{dst}/a.txt", "PRÉCIEUX")
        w = Win("replace")
        w.run_transfer([f"{src}/a.txt"], dst)
        self.assertEqual(read(f"{dst}/a.txt"), "NOUVEAU")
        w.undo()
        self.assertEqual(read(f"{dst}/a.txt"), "PRÉCIEUX")          # avant : fichier absent, perdu
        # déplacement avec remplacement
        write(f"{src}/b.txt", "NOUVEAU-B")
        write(f"{dst}/b.txt", "PRÉCIEUX-B")
        w.run_transfer([f"{src}/b.txt"], dst, move=True)
        w.undo()
        self.assertEqual(read(f"{src}/b.txt"), "NOUVEAU-B")
        self.assertEqual(read(f"{dst}/b.txt"), "PRÉCIEUX-B")
        self.assertEqual(w.redo_stack, [])                          # un remplacement ne se rejoue pas

    def test_undo_trash_restores_our_item_not_a_namesake(self):
        if not gvfs():
            self.skipTest("corbeille GVfs (dbus-run-session)")
        a, b = Win(), Win()
        write(f"{self.d}/rapport.txt", "A")
        a._trash_paths([f"{self.d}/rapport.txt"])
        write(f"{self.d}/rapport.txt", "B")
        b._trash_paths([f"{self.d}/rapport.txt"])
        a.undo()                                    # avant : remettait celui de B (le plus récent)
        self.assertEqual(read(f"{self.d}/rapport.txt"), "A")

    def test_undo_after_item_left_trash_says_so(self):
        if not gvfs():
            self.skipTest("corbeille GVfs (dbus-run-session)")
        w = Win()
        write(f"{self.d}/notes.txt", "x")
        w._trash_paths([f"{self.d}/notes.txt"])
        uri = w.undo_stack[-1][2][f"{self.d}/notes.txt"]
        gi.repository.Gio.File.new_for_uri(uri).delete(None)
        w.undo()
        self.assertTrue(any("n'est plus dans la corbeille" in str(e) or "no longer in the trash" in str(e)
                            for e in LOG), LOG)

    def test_redo_copy_then_undo_spares_user_file(self):
        if not gvfs():
            self.skipTest("corbeille GVfs (dbus-run-session)")
        write(f"{self.d}/s/a.txt", "COPIE")
        os.makedirs(f"{self.d}/D")
        w = Win()
        w.run_transfer([f"{self.d}/s/a.txt"], f"{self.d}/D")
        w.undo()
        write(f"{self.d}/D/a.txt", "TRAVAIL")       # un éditeur enregistre au même nom
        w.redo()                                    # la copie revient en « a (2).txt »
        w.undo()
        self.assertEqual(read(f"{self.d}/D/a.txt"), "TRAVAIL")     # avant : mis à la corbeille
        self.assertFalse(os.path.exists(f"{self.d}/D/a (2).txt"))

    def test_cancelled_move_stays_undoable(self):
        S, D = f"{self.d}/S", f"{self.d}/D"
        for i in range(10):
            write(f"{S}/f{i}.txt", str(i))
        os.makedirs(D)
        write(f"{self.d}/autre/r.txt", "R")
        w = Win()
        os.rename(f"{self.d}/autre/r.txt", f"{self.d}/autre/r2.txt")
        w._push_undo(("rename", f"{self.d}/autre/r.txt", f"{self.d}/autre/r2.txt"))
        orig = fileops._move_same_disk
        n = {"i": 0}

        def spy(*a, **k):
            orig(*a, **k)
            n["i"] += 1
            if n["i"] == 4:
                self.mgr.current._cancel.set()
        fileops._move_same_disk = spy
        try:
            w.run_transfer([f"{S}/f{i}.txt" for i in range(10)], D, move=True)
        finally:
            fileops._move_same_disk = orig
        self.assertEqual(len(os.listdir(D)), 4)
        w.undo()                                    # avant : annulait le renommage d'avant
        self.assertEqual(len(os.listdir(S)), 10)
        self.assertEqual(os.listdir(D), [])
        self.assertEqual(os.listdir(f"{self.d}/autre"), ["r2.txt"])

    def test_toast_undo_targets_its_own_entry(self):
        w = Win()
        write(f"{self.d}/x.txt", "x")
        first = ("rename", f"{self.d}/x0.txt", f"{self.d}/x.txt")
        w._push_undo(first)
        w._push_undo(("rename", f"{self.d}/y0.txt", f"{self.d}/y.txt"))
        w._undo_entry(first)                        # plus la dernière : rien n'est défait
        self.assertEqual(len(w.undo_stack), 2)
        self.assertTrue(os.path.exists(f"{self.d}/x.txt"))

    def test_case_only_rename_check(self):
        d = f"{self.d}/casse"
        write(f"{d}/photo.jpg", "x")
        # support insensible à la casse : « Photo.jpg » « existe » (c'est photo.jpg lui-même) → permis
        self.assertTrue(af._case_only(d, "Photo.jpg", "photo.jpg"))
        write(f"{d}/Photo.jpg", "y")
        self.assertFalse(af._case_only(d, "Photo.jpg", "photo.jpg"))   # deux fichiers distincts : refusé
        self.assertFalse(af._case_only(d, "autre.jpg", "photo.jpg"))   # pas un simple changement de casse


if __name__ == "__main__":
    unittest.main()
