"""Tests du moteur d'Odysseus (sans interface)."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import Gio  # noqa: E402

from odysseus.backend import bookmarks, fileops, formats, media, scanner, search, tools, workers  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402
from odysseus.i18n import tr  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}\n{j.log}")
    return j.result


class ContentTypeTest(unittest.TestCase):
    """Types comme Nautilus (standard::content-type de GIO), pas seulement d'après le nom."""

    def test_sniffed_like_nautilus(self):
        with tempfile.TemporaryDirectory() as d:
            files = {".bash_aliases": b'alias ll="ls -l"\n', "spotlight.sh.bck1": b"#!/bin/bash\necho hi\n",
                     "vide.tmp": b"", "odysseus.log": b"", "notes.txt": b"x"}
            for n, data in files.items():
                with open(os.path.join(d, n), "wb") as f:
                    f.write(data)
            os.mkfifo(os.path.join(d, "tube"))
            recs = {r.name: r for r in scanner.scan_local(d, threading.Event())}
            for n in list(files) + ["tube"]:
                gio = Gio.File.new_for_path(os.path.join(d, n)).query_info(
                    "standard::content-type", Gio.FileQueryInfoFlags.NONE, None).get_content_type()
                self.assertEqual(recs[n].content_type, gio, n)
            self.assertEqual(recs["spotlight.sh.bck1"].content_type, "application/x-shellscript")
            self.assertEqual(recs["vide.tmp"].content_type, "application/x-zerosize")


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


class ReplaceTest(unittest.TestCase):
    """Rechercher / remplacer (retours de l'agent de test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_line_numbers_like_ripgrep(self):
        # \f, \r seul, \x85, U+2028 ne sont pas des fins de ligne pour ripgrep
        for name, text in (("ff.c", "a\fb\nCIBLE\n"), ("cr.txt", "a\rb\nCIBLE\n"),
                           ("nel.txt", "a\x85b\nCIBLE\n"), ("u2028.txt", "a\u2028CIBLE\nCIBLE\n"),
                           ("crlf.txt", "x\r\nCIBLE\r\n")):
            p = os.path.join(self.d, name)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            q = search.Query(pattern="CIBLE", folder=self.d)
            total, files, errors = search.apply_replace(q, {p: {2}}, "OK", backup=False)
            with open(p, encoding="utf-8", newline="") as f:
                got = f.read()
            self.assertEqual(got, text.replace("\nCIBLE", "\nOK", 1) if name != "u2028.txt"
                             else "a\u2028CIBLE\nOK\n", name)
            self.assertEqual(total, 1, name)

    def test_preview_matches_apply_on_long_lines(self):
        """ripgrep coupe les lignes > 400 colonnes : l'aperçu doit compter tout ce qui sera remplacé."""
        p = os.path.join(self.d, "min.js")
        with open(p, "w") as f:
            f.write("var foo=1;" + "x" * 800 + "foo();\n" + "y" * 900 + "foo\n")
        q = search.Query(pattern="foo", folder=self.d)
        found = []
        search.run(q, found.extend, lambda: False)
        preview = search.preview_replace(q, found, "BAR")
        self.assertEqual(sum(after.count("BAR") for _m, after in preview), 3)
        targets = {}
        for m, _after in preview:
            targets.setdefault(m.path, set()).add(m.line)
        total, _files, _err = search.apply_replace(q, targets, "BAR", backup=False)
        self.assertEqual(total, 3)

    def test_backup_never_overwrites_existing_bak(self):
        p = os.path.join(self.d, "f.txt")
        with open(p, "w") as f:
            f.write("bonjour\n")
        with open(p + ".bak", "w") as f:
            f.write("MA SAUVEGARDE")
        made = []
        search.apply_replace(search.Query(pattern="bonjour", folder=self.d), {p: {1}}, "salut", True, made)
        self.assertEqual(open(p + ".bak").read(), "MA SAUVEGARDE")
        self.assertEqual(made, [p + ".bak2"])
        self.assertEqual(open(p + ".bak2").read(), "bonjour\n")


