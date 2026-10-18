"""Opérations destructrices, sur un arbre jetable : là où une erreur coûte des données.

Remplacer, fusionner, annuler en cours de route, déplacer entre deux systèmes de fichiers,
liens symboliques (ne jamais toucher la cible), renommer sur un nom pris, corbeille et annulation.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import Gio  # noqa: E402

from odysseus.backend import fileops, fsinfo  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}")
    return j.result


def ask(answer, for_all=True):
    return lambda c: (answer, for_all)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree(path):
    """{chemin relatif: contenu ou '<lien> cible' ou '<dossier>'} : instantané d'une arborescence."""
    out = {}
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, path)
            if os.path.islink(p):
                out[rel] = "<lien> " + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = "<dossier>"
            else:
                with open(p, "rb") as fh:
                    out[rel] = fh.read()
    return out


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)


class DestructiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.src = os.path.join(self.d, "src")
        self.dst = os.path.join(self.d, "dst")
        os.makedirs(self.dst)
        write(os.path.join(self.src, "a.txt"), "nouveau a")
        write(os.path.join(self.src, "sub", "b.txt"), "nouveau b")
        write(os.path.join(self.src, "sub", "c.txt"), "nouveau c")
        # en dehors de tout ce qu'on manipule : ne doit JAMAIS être touché
        self.outside = os.path.join(self.d, "precieux")
        write(os.path.join(self.outside, "ne-pas-effacer.txt"), "important")
        self.outside_snapshot = tree(self.outside)

    def tearDown(self):
        self.assertEqual(tree(self.outside), self.outside_snapshot, "un fichier hors opération a été modifié")
        self.tmp.cleanup()

    # -- remplacer ---------------------------------------------------------
    def test_replace_dir_by_file(self):
        write(os.path.join(self.dst, "a.txt", "dedans.txt"), "dossier existant")   # dossier nommé a.txt
        run(lambda j: fileops.transfer(j, [os.path.join(self.src, "a.txt")], self.dst, ask("replace")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "a.txt")))
        self.assertEqual(open(os.path.join(self.dst, "a.txt")).read(), "nouveau a")

    def test_replace_file_by_dir(self):
        write(os.path.join(self.dst, "sub"), "fichier existant")
        run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("replace")))
        self.assertEqual(tree(os.path.join(self.dst, "sub")), tree(os.path.join(self.src, "sub")))

    def test_skip_keeps_everything(self):
        write(os.path.join(self.dst, "src", "a.txt"), "ancien a")
        write(os.path.join(self.dst, "src", "sub", "b.txt"), "ancien b")
        before = tree(self.dst)
        run(lambda j: fileops.transfer(j, [self.src], self.dst, ask("skip")))
        self.assertEqual(tree(self.dst), before)

    def test_replace_failure_keeps_existing(self):
        """Disque plein au milieu d'un remplacement : l'élément existant doit rester intact."""
        write(os.path.join(self.dst, "sub", "b.txt"), "ancien b")
        write(os.path.join(self.dst, "sub", "seul-ici.txt"), "ancien seul")
        before = tree(os.path.join(self.dst, "sub"))
        real = fileops._Copier.copy_file

        def full_disk(self_, src, dst, s):
            if src.endswith("c.txt"):
                raise OSError(28, "No space left on device")
            return real(self_, src, dst, s)
        fileops._Copier.copy_file = full_disk
        try:
            j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst,
                                                     ask("replace"))).run_sync()
        finally:
            fileops._Copier.copy_file = real
        self.assertEqual(tree(os.path.join(self.dst, "sub")), before, "remplacement raté : ancien perdu")
        self.assertFalse([n for n in os.listdir(self.dst) if ".odysseus-" in n], "fichier temporaire laissé")
        self.assertTrue(os.path.exists(os.path.join(self.src, "sub", "c.txt")))
        self.assertIn(j.state, ("done", "failed"))

    def test_move_replace_same_disk(self):
        write(os.path.join(self.dst, "sub", "vieux.txt"), "vieux")
        run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("replace"), move=True))
        self.assertFalse(os.path.exists(os.path.join(self.src, "sub")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.dst, "sub"))), ["b.txt", "c.txt"])
        self.assertFalse([n for n in os.listdir(self.dst) if ".odysseus-" in n])

    # -- fusionner ---------------------------------------------------------
    def test_merge_nested_skip_files(self):
        """Fusion des dossiers, mais les fichiers en conflit gardent l'ancienne version."""
        write(os.path.join(self.dst, "src", "sub", "b.txt"), "ancien b")
        write(os.path.join(self.dst, "src", "sub", "garde.txt"), "à garder")
        answers = {"dir": ("merge", False), "file": ("skip", False)}
        run(lambda j: fileops.transfer(j, [self.src], self.dst,
                                       lambda c: answers["dir" if c.src_is_dir and c.dst_is_dir else "file"]))
        sub = os.path.join(self.dst, "src", "sub")
        self.assertEqual(open(os.path.join(sub, "b.txt")).read(), "ancien b")
        self.assertEqual(open(os.path.join(sub, "garde.txt")).read(), "à garder")
        self.assertEqual(open(os.path.join(sub, "c.txt")).read(), "nouveau c")
        self.assertEqual(open(os.path.join(self.dst, "src", "a.txt")).read(), "nouveau a")

    def test_merge_nested_replace_files(self):
        write(os.path.join(self.dst, "src", "sub", "b.txt"), "ancien b")
        write(os.path.join(self.dst, "src", "sub", "garde.txt"), "à garder")
        run(lambda j: fileops.transfer(j, [self.src], self.dst,
                                       lambda c: ("merge", False) if c.src_is_dir and c.dst_is_dir
                                       else ("replace", False)))
        sub = os.path.join(self.dst, "src", "sub")
        self.assertEqual(open(os.path.join(sub, "b.txt")).read(), "nouveau b")
        self.assertEqual(open(os.path.join(sub, "garde.txt")).read(), "à garder")

    def test_move_merge_keeps_unmoved_sources(self):
        """Déplacer en fusionnant avec « ignorer » : ce qui n'a pas été déplacé reste à la source."""
        write(os.path.join(self.dst, "src", "sub", "b.txt"), "ancien b")
        run(lambda j: fileops.transfer(j, [self.src], self.dst,
                                       lambda c: ("merge", False) if c.src_is_dir and c.dst_is_dir
                                       else ("skip", False), move=True))
        self.assertEqual(open(os.path.join(self.src, "sub", "b.txt")).read(), "nouveau b")
        self.assertEqual(open(os.path.join(self.dst, "src", "sub", "b.txt")).read(), "ancien b")
        self.assertTrue(os.path.exists(os.path.join(self.dst, "src", "sub", "c.txt")))

    # -- annuler en cours de route ----------------------------------------------
    def _big(self, path, mb=300):
        with open(path, "wb") as fh:
            block = os.urandom(1 << 20)
            for _ in range(mb):
                fh.write(block)

    def _cancel_during(self, func, delay=0.15):
        j = Job("x", func)
        th = threading.Thread(target=j.run_sync)
        th.start()
        time.sleep(delay)
        j.cancel()
        th.join(30)
        self.assertFalse(th.is_alive(), "l'annulation n'a pas abouti")
        return j

    def test_cancel_copy_leaves_no_partial_file(self):
        big = os.path.join(self.src, "gros.bin")
        self._big(big)
        h = digest(big)
        j = self._cancel_during(lambda j: fileops.transfer(j, [big], self.dst, ask("cancel")))
        self.assertEqual(j.state, "cancelled")
        self.assertEqual(digest(big), h, "la source a été modifiée")
        left = os.listdir(self.dst)
        self.assertFalse(left, f"fichier partiel laissé : {left}")

    def test_cancel_move_keeps_source(self):
        other = "/dev/shm" if os.path.isdir("/dev/shm") and \
            os.stat("/dev/shm").st_dev != os.stat(self.d).st_dev else None
        if other is None:
            self.skipTest("pas de second système de fichiers")
        big = os.path.join(self.src, "gros.bin")
        self._big(big, 200)
        h = digest(big)
        dest = tempfile.mkdtemp(dir=other)
        try:
            self._cancel_during(lambda j: fileops.transfer(j, [self.src], dest, ask("cancel"), move=True),
                                delay=0.05)
            # quel que soit le moment de l'annulation : chaque fichier existe, intact, d'un côté ou de l'autre
            for rel in ("gros.bin", os.path.join("sub", "b.txt"), os.path.join("sub", "c.txt"), "a.txt"):
                here, there = os.path.join(self.src, rel), os.path.join(dest, "src", rel)
                self.assertTrue(os.path.exists(here) or os.path.exists(there), f"{rel} perdu")
            if os.path.exists(big):
                self.assertEqual(digest(big), h, "source abîmée par un déplacement annulé")
            else:
                self.assertEqual(digest(os.path.join(dest, "src", "gros.bin")), h)
        finally:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)

    # -- déplacer entre deux systèmes de fichiers ----------------------------------
    def test_move_across_filesystems(self):
        other = "/dev/shm" if os.path.isdir("/dev/shm") and \
            os.stat("/dev/shm").st_dev != os.stat(self.d).st_dev else None
        if other is None:
            self.skipTest("pas de second système de fichiers")
        before = tree(self.src)
        dest = tempfile.mkdtemp(dir=other)
        try:
            run(lambda j: fileops.transfer(j, [self.src], dest, ask("cancel"), move=True))
            self.assertFalse(os.path.exists(self.src))
            self.assertEqual(tree(os.path.join(dest, "src")), before)
        finally:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)

    # -- liens symboliques ----------------------------------------------------------
    def test_delete_folder_with_link_to_outside(self):
        os.symlink(self.outside, os.path.join(self.src, "lien-dossier"))
        os.symlink(os.path.join(self.outside, "ne-pas-effacer.txt"), os.path.join(self.src, "lien-fichier"))
        run(lambda j: fileops.delete(j, [self.src]))
        self.assertFalse(os.path.lexists(self.src))
        # tearDown vérifie que « precieux » est intact

    def test_delete_link_itself(self):
        link = os.path.join(self.d, "lien")
        os.symlink(self.outside, link)
        run(lambda j: fileops.delete(j, [link]))
        self.assertFalse(os.path.lexists(link))

    def test_copy_and_move_keep_links_as_links(self):
        os.symlink(self.outside, os.path.join(self.src, "lien-dossier"))
        run(lambda j: fileops.transfer(j, [self.src], self.dst, ask("cancel")))
        copied = os.path.join(self.dst, "src", "lien-dossier")
        self.assertTrue(os.path.islink(copied))
        self.assertEqual(os.readlink(copied), self.outside)

    def test_replace_through_link_does_not_touch_target(self):
        """Remplacer un lien qui pointe vers un dossier : on remplace le lien, pas le contenu pointé."""
        os.symlink(self.outside, os.path.join(self.dst, "sub"))
        run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("replace")))
        self.assertFalse(os.path.islink(os.path.join(self.dst, "sub")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "sub", "b.txt")))

    def test_merge_into_link_does_not_write_into_target(self):
        """Fusionner dans un lien vers un dossier extérieur ne doit rien écrire dans ce dossier."""
        os.symlink(self.outside, os.path.join(self.dst, "sub"))
        run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst,
                                       lambda c: ("merge", False) if c.src_is_dir and c.dst_is_dir
                                       else ("skip", False)))

    # -- noms déjà pris -------------------------------------------------------------
    def test_rename_onto_existing_refuses(self):
        a, b = os.path.join(self.d, "un.txt"), os.path.join(self.d, "deux.txt")
        write(a, "un")
        write(b, "deux")
        with self.assertRaises(Exception):
            fileops.rename(a, "deux.txt")
        self.assertEqual(open(b).read(), "deux")
        self.assertEqual(open(a).read(), "un")

    def test_make_folder_and_file_on_existing_refuse(self):
        write(os.path.join(self.d, "pris.txt"), "contenu")
        with self.assertRaises(Exception):
            fileops.make_file(self.d, "pris.txt")
        with self.assertRaises(Exception):
            fileops.make_folder(self.d, "pris.txt")
        self.assertEqual(open(os.path.join(self.d, "pris.txt")).read(), "contenu")

    def test_move_into_itself_refuses(self):
        before = tree(self.src)
        j = Job("x", lambda j: fileops.transfer(j, [self.src], os.path.join(self.src, "sub"), ask("cancel"),
                                                 move=True)).run_sync()
        self.assertEqual(j.state, "failed")
        self.assertEqual(tree(self.src), before)

    # -- corbeille et annulation ------------------------------------------------------
    def _trash_supported(self):
        try:
            f = Gio.File.new_for_path(os.path.join(self.d, "essai"))
            write(f.get_path(), "x")
            f.trash(None)
            return True
        except Exception:  # noqa: BLE001
            return False

    def test_trash_then_restore_does_not_overwrite_new_file(self):
        if not self._trash_supported() or "trash" not in Gio.Vfs.get_default().get_supported_uri_schemes():
            self.skipTest("corbeille GIO indisponible")
        p = os.path.join(self.d, "doc.txt")
        write(p, "version jetée")
        res = run(lambda j: fileops.trash(j, [p]))
        self.assertFalse(os.path.exists(p))
        write(p, "nouvelle version")          # nom réutilisé entre-temps
        run(lambda j: fileops.restore_trashed_paths(j, list(res.trashed)))
        self.assertEqual(open(p).read(), "nouvelle version", "la restauration a écrasé le nouveau fichier")
        restored = [n for n in os.listdir(self.d) if n.startswith("doc") and n != "doc.txt"]
        self.assertTrue(restored, "l'élément jeté n'a pas été restauré à côté")


