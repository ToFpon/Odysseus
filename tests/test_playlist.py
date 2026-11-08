"""Listes de lecture M3U (backend/playlist.py) : création, medley pris dans plusieurs dossiers."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import playlist  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}")
    return j.result


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def entries(m3u):
    with open(m3u, encoding="utf-8", errors="surrogateescape") as fh:
        return [l for l in fh.read().splitlines() if l and not l.startswith("#")]


class PlaylistTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="odysseus-m3u-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.music = os.path.join(self.root, "Musique")
        for rel in ("Album A/10 - Fin.mp3", "Album A/2 - Deux.mp3", "Album A/1 - Un.flac", "Album A/cover.jpg",
                    "Album A/CD2/1 - Bonus.ogg", "Album A/.cache.mp3",
                    "Album B/01 - Café.mp3", "Album B/02 - Été.m4a", "Album B/notes.txt"):
            touch(os.path.join(self.music, rel))
        # pas de ffprobe dans les tests : titres = noms de fichiers, durées inconnues (-1)
        real = playlist.shutil.which
        playlist.shutil.which = lambda name, *a, **k: None if name == "ffprobe" else real(name, *a, **k)
        self.addCleanup(setattr, playlist.shutil, "which", real)

    def test_folder_natural_order_relative_paths(self):
        a = os.path.join(self.music, "Album A")
        out = run(lambda j: playlist.create(j, playlist.collect([a], j), os.path.join(self.music, "A.m3u")))
        self.assertEqual(out, os.path.join(self.music, "A.m3u"))
        self.assertEqual(entries(out), ["Album A/1 - Un.flac", "Album A/2 - Deux.mp3", "Album A/10 - Fin.mp3",
                                        "Album A/CD2/1 - Bonus.ogg"])   # ordre naturel, CD2 après, ni jpg ni caché
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("#EXTM3U\n#EXTINF:-1,1 - Un\nAlbum A/1 - Un.flac\n"), text)

    def test_never_overwrites(self):
        b = os.path.join(self.music, "Album B")
        target = os.path.join(self.music, "B.m3u")
        touch(target, b"mien\n")
        out = run(lambda j: playlist.create(j, playlist.collect([b], j), target))
        self.assertEqual(os.path.basename(out), "B (2).m3u")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"mien\n")
        self.assertEqual(entries(out), ["Album B/01 - Café.mp3", "Album B/02 - Été.m4a"])   # accents en UTF-8

    def test_medley_from_several_folders(self):
        """Un medley : liste créée avec un morceau de A, puis complétée dossier par dossier, sans doublon ;
        la liste est ailleurs que la musique (chemins relatifs avec « .. »)."""
        lists = os.path.join(self.root, "Listes")
        os.makedirs(lists)
        m = os.path.join(lists, "Medley.m3u")
        a1 = os.path.join(self.music, "Album A", "2 - Deux.mp3")
        run(lambda j: playlist.create(j, [a1], m))
        b = [os.path.join(self.music, "Album B", n) for n in ("02 - Été.m4a", "01 - Café.mp3")]
        added, already = run(lambda j: playlist.append(j, m, b + [a1]))
        self.assertEqual((added, already), (2, 1))
        self.assertEqual(entries(m), ["../Musique/Album A/2 - Deux.mp3", "../Musique/Album B/02 - Été.m4a",
                                      "../Musique/Album B/01 - Café.mp3"])      # ordre d'ajout gardé
        # les chemins relatifs mènent bien aux morceaux
        for e in entries(m):
            self.assertTrue(os.path.isfile(os.path.join(lists, e)), e)
        self.assertEqual(sorted(os.listdir(lists)), ["Medley.m3u"])            # aucun fichier caché laissé
        self.assertEqual(run(lambda j: playlist.append(j, m, b)), (0, 2))

    def test_append_keeps_existing_lines(self):
        """Liste écrite par un autre programme (chemins absolus, URI, flux réseau, sans #EXTM3U) : gardée
        telle quelle, doublons reconnus quelle que soit leur écriture."""
        m = os.path.join(self.music, "autre.m3u")
        a1 = os.path.join(self.music, "Album A", "1 - Un.flac")
        a2 = os.path.join(self.music, "Album A", "2 - Deux.mp3")
        touch(m, (a1 + "\nfile://" + a2.replace(" ", "%20") + "\nhttp://radio.example/stream\n").encode())
        added, already = run(lambda j: playlist.append(j, m, [a1, a2, os.path.join(self.music, "Album B",
                                                                                   "01 - Café.mp3")]))
        self.assertEqual((added, already), (1, 2))
        with open(m, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[:3], [a1, "file://" + a2.replace(" ", "%20"), "http://radio.example/stream"])
        self.assertEqual(lines[3:], ["Album B/01 - Café.mp3"])      # pas de #EXTINF dans une liste simple

    def test_nothing_to_add(self):
        j = Job("test", lambda j: playlist.create(j, playlist.collect([os.path.join(self.music, "Album B",
                                                                                   "notes.txt")], j),
                                                  os.path.join(self.music, "vide.m3u"))).run_sync()
        self.assertEqual(j.state, "failed")
        self.assertFalse(os.path.exists(os.path.join(self.music, "vide.m3u")))


if __name__ == "__main__":
    unittest.main()