class TrashWatchTest(unittest.TestCase):
    """Comptage des corbeilles (contournement de gvfsd-trash qui « oublie » les autres disques)."""

    def test_counts_home_and_mounted_trash_with_info_only(self):
        from odysseus.backend import fsinfo, trashwatch
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "data")
            disk = os.path.join(d, "disque")
            uid = os.getuid()
            for root in (os.path.join(home, "Trash"), os.path.join(disk, f".Trash-{uid}")):
                os.makedirs(os.path.join(root, "files"))
                os.makedirs(os.path.join(root, "info"))
                for n in ("a.txt", "b"):
                    open(os.path.join(root, "files", n), "w").close()
                    open(os.path.join(root, "info", n + ".trashinfo"), "w").close()
            open(os.path.join(disk, f".Trash-{uid}", "files", "orphelin"), "w").close()   # sans fiche
            real_mounts, old_env = fsinfo.mounts, os.environ.get("XDG_DATA_HOME")
            fsinfo.mounts = lambda: [(disk, "ext4"), ("/proc", "proc")]
            os.environ["XDG_DATA_HOME"] = home
            try:
                self.assertEqual(trashwatch.expected_count(), 4)
            finally:
                fsinfo.mounts = real_mounts
                if old_env is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = old_env

    def test_restart_throttled(self):
        from odysseus.backend import trashwatch
        trashwatch._last_restart = 0.0
        real = trashwatch.subprocess.run
        calls = []
        trashwatch.subprocess.run = lambda *a, **k: calls.append(a)
        try:
            self.assertTrue(trashwatch.restart_gvfs_trash())
            self.assertFalse(trashwatch.restart_gvfs_trash())      # moins d'une minute après
        finally:
            trashwatch.subprocess.run = real
        self.assertEqual(len(calls), 1)


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
    def test_no_idle_grab_focus(self):
        """idle_add(x.grab_focus) boucle sans fin (grab_focus renvoie True) : common.focus_later."""
        import re
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "odysseus")
        bad = []
        for d, _dirs, files in os.walk(root):
            for n in files:
                if n.endswith(".py"):
                    with open(os.path.join(d, n), encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if re.search(r"(idle_add|timeout_add)\([^#]*\.grab_focus\)", line) and "Jamais" not in line:
                                bad.append(f"{n}:{i}")
        self.assertEqual(bad, [])

    def test_7z_error_on_next_line(self):
        """« System ERROR: » / « Error: » suivis de la raison à la ligne suivante : message non vide."""
        from odysseus.backend.engine import ToolError, raise_for_7z
        text = ("\nError:\nUpdating for multivolume archives is not implemented\n/tmp/x.7z.001\n\n\n"
                "System ERROR:\nE_NOTIMPL : Not implemented\n")
        with self.assertRaises(ToolError) as cm:
            raise_for_7z(text, 2)
        self.assertEqual(str(cm.exception), "Updating for multivolume archives is not implemented")
        with self.assertRaises(ToolError) as cm:
            raise_for_7z("System ERROR:\nE_FAIL\n", 2)
        self.assertEqual(str(cm.exception), "E_FAIL")

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


class FileManager1ServiceTest(unittest.TestCase):
    """« Afficher dans le dossier » Odysseus fermé : fichier de service D-Bus suivi de l'option."""

    def test_service_follows_option(self):
        from unittest import mock
        from odysseus import dbus
        with tempfile.TemporaryDirectory() as d:
            svc = os.path.join(d, dbus.NAME + ".service")
            with open(os.path.join(d, dbus.APP_ID + ".service"), "w") as f:
                f.write("[D-BUS Service]\nName=x\nExec=/opt/bin/odysseus --gapplication-service\n")
            with mock.patch.object(dbus, "SERVICE_DIR", d), mock.patch.object(dbus, "SERVICE_FILE", svc):
                dbus.sync_service(True)
                with open(svc) as f:
                    self.assertIn("Exec=/opt/bin/odysseus --gapplication-service", f.read())
                dbus.sync_service(False)
                self.assertFalse(os.path.exists(svc))
                with open(svc, "w") as f:           # fichier d'un autre programme : jamais touché
                    f.write("[D-BUS Service]\nName=org.freedesktop.FileManager1\nExec=autre\n")
                dbus.sync_service(True)
                dbus.sync_service(False)
                with open(svc) as f:
                    self.assertIn("Exec=autre", f.read())


class ChecksumTest(unittest.TestCase):
    """Comparer à l'empreinte publiée, vérifier un fichier .sha256 / SHA256SUMS."""

    def setUp(self):
        import hashlib
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.iso = os.path.join(self.d, "zorin.iso")
        with open(self.iso, "wb") as f:
            f.write(b"image disque" * 1000)
        data = open(self.iso, "rb").read()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.md5 = hashlib.md5(data).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fn):
        j = Job("t", fn).run_sync()
        self.assertEqual(j.state, "done", j.error)
        return j.result

    def test_expected_digest_formats(self):
        h = self.sha256
        for text in (h, h.upper(), f"{h}  zorin.iso", f"SHA256 (zorin.iso) = {h}",
                     " ".join(h[i:i + 8] for i in range(0, 64, 8)), f"\n  {h}\n"):
            self.assertEqual(tools.expected_digest(text), h, text)
        self.assertIsNone(tools.expected_digest("bonjour"))
        self.assertIsNone(tools.expected_digest("abc123"))

    def test_compare_match_and_mismatch(self):
        match, _res = self._run(lambda j: tools.compare_digest(j, self.iso, self.sha256))
        self.assertEqual(match, "SHA-256")
        match, _res = self._run(lambda j: tools.compare_digest(j, self.iso, self.md5))
        self.assertEqual(match, "MD5")
        wrong = ("0" if self.sha256[0] != "0" else "1") + self.sha256[1:]
        match, res = self._run(lambda j: tools.compare_digest(j, self.iso, wrong))
        self.assertIsNone(match)
        self.assertEqual(res["SHA-256"], self.sha256)

    def test_verify_sums_file_statuses(self):
        for name in ("abime.bin", "secret.bin"):
            with open(os.path.join(self.d, name), "wb") as f:
                f.write(b"x")
        sums = os.path.join(self.d, "SHA256SUMS")
        with open(sums, "w") as f:
            f.write(f"{self.sha256}  zorin.iso\n{'0' * 64}  abime.bin\n{'1' * 64}  absent.bin\n"
                    f"{'2' * 64}  secret.bin\n")
        os.chmod(os.path.join(self.d, "secret.bin"), 0)
        try:
            res = {os.path.basename(r["path"]): r["status"] for r in self._run(lambda j: tools.verify_hash_file(j, sums))}
        finally:
            os.chmod(os.path.join(self.d, "secret.bin"), 0o644)
        expected = {"zorin.iso": "ok", "abime.bin": "mismatch", "absent.bin": "missing",
                    "secret.bin": "unreadable" if os.geteuid() != 0 else "mismatch"}   # root lit tout
        self.assertEqual(res, expected)

    def test_is_checksum_file(self):
        for n in ("x.sha256", "x.md5", "x.sfv", "SHA256SUMS", "MD5SUMS", "SHA512SUMS.txt"):
            self.assertTrue(tools.is_checksum_file(n), n)
        for n in ("x.iso", "SHA256SUMS.gpg", "notes.txt"):
            self.assertFalse(tools.is_checksum_file(n), n)


class SidebarPlacesTest(unittest.TestCase):
    """Dossiers de l'utilisateur comme Nautilus : leur vrai nom sur le disque, triés par ordre
    alphabétique (les noms de Tof : Music, Telechargements, Video…)."""

    def test_real_names_alphabetical(self):
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            names = {"DESKTOP": "Bureau", "DOCUMENTS": "Documents", "DOWNLOAD": "Telechargements",
                     "MUSIC": "Music", "PICTURES": "Images", "VIDEOS": "Video"}
            os.makedirs(os.path.join(home, ".config"))
            with open(os.path.join(home, ".config", "user-dirs.dirs"), "w") as f:
                for key, name in names.items():
                    os.makedirs(os.path.join(home, name))
                    f.write(f'XDG_{key}_DIR="$HOME/{name}"\n')
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from odysseus.ui.sidebar import places\n"
                    "print('|'.join(label for _i, label, _p in places(desktop=False)))") % root
            env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"), LANGUAGE="fr")
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            labels = out.stdout.strip().split("|")
            self.assertEqual(labels[1:-1], ["Documents", "Images", "Music", "Telechargements", "Video"],
                             out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
