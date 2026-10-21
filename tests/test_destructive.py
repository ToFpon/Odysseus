"""Opérations destructrices, sur un arbre jetable : là où une erreur coûte des données.

Remplacer, fusionner, annuler en cours de route, déplacer entre deux systèmes de fichiers,
liens symboliques (ne jamais toucher la cible), renommer sur un nom pris, corbeille et annulation.
"""
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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


def _purge_trash(prefix):
    """Les tests de corbeille passent par la VRAIE corbeille (GIO) : on en retire ce qu'ils y ont mis."""
    for info, f in fileops.trash_items(None):
        orig = info.get_attribute_byte_string("trash::orig-path") or ""
        if orig == prefix or orig.startswith(prefix.rstrip("/") + "/"):
            try:
                f.delete(None)
            except Exception:  # noqa: BLE001
                pass


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
        _purge_trash(self.d)
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

    def test_replace_dir_name_never_missing(self):
        """Dossier remplacé par un dossier : échange atomique, le nom visé existe à tout instant."""
        write(os.path.join(self.dst, "sub", "vieux.txt"), "vieux")
        target = os.path.join(self.dst, "sub")
        gaps, real = [], os.rename

        def watched(a, b, *k, **kw):
            real(a, b, *k, **kw)
            if not os.path.lexists(target):
                gaps.append((a, b))
        os.rename = watched
        try:
            run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("replace")))
        finally:
            os.rename = real
        self.assertEqual(gaps, [], "le dossier visé a disparu entre deux renommages")
        self.assertEqual(tree(target), tree(os.path.join(self.src, "sub")))
        self.assertFalse([n for n in os.listdir(self.dst) if ".odysseus-" in n])

    def test_replace_dir_without_exchange_support(self):
        """Système de fichiers sans RENAME_EXCHANGE (FAT…) : repli en deux renommages, rien perdu."""
        write(os.path.join(self.dst, "sub", "vieux.txt"), "vieux")
        real = fileops._exchange
        fileops._exchange = lambda a, b: False
        try:
            run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("replace")))
        finally:
            fileops._exchange = real
        self.assertEqual(tree(os.path.join(self.dst, "sub")), tree(os.path.join(self.src, "sub")))
        self.assertFalse([n for n in os.listdir(self.dst) if ".odysseus-" in n])

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

    def _cancel_during(self, func, after=8 << 20):
        """Annule la tâche quand « after » octets ont été copiés : au milieu du gros fichier, quelle
        que soit la vitesse du disque (un délai fixe ne marche plus quand la copie est trop rapide)."""
        j = Job("x", func)
        real = fileops._Copier.tick

        def tick(self_, force=False):
            if self_.done >= after:
                j.cancel()
            return real(self_, force)
        fileops._Copier.tick = tick
        try:
            th = threading.Thread(target=j.run_sync)
            th.start()
            th.join(30)
        finally:
            fileops._Copier.tick = real
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
            self._cancel_during(lambda j: fileops.transfer(j, [self.src], dest, ask("cancel"), move=True))
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

    def test_merge_reports_only_what_it_created(self):
        """Fusion : l'annulation ne doit viser que ce qui a été ajouté, pas le dossier existant."""
        write(os.path.join(self.dst, "sub", "garde.txt"), "à garder")
        res = run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("merge")))
        self.assertEqual(sorted(res.new_items), sorted([os.path.join(self.dst, "sub", "b.txt"),
                                                        os.path.join(self.dst, "sub", "c.txt")]))
        res = run(lambda j: fileops.transfer(j, [os.path.join(self.src, "a.txt")], self.dst, ask("skip")))
        self.assertEqual(res.new_items, [os.path.join(self.dst, "a.txt")])

    def test_move_merge_reports_leaf_moves(self):
        write(os.path.join(self.dst, "sub", "garde.txt"), "à garder")
        res = run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("merge"),
                                             move=True))
        self.assertEqual(sorted(res.new_moves),
                         sorted([(os.path.join(self.src, "sub", n), os.path.join(self.dst, "sub", n))
                                 for n in ("b.txt", "c.txt")]))

    def test_copy_into_new_folder_reports_the_folder(self):
        res = run(lambda j: fileops.transfer(j, [os.path.join(self.src, "sub")], self.dst, ask("cancel")))
        self.assertEqual(res.new_items, [os.path.join(self.dst, "sub")])

    def test_move_link_into_its_target(self):
        target = os.path.join(self.d, "cible")
        os.makedirs(target)
        link = os.path.join(self.src, "raccourci")
        os.symlink(target, link)
        run(lambda j: fileops.transfer(j, [link], target, ask("cancel"), move=True))
        self.assertEqual(os.readlink(os.path.join(target, "raccourci")), target)
        self.assertFalse(os.path.lexists(link))

    def test_move_into_itself_refuses(self):
        before = tree(self.src)
        j = Job("x", lambda j: fileops.transfer(j, [self.src], os.path.join(self.src, "sub"), ask("cancel"),
                                                 move=True)).run_sync()
        self.assertEqual(j.state, "failed")
        self.assertEqual(tree(self.src), before)

    # -- liens symboliques vers le même dossier, remplacements qui échouent -------------------
    def test_move_to_same_folder_through_link_changes_nothing(self):
        docs = os.path.join(self.d, "Documents")
        write(os.path.join(docs, "Projet", "rapport.txt"), "précieux")
        write(os.path.join(docs, "Projet", "sub", "x.txt"), "x")
        os.symlink(docs, os.path.join(self.d, "Docs-lien"))
        before = tree(docs)
        for answer in ("replace", "merge"):
            run(lambda j: fileops.transfer(j, [os.path.join(docs, "Projet")], os.path.join(self.d, "Docs-lien"),
                                           ask(answer), move=True))
            self.assertEqual(tree(docs), before, f"déplacement sur lui-même ({answer})")

    def test_copy_to_same_folder_through_link_makes_a_copy(self):
        docs = os.path.join(self.d, "Documents")
        write(os.path.join(docs, "rapport.txt"), "précieux")
        os.symlink(docs, os.path.join(self.d, "Docs-lien"))
        run(lambda j: fileops.transfer(j, [os.path.join(docs, "rapport.txt")], os.path.join(self.d, "Docs-lien"),
                                       ask("merge")))
        self.assertEqual(open(os.path.join(docs, "rapport.txt")).read(), "précieux")
        self.assertEqual(len(os.listdir(docs)), 2, "« rapport (copie).txt » attendu")

    def test_failed_nested_replace_keeps_sources_on_move(self):
        other = tempfile.mkdtemp(dir="/dev/shm") if os.path.isdir("/dev/shm") else None
        if not other or os.stat(other).st_dev == os.stat(self.d).st_dev:
            self.skipTest("pas de second système de fichiers")
        try:
            write(os.path.join(other, "A", "S", "f1.txt"), "f1")
            write(os.path.join(other, "A", "S", "f2.txt"), "f2")
            os.mkfifo(os.path.join(other, "A", "S", "tube"))       # erreur : le remplacement échoue
            write(os.path.join(self.dst, "A", "S"), "un fichier S")
            answers = iter(["merge", "replace"])
            Job("x", lambda j: fileops.transfer(j, [os.path.join(other, "A")], self.dst,
                                                lambda c: (next(answers), False), move=True)).run_sync()
            for n in ("f1.txt", "f2.txt"):
                self.assertTrue(os.path.exists(os.path.join(other, "A", "S", n)), f"{n} perdu")
            self.assertEqual(open(os.path.join(self.dst, "A", "S")).read(), "un fichier S")
        finally:
            shutil.rmtree(other, ignore_errors=True)

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