class FatKeyTest(unittest.TestCase):
    """Copie vers une « clé USB » FAT32 : vraie image mkfs.vfat montée avec fusefat (sans droits
    root ni module noyau). Ignoré si les outils manquent."""

    def setUp(self):
        if not (shutil.which("mkfs.vfat") and shutil.which("fusefat") and shutil.which("fusermount")):
            self.skipTest("mkfs.vfat / fusefat indisponibles")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        img = os.path.join(self.d, "cle.img")
        with open(img, "wb") as fh:
            fh.truncate(32 * 1024 * 1024)
        subprocess.run(["mkfs.vfat", "-F", "32", img], check=True, capture_output=True)
        self.key = os.path.join(self.d, "cle")
        os.makedirs(self.key)
        r = subprocess.run(["fusefat", "-o", "rw+", img, self.key], capture_output=True)
        if r.returncode or fsinfo.fs_type(self.key) not in fsinfo.FAT:
            self.tmp.cleanup()
            self.skipTest("montage fusefat impossible")
        self.src = os.path.join(self.d, "src")
        os.makedirs(self.src)

    def tearDown(self):
        subprocess.run(["fusermount", "-u", self.key], capture_output=True)
        self.tmp.cleanup()

    def copy(self, names, move=False):
        return Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, n) for n in names], self.key,
                                                    ask("cancel"), move=move)).run_sync()

    def test_detected_as_fat(self):
        self.assertTrue(fsinfo.is_fat(self.key))
        self.assertTrue(fsinfo.is_windows_like(os.path.join(self.key, "pas", "encore", "là")))
        self.assertFalse(fsinfo.is_fat(self.src))

    def test_names_differing_by_case_do_not_overwrite(self):
        write(os.path.join(self.src, "Photo.jpg"), "majuscule")
        write(os.path.join(self.src, "photo.jpg"), "minuscule")
        write(os.path.join(self.src, "album", "IMG.png"), "1")
        write(os.path.join(self.src, "album", "img.png"), "2")
        j = self.copy(["Photo.jpg", "photo.jpg", "album"])
        self.assertEqual(j.state, "done", j.error)
        top = sorted(n for n in os.listdir(self.key) if n.lower().startswith("photo"))
        self.assertEqual(len(top), 2, top)
        self.assertEqual(sorted(open(os.path.join(self.key, n)).read() for n in top), ["majuscule", "minuscule"])
        self.assertEqual(sorted(open(os.path.join(self.key, "album", n)).read()
                                for n in os.listdir(os.path.join(self.key, "album"))), ["1", "2"])

    def test_forbidden_characters_replaced(self):
        write(os.path.join(self.src, "rapport: v2?.txt"), "r")
        write(os.path.join(self.src, "dossier*", "a|b.txt"), "ab")
        write(os.path.join(self.src, "fin."), "f")
        j = self.copy(["rapport: v2?.txt", "dossier*", "fin."])
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual(tree(self.key), {"rapport_ v2_.txt": b"r", "dossier_": "<dossier>",
                                          os.path.join("dossier_", "a_b.txt"): b"ab", "fin": b"f"})

    def test_symlink_gives_clear_error_and_move_keeps_it(self):
        write(os.path.join(self.src, "projet", "vrai.txt"), "v")
        os.symlink("vrai.txt", os.path.join(self.src, "projet", "lien"))
        j = self.copy(["projet"], move=True)
        self.assertEqual(j.state, "done", j.error)
        self.assertTrue(any("liens symboliques" in m or "symbolic" in m for _p, m in j.result.errors),
                        j.result.errors)
        self.assertEqual(open(os.path.join(self.key, "projet", "vrai.txt")).read(), "v")
        # le lien n'a pas pu être copié : il reste à la source (et seulement lui)
        self.assertTrue(os.path.islink(os.path.join(self.src, "projet", "lien")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "projet", "vrai.txt")))

    def test_file_over_4gb_refused_up_front(self):
        big = os.path.join(self.src, "film.mkv")
        with open(big, "wb") as fh:
            fh.truncate(fsinfo.FAT_MAX_FILE + 1)      # fichier creux : rien à lire
        write(os.path.join(self.src, "petit.txt"), "p")
        t = time.monotonic()
        j = self.copy(["film.mkv", "petit.txt"], move=True)
        self.assertLess(time.monotonic() - t, 5, "le refus doit venir avant la copie")
        self.assertEqual(j.state, "done", j.error)
        self.assertTrue(any("4" in m for _p, m in j.result.errors), j.result.errors)
        self.assertEqual(sorted(os.listdir(self.key)), ["petit.txt"], "fichier partiel laissé sur la clé")
        self.assertEqual(os.path.getsize(big), fsinfo.FAT_MAX_FILE + 1, "source touchée")

    def test_rename_changing_only_case(self):
        write(os.path.join(self.key, "photo.jpg"), "x")
        out = fileops.rename(os.path.join(self.key, "photo.jpg"), "Photo.jpg")
        self.assertEqual(os.listdir(self.key), ["Photo.jpg"])
        self.assertEqual(open(out).read(), "x")
        write(os.path.join(self.key, "autre.jpg"), "y")
        with self.assertRaises(Exception):
            fileops.rename(os.path.join(self.key, "autre.jpg"), "PHOTO.JPG")    # vrai conflit
        self.assertEqual(open(os.path.join(self.key, "Photo.jpg")).read(), "x")


class FsInfoTest(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(fsinfo.safe_name('a:b?c*d"e<f>g|h\\i'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(fsinfo.safe_name("fin. . "), "fin")
        self.assertEqual(fsinfo.safe_name("..."), "_")
        self.assertEqual(fsinfo.safe_name("Été 2024 — vacances.jpg"), "Été 2024 — vacances.jpg")

    def test_fs_type_longest_prefix(self):
        self.assertIsNotNone(fsinfo.fs_type("/"))
        pts = [p for p, _k in fsinfo.mounts()]
        self.assertEqual(pts, sorted(pts, key=len, reverse=True))
        self.assertEqual(fsinfo.fs_type("/proc/self"), "proc")


if __name__ == "__main__":
    unittest.main()
