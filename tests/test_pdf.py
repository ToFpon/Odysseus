"""Tests des outils PDF (nécessitent qpdf, Ghostscript et python3-cairo)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import pdf  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402

try:
    import cairo
except ImportError:  # pragma: no cover
    cairo = None


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}\n{j.log}")
    return j.result


@unittest.skipUnless(cairo and shutil.which("qpdf") and shutil.which("gs"), "qpdf, gs et cairo requis")
class PdfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.a = self.make("a.pdf", 3)
        self.b = self.make("b.pdf", 4, landscape=True)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, n, landscape=False):
        p = os.path.join(self.d, name)
        s = cairo.PDFSurface(p, 842 if landscape else 595, 595 if landscape else 842)
        c = cairo.Context(s)
        for i in range(n):
            c.move_to(50, 100)
            c.show_text(f"{name} {i + 1}")
            c.show_page()
        s.finish()
        return p

    def test_ranges(self):
        self.assertEqual(pdf.parse_ranges("", 3), [0, 1, 2])
        self.assertEqual(pdf.parse_ranges("1-3, 5, 7-", 8), [0, 1, 2, 4, 6, 7])
        self.assertEqual(pdf.parse_ranges("3-1", 3), [2, 1, 0])
        self.assertEqual(pdf.parse_ranges("z", 4), [3])
        with self.assertRaises(ValueError):
            pdf.parse_ranges("9", 3)
        self.assertEqual(pdf.to_qpdf_ranges([0, 1, 2, 4, 6, 7]), "1-3,5,7-8")

    def test_merge_pages_rotate_split(self):
        out = os.path.join(self.d, "m.pdf")
        run(lambda j: pdf.merge(j, [(self.a, ""), (self.b, "2-3")], out))
        self.assertEqual(pdf.page_count(out), 5)
        run(lambda j: pdf.delete_pages(j, out, "1", os.path.join(self.d, "d.pdf")))
        self.assertEqual(pdf.page_count(os.path.join(self.d, "d.pdf")), 4)
        run(lambda j: pdf.extract_pages(j, out, "2,4", os.path.join(self.d, "e.pdf")))
        self.assertEqual(pdf.page_count(os.path.join(self.d, "e.pdf")), 2)
        run(lambda j: pdf.rotate_pages(j, self.a, "2", 90, os.path.join(self.d, "r.pdf")))
        files = run(lambda j: pdf.split_pages(j, self.b, self.d, 3))
        self.assertEqual(len(files), 2)

    def test_output_permissions(self):
        """Fichiers produits avec les droits d'un fichier ordinaire (mkstemp crée en 0600), et un
        original modifié sur place garde les siens."""
        import stat
        from odysseus.backend.engine import UMASK
        out = os.path.join(self.d, "m.pdf")
        run(lambda j: pdf.merge(j, [(self.a, ""), (self.b, "")], out))
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o666 & ~UMASK)
        os.chmod(self.a, 0o640)
        run(lambda j: pdf.rotate_pages(j, self.a, "1", 90, self.a))
        self.assertEqual(stat.S_IMODE(os.stat(self.a).st_mode), 0o640)

    def test_watermark_and_compress(self):
        for opts in (pdf.WatermarkOptions(text="CONFIDENTIEL", position="tiled", pages="1"),
                     pdf.WatermarkOptions(text="Été €", flatten=True, dpi=72)):
            out = os.path.join(self.d, "w.pdf")
            run(lambda j: pdf.watermark(j, self.b, out, opts))
            self.assertEqual(pdf.page_count(out), 4)
        res = run(lambda j: pdf.compress(j, self.a, os.path.join(self.d, "c.pdf"), "/screen"))
        self.assertTrue(os.path.exists(res.dst))


if __name__ == "__main__":
    unittest.main()