class ConcurrentTest(unittest.TestCase):
    """Deux opérations lancées en même temps (une par fenêtre) vers le même nom. Une barrière
    retient chaque opération juste après sa vérification « le nom est libre » jusqu'à ce que
    l'autre ait vérifié aussi : les deux croient le nom libre. Avant : contenus mélangés dans un
    même fichier, ou premier fichier écrasé par le second renommage — et, pour un déplacement, les
    sources supprimées ensuite."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.dest = os.path.join(self.d, "dest")
        os.makedirs(self.dest)
        # deux fichiers DIFFÉRENTS du même nom, assez gros pour que les écritures se chevauchent
        self.contents = {"un": os.urandom(3 << 20), "deux": os.urandom(3 << 20)}
        self.sources = []
        for folder, data in self.contents.items():
            path = os.path.join(self.d, folder, "rapport.pdf")
            write(path, data)
            self.sources.append(path)

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self, dest, move, checks):
        target = os.path.join(dest, "rapport.pdf")
        barrier = threading.Barrier(2, timeout=5)
        real = os.path.lexists
        seen: dict = {}
        lock = threading.Lock()

        def lexists(p):
            if os.fspath(p) != target:
                return real(p)
            me = threading.get_ident()
            with lock:
                n = seen[me] = seen.get(me, 0) + 1
            if n <= checks:                        # vérifications d'avant la création : « libre »
                if n == checks:
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                return False
            return real(p)

        errors = []

        def go(src):
            try:
                run(lambda j: fileops.transfer(j, [src], dest, ask("rename", False), move=move))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        with mock.patch("os.path.lexists", lexists):
            threads = [threading.Thread(target=go, args=(s,)) for s in self.sources]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        got = sorted(tree(dest).values())
        self.assertEqual(got, sorted(self.contents.values()), "un fichier a été écrasé ou mélangé")
        self.assertEqual(sorted(os.listdir(dest)), ["rapport (2).pdf", "rapport.pdf"])

    def test_two_copies_same_name(self):
        self._race(self.dest, move=False, checks=1)
        for src in self.sources:
            self.assertTrue(os.path.exists(src))

    def test_two_moves_same_disk(self):
        self._race(self.dest, move=True, checks=2)
        for src in self.sources:
            self.assertFalse(os.path.exists(src))

    def test_two_moves_across_filesystems(self):
        other = "/dev/shm" if os.path.isdir("/dev/shm") and \
            os.stat("/dev/shm").st_dev != os.stat(self.d).st_dev else None
        if other is None:
            self.skipTest("pas de second système de fichiers")
        dest = tempfile.mkdtemp(dir=other)
        try:
            self._race(dest, move=True, checks=1)
            for src in self.sources:
                self.assertFalse(os.path.exists(src))
        finally:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)


class ConcurrentArchiveTest(unittest.TestCase):
    """Deux compressions lancées en même temps vers le même nom d'archive : avant, la seconde
    écrasait la première (tar.gz…) ou AJOUTAIT ses fichiers dedans (7z, commande « a »)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.sources = []
        for folder in ("un", "deux"):
            path = os.path.join(self.d, folder, f"fichier-{folder}.txt")
            write(path, folder * 1000)
            self.sources.append(path)

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self, fmt):
        from odysseus.backend import archive
        out = os.path.join(self.d, "sauvegarde." + fmt)
        barrier = threading.Barrier(2, timeout=5)
        real = os.path.lexists
        seen: dict = {}
        lock = threading.Lock()

        def lexists(p):
            if os.fspath(p) != out:
                return real(p)
            me = threading.get_ident()
            with lock:
                first = me not in seen
                seen[me] = True
            if first:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                return False
            return real(p)
        made, errors = [], []

        def go(src):
            try:
                made.append(run(lambda j: archive.create(j, [src], out, archive.CreateOptions(fmt=fmt))))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        with mock.patch("os.path.lexists", lexists):
            threads = [threading.Thread(target=go, args=(s,)) for s in self.sources]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(made)), 2, f"les deux compressions ont visé la même archive : {made}")
        contents = sorted(sorted(e.path for e in archive.list_archive(m).entries if not e.is_dir) for m in made)
        self.assertEqual(contents, [["fichier-deux.txt"], ["fichier-un.txt"]])
        self.assertEqual(sorted(n for n in os.listdir(self.d) if n.startswith(".")), [], "temporaire oublié")

    def test_two_7z_same_name(self):
        self._race("7z")

    def test_two_tar_gz_same_name(self):
        self._race("tar.gz")


