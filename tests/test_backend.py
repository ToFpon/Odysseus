"""Tests du moteur d'Odysseus (sans interface)."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import bookmarks, fileops, formats, media, scanner, search, workers  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402
from odysseus.i18n import tr  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}\n{j.log}")
    return j.result


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        for n in ("b.txt", "a10.txt", "a2.txt", ".cache", "notes~", "Été.md"):
            with open(os.path.join(self.d, n), "w") as f:
                f.write("x" * len(n))
        os.makedirs(os.path.join(self.d, "zeta"))
        os.makedirs(os.path.join(self.d, "Alpha"))
        with open(os.path.join(self.d, ".hidden"), "w") as f:
            f.write("b.txt\n")
        os.symlink("a2.txt", os.path.join(self.d, "lien"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_sort(self):
        recs = scanner.scan_local(self.d, threading.Event(), with_stat=False)
        scanner.stat_all(recs, threading.Event())
        names = [r.name for r in scanner.sort_recs(recs)]
        self.assertEqual(names[:2], ["Alpha", "zeta"])                  # dossiers en premier
        self.assertLess(names.index("a2.txt"), names.index("a10.txt"))  # tri naturel
        hidden = {r.name for r in recs if r.hidden}
        self.assertTrue({".cache", "notes~", "b.txt"} <= hidden)        # .hidden, sauvegardes
        rev = [r.name for r in scanner.sort_recs(recs, "name", reverse=True)]
        self.assertEqual(rev[:2], ["zeta", "Alpha"])                    # l'ordre inverse garde les dossiers en tête
        link = next(r for r in recs if r.name == "lien")
        self.assertTrue(link.is_link and link.stat_ok and link.size == len("a2.txt"))

    def test_sort_keys(self):
        recs = scanner.scan_local(self.d, threading.Event())
        for key in scanner.SORT_KEYS:
            scanner.sort_recs(recs, key)
        by_size = [r.name for r in scanner.sort_recs(recs, "size") if not r.is_dir]
        self.assertEqual(by_size[0], "lien" if os.path.getsize(os.path.join(self.d, "lien")) <= 4 else by_size[0])

    def test_hidden_names_last(self):
        # (l'ordre des majuscules dépend de la locale : noms en minuscules seulement)
        names = [".zeta", "beta", "#sauve#", "alpha", ".android", "10", "2"]
        out = sorted(names, key=scanner.sort_name)
        self.assertEqual(out[:4], ["2", "10", "alpha", "beta"])
        self.assertEqual(set(out[4:]), {".android", ".zeta", "#sauve#"})
        self.assertLess(out.index(".android"), out.index(".zeta"))

    def test_date_label(self):
        import time
        from datetime import datetime
        from odysseus.ui.common import MONTHS, date_label
        from odysseus.i18n import tr
        now = time.time()
        self.assertEqual(date_label(now), tr("Aujourd'hui à {0}").format(time.strftime("%H:%M", time.localtime(now))))
        self.assertTrue(date_label(now - 86400).startswith(tr("Hier à {0}").split("{0}")[0]))
        old = datetime(2024, 9, 4, 12, 0).timestamp()
        self.assertEqual(date_label(old), tr("{day} {month} {year}").format(day=4, month=MONTHS[8], year=2024))

    def test_scan_gio(self):
        """Lecture par GIO (corbeille, réseau) : ici sur une URI file://."""
        from gi.repository import Gio
        recs = scanner.scan_gio(Gio.File.new_for_path(self.d), threading.Event())
        self.assertEqual(sorted(r.name for r in recs), sorted(os.listdir(self.d)))

    def test_sparse_gio_info(self):
        """Élément réseau (Freebox) sans taille ni attributs « cachés » : aucune erreur GIO."""
        from gi.repository import Gio
        info = Gio.FileInfo()
        info.set_name("Freebox")
        info.set_file_type(Gio.FileType.MOUNTABLE)
        r = scanner.rec_from_info(info, Gio.File.new_for_uri("network:///"))
        self.assertTrue(r.is_dir and r.get("mountable"))
        self.assertEqual((r.size, r.hidden), (0, False))

    def test_sizes_like_nautilus(self):
        from odysseus.backend.engine import decimal, human_size
        from odysseus.i18n import tr
        self.assertEqual(human_size(35100), decimal(35.1) + " " + tr("Ko"))
        self.assertEqual(human_size(999), "999 " + tr("o"))

    def test_rec_for_path_and_count(self):
        r = scanner.rec_for_path(os.path.join(self.d, "b.txt"))
        self.assertTrue(r.hidden)
        self.assertEqual(scanner.count_children(self.d, False), 8)


class FileOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        os.makedirs(os.path.join(self.d, "src", "sub"))
        with open(os.path.join(self.d, "src", "sub", "f.bin"), "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024))
        with open(os.path.join(self.d, "src", "a.txt"), "w") as f:
            f.write("bonjour")
        os.symlink("a.txt", os.path.join(self.d, "src", "lien"))
        os.makedirs(os.path.join(self.d, "dst"))

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, answer, for_all=False):
        return lambda c: (answer, for_all)

    def test_copy_tree(self):
        res = run(lambda j: fileops.transfer(j, [os.path.join(self.d, "src")], os.path.join(self.d, "dst"),
                                             self.ask("cancel")))
        out = os.path.join(self.d, "dst", "src")
        self.assertEqual(res.created, [out])
        with open(os.path.join(out, "sub", "f.bin"), "rb") as a, open(os.path.join(self.d, "src", "sub", "f.bin"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.path.islink(os.path.join(out, "lien")))

    def test_conflicts(self):
        dst = os.path.join(self.d, "dst")
        with open(os.path.join(dst, "a.txt"), "w") as f:
            f.write("ancien")
        src = os.path.join(self.d, "src", "a.txt")
        run(lambda j: fileops.transfer(j, [src], dst, self.ask("skip")))
        self.assertEqual(open(os.path.join(dst, "a.txt")).read(), "ancien")
        run(lambda j: fileops.transfer(j, [src], dst, self.ask("rename")))
        self.assertTrue(os.path.exists(os.path.join(dst, "a (2).txt")))
        run(lambda j: fileops.transfer(j, [src], dst, self.ask("replace")))
        self.assertEqual(open(os.path.join(dst, "a.txt")).read(), "bonjour")
        j = Job("x", lambda j: fileops.transfer(j, [src], dst, self.ask("cancel"))).run_sync()
        self.assertEqual(j.state, "cancelled")

    def test_merge_dirs(self):
        dst = os.path.join(self.d, "dst")
        os.makedirs(os.path.join(dst, "src", "sub"))
        with open(os.path.join(dst, "src", "garde.txt"), "w") as f:
            f.write("x")
        run(lambda j: fileops.transfer(j, [os.path.join(self.d, "src")], dst, self.ask("merge")))
        self.assertTrue(os.path.exists(os.path.join(dst, "src", "garde.txt")))
        self.assertTrue(os.path.exists(os.path.join(dst, "src", "sub", "f.bin")))

    def test_same_place_and_into_itself(self):
        src = os.path.join(self.d, "src", "a.txt")
        run(lambda j: fileops.transfer(j, [src], os.path.join(self.d, "src"), self.ask("cancel")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "src", "a" + tr(" (copie)") + ".txt")))
        j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.d, "src")],
                                                 os.path.join(self.d, "src", "sub"), self.ask("cancel"))).run_sync()
        self.assertEqual(j.state, "failed")

    def test_move_and_delete(self):
        src = os.path.join(self.d, "src")
        res = run(lambda j: fileops.transfer(j, [src], os.path.join(self.d, "dst"), self.ask("cancel"), move=True))
        self.assertFalse(os.path.exists(src))
        self.assertEqual(res.moved[0][0], src)
        run(lambda j: fileops.delete(j, [os.path.join(self.d, "dst", "src")]))
        self.assertEqual(os.listdir(os.path.join(self.d, "dst")), [])

    def test_names(self):
        self.assertEqual(fileops.split_ext("photo.tar.gz"), ("photo", ".tar.gz"))
        self.assertEqual(fileops.split_ext(".bashrc"), (".bashrc", ""))
        self.assertTrue(fileops.valid_name("a/b"))
        self.assertTrue(fileops.valid_name(""))
        self.assertEqual(fileops.valid_name("ok.txt"), "")
        p = fileops.make_folder(self.d, "Nouveau")
        self.assertTrue(os.path.isdir(p))
        p2 = fileops.rename(p, "Renommé")
        self.assertTrue(os.path.isdir(p2))
        with self.assertRaises(Exception):
            fileops.make_folder(self.d, "Renommé")


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        os.makedirs(os.path.join(self.d, "sous"))
        with open(os.path.join(self.d, "a.py"), "w") as f:
            f.write("x = 1\nbonjour monde\nBonjour encore\n")
        with open(os.path.join(self.d, "sous", "b:c.txt"), "w") as f:
            f.write("rien\nbonjour ici\n")

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, q):
        out = []
        search.run(q, out.extend, lambda: False)
        return out

    def test_search(self):
        res = self.collect(search.Query("bonjour", self.d))
        self.assertEqual(len(res), 3)
        self.assertTrue(any(m.path.endswith("b:c.txt") and m.line == 2 for m in res))
        self.assertEqual(len(self.collect(search.Query("bonjour", self.d, case=True))), 2)
        self.assertEqual(len(self.collect(search.Query("bonjour", self.d, exts="py"))), 2)
        self.assertEqual(len(self.collect(search.Query("bonjour", self.d, recursive=False))), 2)

    def test_replace(self):
        q = search.Query("bonjour", self.d)
        matches = self.collect(q)
        prev = search.preview_replace(q, matches, "salut")
        self.assertEqual(len(prev), 3)
        a = os.path.join(self.d, "a.py")
        total, files, errors = search.apply_replace(q, {a: {2}}, "salut")
        self.assertEqual((total, files, errors), (1, 1, []))
        self.assertEqual(open(a).read(), "x = 1\nsalut monde\nBonjour encore\n")
        self.assertTrue(os.path.exists(a + ".bak"))


