"""Outils image (ImageMagick) : redimensionner, pivoter, convertir ; jamais d'écrasement, jamais
d'image à moitié écrite, « remplacer l'original » sûr."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import fileops, images  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}")
    return j.result


def size(path):
    out = subprocess.run(["identify", "-format", "%w %h %m|", path], capture_output=True, text=True).stdout
    w, h, fmt = out.split("|")[0].split()
    return int(w), int(h), fmt


@unittest.skipUnless(images.tool() and shutil.which("identify"), "ImageMagick absent")
class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def tearDown(self):
        from test_destructive import _purge_trash
        _purge_trash(self.d)

    def make(self, name, w=400, h=200, extra=()):
        p = os.path.join(self.d, name)
        subprocess.run(["convert", "-size", f"{w}x{h}", "plasma:", *extra, p], check=True)
        return p

    def leftovers(self):
        return [n for n in os.listdir(self.d) if n.startswith(".odysseus")]

    def test_resize_scale_and_fit(self):
        a = self.make("a.jpg")
        res = run(lambda j: images.process(j, [a], images.Options(resize="scale", scale=50, suffix=" (petite)")))
        self.assertEqual(res.created, [os.path.join(self.d, "a (petite).jpg")])
        self.assertEqual(size(res.created[0]), (200, 100, "JPEG"))
        self.assertEqual(size(a), (400, 200, "JPEG"))            # l'original ne bouge pas
        res = run(lambda j: images.process(j, [a], images.Options(resize="fit", width=100, height=100, suffix="-m")))
        self.assertEqual(size(res.created[0])[:2], (100, 50))
        # « ne pas agrandir »
        res = run(lambda j: images.process(j, [a], images.Options(resize="fit", width=4000, height=4000, suffix="-g")))
        self.assertEqual(size(res.created[0])[:2], (400, 200))
        self.assertEqual(self.leftovers(), [])

    def test_rotate_and_convert(self):
        p = self.make("logo.png", extra=("-alpha", "set", "-channel", "A", "-evaluate", "set", "50%"))
        res = run(lambda j: images.process(j, [p], images.Options(rotate=90, fmt="jpg")))
        self.assertEqual(res.created, [os.path.join(self.d, "logo.jpg")])
        self.assertEqual(size(res.created[0]), (200, 400, "JPEG"))
        res = run(lambda j: images.process(j, [p], images.Options(fmt="webp", quality=70)))
        self.assertEqual(size(res.created[0])[2], "WEBP")

    def test_never_overwrites_and_odd_names(self):
        p = self.make("photo [1].png")
        q = self.make("a:b.png", 50, 50)
        with open(os.path.join(self.d, "photo [1].jpg"), "w") as f:
            f.write("déjà là")
        res = run(lambda j: images.process(j, [p, q], images.Options(fmt="jpg")))
        self.assertEqual(sorted(os.path.basename(c) for c in res.created), ["a:b.jpg", "photo [1] (2).jpg"])
        with open(os.path.join(self.d, "photo [1].jpg")) as f:
            self.assertEqual(f.read(), "déjà là")

    def test_replace_same_format(self):
        p = self.make("r.png")
        os.chmod(p, 0o640)
        res = run(lambda j: images.process(j, [p], images.Options(resize="scale", scale=25, replace=True,
                                                                  suffix="ignoré")))
        self.assertEqual(res.created, [p])
        self.assertEqual(size(p)[:2], (100, 50))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(self.d)), ["r.png"])

    def test_replace_other_format_trashes_original(self):
        p = self.make("t.png")
        res = run(lambda j: images.process(j, [p], images.Options(fmt="jpg", replace=True)))
        self.assertEqual(res.created, [os.path.join(self.d, "t.jpg")])
        self.assertEqual(res.trashed, [p])
        self.assertEqual(os.listdir(self.d), ["t.jpg"])

    def test_broken_image_does_not_stop_others(self):
        bad = os.path.join(self.d, "abîmée.jpg")
        with open(bad, "wb") as f:
            f.write(b"\xff\xd8 pas une image")
        good = self.make("bonne.png", 20, 20)
        res = run(lambda j: images.process(j, [bad, good], images.Options(fmt="jpg", replace=True)))
        self.assertEqual([os.path.basename(e[0]) for e in res.errors], ["abîmée.jpg"])
        self.assertTrue(os.path.exists(bad))                     # jamais touchée
        self.assertEqual(sorted(os.listdir(self.d)), ["abîmée.jpg", "bonne.jpg"])
        j = Job("t", lambda j: images.process(j, [bad], images.Options(fmt="png"))).run_sync()
        self.assertEqual(j.state, "failed")
        self.assertEqual(self.leftovers(), [])

    def test_output_names(self):
        o = images.Options(suffix=" (redim)")
        self.assertEqual(images.output_name("/x/a.JPG", o), "a (redim).JPG")
        self.assertEqual(images.output_name("/x/a.heic", o), "a (redim).jpg")      # HEIC non écrit : JPEG
        self.assertEqual(images.output_name("/x/a.heic", images.Options(fmt="png", replace=True)), "a.png")


if __name__ == "__main__":
    unittest.main()