class KilledTest(unittest.TestCase):
    """Odysseus tué net (SIGKILL : AppArmor, OOM, fin de session) au pire moment, dans un vrai
    processus séparé : rien de tronqué sous un vrai nom, aucune source supprimée sans que son
    résultat soit en place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _killed(self, code):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prog = ("import os, signal, sys\n"
                f"sys.path.insert(0, {root!r})\n"
                "from odysseus.backend import fileops, archive\n"
                "from odysseus.backend.engine import Job\n"
                "def die(*a, **k):\n    os.kill(os.getpid(), signal.SIGKILL)\n" + code)
        r = subprocess.run([sys.executable, "-c", prog], capture_output=True, timeout=120)
        self.assertEqual(r.returncode, -signal.SIGKILL, r.stderr.decode()[-500:])

    def test_copy_killed_midway_leaves_no_truncated_file(self):
        src = os.path.join(self.d, "src", "film.mkv")
        write(src, os.urandom(40 << 20))
        before = digest(src)
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        # tué après 16 Mo écrits sur 40
        self._killed(
            "real = fileops._Copier.tick\n"
            "def tick(self, force=False):\n"
            "    if self.done > (16 << 20):\n        die()\n"
            "    return real(self, force)\n"
            "fileops._Copier.tick = tick\n"
            f"Job('t', lambda j: fileops.transfer(j, [{src!r}], {dest!r}, lambda c: ('skip', True))).run_sync()\n")
        self.assertEqual(digest(src), before)
        visible = [n for n in os.listdir(dest) if not n.startswith(".")]
        self.assertEqual(visible, [], "fichier tronqué laissé sous son vrai nom")

    def test_7z_delete_after_killed_before_placing_keeps_sources(self):
        srcdir = os.path.join(self.d, "photos")
        for i in range(3):
            write(os.path.join(srcdir, f"img{i}.jpg"), os.urandom(4096))
        snap = tree(srcdir)
        out = os.path.join(self.d, "photos.7z")
        # tué juste avant que l'archive terminée soit posée sous son vrai nom
        self._killed(
            "fileops.place_new = die\n"
            f"o = archive.CreateOptions(fmt='7z', delete_after=True)\n"
            f"Job('t', lambda j: archive.create(j, [{srcdir!r}], {out!r}, o)).run_sync()\n")
        self.assertEqual(tree(srcdir), snap, "sources supprimées alors que l'archive n'est pas en place")


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

    def test_key_reached_through_symlink(self):
        """Clé atteinte par un lien (« ~/Clé → /media/… ») : reconnue comme FAT quand même."""
        link = os.path.join(self.d, "lien-cle")
        os.symlink(self.key, link)
        write(os.path.join(self.src, "rapport: v2?.txt"), "r")
        j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, "rapport: v2?.txt")], link,
                                                 ask("cancel"))).run_sync()
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual(os.listdir(self.key), ["rapport_ v2_.txt"])

    def _kernel_vfat_lookup(self):
        """Imite le vfat du noyau monté en utf8 (clés USB sous Ubuntu / Zorin) : chercher un nom
        tient compte de la casse, mais créer un nom qui ne diffère que par la casse est refusé.
        (fusefat, lui, retrouve le nom quelle que soit la casse.)"""
        real = os.path.lexists

        def lexists(p):
            folder, name = os.path.split(os.path.abspath(p))
            if folder.startswith(self.key) and real(folder) and os.path.isdir(folder):
                return name in os.listdir(folder)
            return real(p)
        os.path.lexists = lexists
        self.addCleanup(setattr, os.path, "lexists", real)

    def test_case_with_case_sensitive_lookup(self):
        """Le cas vu sur une vraie clé : « Photo.jpg : Le fichier existe » au lieu de « (2) »."""
        self._kernel_vfat_lookup()
        write(os.path.join(self.src, "Photo.jpg"), "majuscule")
        write(os.path.join(self.src, "photo.jpg"), "minuscule")
        j = self.copy(["Photo.jpg", "photo.jpg"])
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual(j.result.errors, [])
        self.assertEqual(sorted(open(os.path.join(self.key, n)).read() for n in os.listdir(self.key)),
                         ["majuscule", "minuscule"])

    def test_existing_name_in_other_case_is_a_conflict(self):
        """« PHOTO.JPG » déjà sur la clé, on copie « photo.jpg » : c'est un conflit (question), pas
        un écrasement silencieux ni une erreur."""
        self._kernel_vfat_lookup()
        write(os.path.join(self.key, "PHOTO.JPG"), "ancien")
        write(os.path.join(self.src, "photo.jpg"), "nouveau")
        seen = []

        def asker(c):
            seen.append(os.path.basename(c.dst))
            return ("skip", True)
        j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, "photo.jpg")], self.key,
                                                 asker)).run_sync()
        self.assertEqual(seen, ["PHOTO.JPG"])
        self.assertEqual(tree(self.key), {"PHOTO.JPG": b"ancien"})
        j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, "photo.jpg")], self.key,
                                                 ask("rename"))).run_sync()
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual(sorted(tree(self.key).values()), [b"ancien", b"nouveau"])

    def test_rename_case_only_with_case_sensitive_lookup(self):
        self._kernel_vfat_lookup()
        write(os.path.join(self.key, "photo.jpg"), "x")
        fileops.rename(os.path.join(self.key, "photo.jpg"), "Photo.jpg")
        self.assertEqual(os.listdir(self.key), ["Photo.jpg"])
        write(os.path.join(self.key, "autre.jpg"), "y")
        with self.assertRaises(Exception):
            fileops.rename(os.path.join(self.key, "autre.jpg"), "PHOTO.JPG")
        self.assertEqual(open(os.path.join(self.key, "Photo.jpg")).read(), "x")

    def test_cleaned_names_that_collide_are_renamed(self):
        """« a:b.txt » et « a_b.txt », « fin. » et « fin » : même nom une fois nettoyés."""
        for n in ("a:b.txt", "a_b.txt", "fin.", "fin"):
            write(os.path.join(self.src, "lot", n), n)
        j = self.copy(["lot"])
        self.assertEqual(j.state, "done", j.error)
        got = {n: open(os.path.join(self.key, "lot", n)).read() for n in os.listdir(os.path.join(self.key, "lot"))}
        self.assertEqual(sorted(got.values()), sorted(["a:b.txt", "a_b.txt", "fin.", "fin"]), got)
        self.assertEqual(len(got), 4)

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


class NtfsFuseTest(unittest.TestCase):
    """Image NTFS montée avec ntfs-3g : FUSE sans RENAME_EXCHANGE, le remplacement d'un dossier
    passe par le repli en deux renommages, sur un vrai système de fichiers. (fusefat ne convient pas
    ici : il perd le contenu d'un dossier non vide qu'on renomme.)"""

    def setUp(self):
        if not (shutil.which("mkfs.ntfs") and shutil.which("ntfs-3g") and shutil.which("fusermount")):
            self.skipTest("mkfs.ntfs / ntfs-3g indisponibles")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        img = os.path.join(self.d, "disque.img")
        with open(img, "wb") as fh:
            fh.truncate(16 * 1024 * 1024)
        subprocess.run(["mkfs.ntfs", "-F", "-Q", img], check=True, capture_output=True)
        self.key = os.path.join(self.d, "disque")
        os.makedirs(self.key)
        r = subprocess.run(["ntfs-3g", img, self.key], capture_output=True)
        if r.returncode or not (fsinfo.fs_type(self.key) or "").startswith("fuse"):
            self.tmp.cleanup()
            self.skipTest("montage ntfs-3g impossible")
        self.src = os.path.join(self.d, "src")

    def tearDown(self):
        subprocess.run(["fusermount", "-u", self.key], capture_output=True)
        self.tmp.cleanup()

    def test_replace_folder_without_exchange(self):
        write(os.path.join(self.key, "docs", "vieux.txt"), "vieux")
        write(os.path.join(self.src, "docs", "sous", "neuf.txt"), "neuf")
        os.makedirs(os.path.join(self.key, "essai"))
        self.assertFalse(fileops._exchange(os.path.join(self.key, "essai"), os.path.join(self.key, "docs")))
        os.rmdir(os.path.join(self.key, "essai"))
        j = Job("x", lambda j: fileops.transfer(j, [os.path.join(self.src, "docs")], self.key,
                                                 ask("replace"))).run_sync()
        self.assertEqual(j.state, "done", j.error)
        got = {k: v for k, v in tree(self.key).items() if not k.startswith("$")}
        self.assertEqual(got, {"docs": "<dossier>", os.path.join("docs", "sous"): "<dossier>",
                               os.path.join("docs", "sous", "neuf.txt"): b"neuf"})


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