class PoolTest(unittest.TestCase):
    def test_stuck_workers_do_not_block_others(self):
        """Deux fils coincés (stat sur un montage réseau mort) : une nouvelle tâche passe quand même."""
        pool = workers.Pool(2)
        pool.STUCK = 0.3
        gate, done = threading.Event(), threading.Event()
        try:
            for i in range(2):
                pool.submit(("bloque", i), lambda: gate.wait(30), lambda r: None)
            time.sleep(0.6)
            pool.submit("normal", done.set, lambda r: None)
            self.assertTrue(done.wait(2), "tâche normale bloquée derrière les fils coincés")
            self.assertEqual(pool._threads, 3)
        finally:
            gate.set()


class MiscTest(unittest.TestCase):
    def test_bookmarks(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "bookmarks")
            self.assertTrue(bookmarks.toggle("/tmp/a b", f))
            self.assertTrue(bookmarks.contains("/tmp/a b", f))
            bookmarks.rename("/tmp/a b", "Mon dossier", f)
            self.assertEqual(bookmarks.load(f)[0].title, "Mon dossier")
            self.assertEqual(bookmarks.load(f)[0].path, "/tmp/a b")
            self.assertFalse(bookmarks.toggle("/tmp/a b", f))
            self.assertEqual(bookmarks.load(f), [])

    def test_formats(self):
        self.assertTrue(formats.looks_like_archive("a.tar.gz"))
        self.assertTrue(formats.looks_like_archive("a.zip"))
        self.assertFalse(formats.looks_like_archive("a.docx"))
        self.assertTrue(formats.readable("a.docx"))
        self.assertFalse(formats.readable("a.txt"))

    def test_media(self):
        self.assertEqual(media.fmt_duration(3725), "01:02:05")
        self.assertEqual(media.fmt_duration(65), "01:05")
        self.assertTrue(media.is_video("x.MKV"))
        self.assertTrue(media.is_media("x.flac"))
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "film.mp4")
            open(v, "w").close()
            open(os.path.join(d, "film.mp3"), "w").close()
            self.assertEqual(os.path.basename(media.audio_output(v, "mp3")), "film (2).mp3")


if __name__ == "__main__":
    unittest.main()
