"""Opérations destructrices, sur un arbre jetable : là où une erreur coûte des données.

Remplacer, fusionner, annuler en cours de route, déplacer entre deux systèmes de fichiers,
liens symboliques (ne jamais toucher la cible), renommer sur un nom pris, corbeille et annulation.
"""
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import Gio  # noqa: E402

from odysseus.backend import fileops  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
