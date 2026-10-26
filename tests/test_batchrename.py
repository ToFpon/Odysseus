"""Renommage de plusieurs éléments (comme Nautilus) : modèle, rechercher / remplacer, conflits,
renommage sûr (chaînes, permutations, jamais d'écrasement) et métadonnées EXIF."""
import os
import shutil
import struct
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import batchrename as br  # noqa: E402


def T(tag):
    return br.tag_text(tag)


class TemplateTest(unittest.TestCase):
    def test_split(self):
        text = T("num2") + " Paris " + T("creation_date") + "[pas une balise]"
        self.assertEqual(br.split_template(text),
                         [("tag", "num2"), ("text", " Paris "), ("tag", "creation_date"), ("text", "[pas une balise]")])
        self.assertTrue(br.uses_numbering(text))
        self.assertFalse(br.uses_numbering(T("original")))
        start = len(T("num2")) + len(" Paris ")
        self.assertEqual(br.tag_spans(text), [(0, len(T("num2"))), (start, start + len(T("creation_date")))])

    def test_names_keep_extension(self):
        items = [br.Item("/x/b.jpg"), br.Item("/x/a.tar.gz"), br.Item("/x/zdossier.d", is_dir=True),
                 br.Item("/x/c (10).jpg"), br.Item("/x/c (2).jpg")]
        got = br.template_names(items, "Vacances " + T("num3"), "name_asc")
        self.assertEqual(got, {"/x/a.tar.gz": "Vacances 001.tar.gz", "/x/b.jpg": "Vacances 002.jpg",
                               "/x/c (2).jpg": "Vacances 003.jpg", "/x/c (10).jpg": "Vacances 004.jpg",
                               "/x/zdossier.d": "Vacances 005"})
        got = br.template_names(items, T("original") + "-" + T("num1"), "name_desc")
        self.assertEqual(got["/x/zdossier.d"], "zdossier.d-1")
        self.assertEqual(got["/x/a.tar.gz"], "a-5.tar.gz")
        # modèle vide : le nom ne change pas
        self.assertEqual(br.template_names(items, "", "name_asc")["/x/b.jpg"], "b.jpg")

    def test_order_by_dates(self):
        a = br.Item("/x/a", mtime=30, meta={"created": datetime(2020, 1, 2)})
        b = br.Item("/x/b", mtime=10, meta={"created": datetime(2021, 1, 2)})
        c = br.Item("/x/c", mtime=20, meta={"created": datetime(2019, 1, 2)})
        names = lambda s: [i.name for i in br.ordered([a, b, c], s)]  # noqa: E731
        self.assertEqual(names("first_modified"), ["b", "c", "a"])
        self.assertEqual(names("last_modified"), ["a", "c", "b"])
        self.assertEqual(names("first_created"), ["c", "a", "b"])
        self.assertEqual(names("last_created"), ["b", "a", "c"])

    def test_metadata_tags(self):
        a = br.Item("/x/1.mp3", meta={"artist": "Brel", "track": "3/12", "album": "Ces gens-là"})
        b = br.Item("/x/2.mp3", meta={"artist": "Brel", "track": "11"})
        self.assertEqual(br.available_tags([a, b]), ["track", "artist"])
        got = br.template_names([a, b], T("track") + " - " + T("artist"), "name_asc")
        self.assertEqual(got, {"/x/1.mp3": "03 - Brel.mp3", "/x/2.mp3": "11 - Brel.mp3"})

    def test_replace(self):
        items = [br.Item("/x/IMG_001.JPG"), br.Item("/x/IMG_IMG.png"), br.Item("/x/autre.txt")]
        got = br.replace_names(items, "IMG", "Photo")
        self.assertEqual(got, {"/x/IMG_001.JPG": "Photo_001.JPG", "/x/IMG_IMG.png": "Photo_Photo.png",
                               "/x/autre.txt": "autre.txt"})
        self.assertEqual(br.replace_names(items, "", "Photo")["/x/autre.txt"], "autre.txt")


class DiskTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def make(self, *names):
        for n in names:
            with open(os.path.join(self.d, n), "w") as f:
                f.write(n)
        return [br.Item(os.path.join(self.d, n)) for n in names]

    def content(self, name):
        with open(os.path.join(self.d, name)) as f:
            return f.read()

    def test_conflicts(self):
        items = self.make("a.txt", "b.txt", "c.txt")
        self.make("garde.txt")
        p = {it.name: it.path for it in items}
        new = {p["a.txt"]: "x.txt", p["b.txt"]: "x.txt", p["c.txt"]: "garde.txt"}
        got = br.conflicts(items, new)
        unique = br.tr("« {0} » ne serait pas un nouveau nom unique.").format("x.txt")
        taken = br.tr("« {0} » créerait un conflit avec un fichier existant.").format("garde.txt")
        self.assertEqual((got[p["a.txt"]], got[p["b.txt"]], got[p["c.txt"]]), (unique, unique, taken))
        # un nom pris par un élément qui est lui-même renommé : pas un conflit
        got = br.conflicts(items, {p["a.txt"]: "b.txt", p["b.txt"]: "c.txt", p["c.txt"]: "a.txt"})
        self.assertEqual(got, {})
        got = br.conflicts(items[:1], {p["a.txt"]: "a/b"})
        self.assertIn("/", got[p["a.txt"]])
        got = br.conflicts(items[:1], {p["a.txt"]: ".."})
        self.assertIn("..", got[p["a.txt"]])

    def test_conflicts_case_insensitive_fs(self):
        items = self.make("a.txt")
        self.make("Garde.txt")
        with mock.patch.object(br.fsinfo, "is_windows_like", return_value=True):
            self.assertIn(items[0].path, br.conflicts(items, {items[0].path: "GARDE.txt"}))
        self.assertEqual(br.conflicts(items, {items[0].path: "GARDE.txt"}), {})

    def test_chain_swap_and_cycle(self):
        items = self.make("a", "b", "c", "x", "y")
        p = {it.name: it.path for it in items}
        # chaîne a → b → c → d, et permutation x ↔ y
        done, errors = br.apply([(p["a"], "b"), (p["b"], "c"), (p["c"], "d"), (p["x"], "y"), (p["y"], "x")])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(os.listdir(self.d)), ["b", "c", "d", "x", "y"])
        self.assertEqual([self.content(n) for n in ("b", "c", "d", "x", "y")], ["a", "b", "c", "y", "x"])
        # annuler = appliquer à l'envers
        back, errors = br.apply([(new, os.path.basename(old)) for old, new in done])
        self.assertEqual(errors, [])
        self.assertEqual(sorted(os.listdir(self.d)), ["a", "b", "c", "x", "y"])
        self.assertEqual([self.content(n) for n in ("a", "b", "c", "x", "y")], ["a", "b", "c", "x", "y"])

    def test_never_overwrites(self):
        items = self.make("a", "b")
        p = {it.name: it.path for it in items}
        self.make("pris")
        done, errors = br.apply([(p["a"], "pris"), (p["b"], "libre")])
        self.assertEqual([os.path.basename(e[0]) for e in errors], ["a"])
        self.assertEqual(self.content("pris"), "pris")
        self.assertEqual(self.content("a"), "a")
        self.assertEqual(self.content("libre"), "b")
        self.assertEqual(done, [(p["b"], os.path.join(self.d, "libre"))])
        self.assertFalse([n for n in os.listdir(self.d) if n.startswith(".odysseus")])

    def test_case_only_change(self):
        items = self.make("photo.jpg")
        with mock.patch.object(br.fsinfo, "is_windows_like", return_value=True):
            done, errors = br.apply([(items[0].path, "Photo.jpg")])
        self.assertEqual(errors, [])
        self.assertEqual(os.listdir(self.d), ["Photo.jpg"])


def _jpeg_with_exif(model: str, when: str) -> bytes:
    """JPEG minimal avec un bloc EXIF : IFD0 (Model, pointeur Exif), IFD Exif (DateTimeOriginal)."""
    model_b = model.encode() + b"\0"
    when_b = when.encode() + b"\0"
    ifd0_off = 8
    ifd0_size = 2 + 2 * 12 + 4
    exif_off = ifd0_off + ifd0_size
    exif_size = 2 + 12 + 4
    data_off = exif_off + exif_size
    model_off = data_off
    when_off = model_off + len(model_b)
    tiff = b"II*\0" + struct.pack("<I", ifd0_off)
    tiff += struct.pack("<H", 2)
    tiff += struct.pack("<HHII", 0x0110, 2, len(model_b), model_off)
    tiff += struct.pack("<HHII", 0x8769, 4, 1, exif_off)
    tiff += struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(when_b), when_off) + struct.pack("<I", 0)
    tiff += model_b + when_b
    app1 = b"Exif\0\0" + tiff
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"\0\0" + \
        b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"


class ExifTest(unittest.TestCase):
    def test_read_exif(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "p.jpg")
        with open(path, "wb") as f:
            f.write(_jpeg_with_exif("Canon EOS 80D", "2024:07:14 18:30:05"))
        meta = br.read_metadata(path)
        self.assertEqual(meta["camera"], "Canon EOS 80D")
        self.assertEqual(meta["created"], datetime(2024, 7, 14, 18, 30, 5))
        it = br.Item(path, meta=meta)
        self.assertEqual(br.available_tags([it]), ["creation_date", "camera"])
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe1\x00")                # tronqué : pas d'exception
        self.assertEqual(br.read_metadata(path), {})


if __name__ == "__main__":
    unittest.main()
