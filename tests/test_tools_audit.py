"""Audit des outils externes (novembre 2026) : noms et dossiers piégés, résultats faux silencieux.
Chaque cas échouait sur l'ancien code ; sauté si l'outil n'est pas installé."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from odysseus.backend import debpkg, images, media, pdf, playlist, tools  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402


def run(func):
    j = Job("t", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state} : {j.error}")
    return j.result


def make_pdf(path, pages=3):
    import cairo
    s = cairo.PDFSurface(path, 200, 200)
    c = cairo.Context(s)
    for i in range(pages):
        c.move_to(20, 100)
        c.show_text(f"page {i + 1}")
        c.show_page()
    s.finish()


class ToolsAuditTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @unittest.skipUnless(shutil.which("gs") and shutil.which("qpdf"), "ghostscript / qpdf")
    def test_ghostscript_percent_in_folder(self):
        folder = os.path.join(self.d, "TVA 20 %d")
        os.makedirs(folder)
        src = os.path.join(folder, "doc.pdf")
        make_pdf(src)
        r = run(lambda j: pdf.compress(j, src, os.path.join(folder, "doc (c).pdf"), keep_if_bigger=False))
        self.assertEqual(pdf.page_count(r.dst), 3)
        self.assertEqual(sorted(os.listdir(folder)), ["doc (c).pdf", "doc.pdf"])

    @unittest.skipUnless(shutil.which("gs") and shutil.which("qpdf"), "ghostscript / qpdf")
    def test_compress_in_place_through_link_keeps_link(self):
        os.makedirs(os.path.join(self.d, "real"))
        real = os.path.join(self.d, "real", "d.pdf")
        make_pdf(real)
        link = os.path.join(self.d, "l.pdf")
        os.symlink(real, link)
        before = os.path.getsize(real)
        run(lambda j: pdf.compress(j, link, link, keep_if_bigger=False))
        self.assertTrue(os.path.islink(link))
        self.assertNotEqual(os.path.getsize(real), before)

    @unittest.skipUnless(images.tool(), "ImageMagick")
    def test_imagemagick_percent_names(self):
        base = os.path.join(self.d, "base.png")
        subprocess.run(["convert", "-size", "20x20", "xc:red", base], check=True)
        for folder, name in (("n", "50%off.png"), ("n", "p%d.png"), ("Taux 5%o", "a.png")):
            dd = os.path.join(self.d, folder)
            os.makedirs(dd, exist_ok=True)
            src = os.path.join(dd, name)
            shutil.copy(base, src)
            res = run(lambda j: images.process(j, [src], images.Options(resize="scale", scale=50, suffix="-s")))
            self.assertEqual(res.errors, [], name)
            self.assertTrue(os.path.getsize(res.created[0]) > 0)
            self.assertFalse([n for n in os.listdir(dd) if n.startswith(".odysseus")])

    def test_checksum_files(self):
        data = os.path.join(self.d, "iso.img")
        with open(data, "wb") as fh:
            fh.write(b"x" * 1000)
        import hashlib
        b2 = hashlib.blake2b(b"x" * 1000).hexdigest()
        s256 = hashlib.sha256(b"x" * 1000).hexdigest()
        for name, body in (("B2SUMS", f"{b2}  iso.img\n"), ("b2sums.txt", f"{b2}  iso.img\n"),
                           ("y.md5", f"{s256}  iso.img\n")):          # SHA-256 dans un « .md5 »
            p = os.path.join(self.d, name)
            with open(p, "w") as fh:
                fh.write(body)
            self.assertEqual([r["status"] for r in tools.verify_hash_file(Job("t", None), p)], ["ok"], name)
        # nom échappé (« \ » de sha256sum) et nom qui finit par une espace
        for n in ("back\\slash.txt", "trail "):
            with open(os.path.join(self.d, n), "wb") as fh:
                fh.write(b"y")
        h = hashlib.sha256(b"y").hexdigest()
        p = os.path.join(self.d, "e.sha256")
        with open(p, "w") as fh:
            fh.write(f"\\{h}  back\\\\slash.txt\n{h}  trail \n")
        self.assertEqual([r["status"] for r in tools.verify_hash_file(Job("t", None), p)], ["ok", "ok"])

    def test_playlist_hash_and_space_names(self):
        tracks = [os.path.join(self.d, n) for n in ("#1 Hit.mp3", " lead.mp3")]
        for t in tracks:
            open(t, "w").close()
        out = playlist.create(Job("t", None), tracks, os.path.join(self.d, "l.m3u"))
        self.assertEqual(playlist.append(Job("t", None), out, tracks), (0, 2))     # déjà là, pas de doublon

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg")
    def test_extract_audio_non_utf8(self):
        d = os.fsencode(self.d)
        src = os.path.join(d, b"caf\xe9.mp4")
        subprocess.run([b"ffmpeg", b"-v", b"quiet", b"-f", b"lavfi", b"-i", b"sine=d=1", b"-metadata",
                        b"title=Ol\xe9", src], check=True)
        res = run(lambda j: media.extract_audio(j, [os.fsdecode(src)], "mp3", "medium"))
        self.assertTrue(res[0][1])
        self.assertTrue(os.path.getsize(res[0][0]) > 0)
        self.assertFalse([n for n in os.listdir(d) if n.startswith(b".odysseus")])

    @unittest.skipUnless(shutil.which("apt-get") and shutil.which("dpkg-deb"), "apt / dpkg")
    def test_deb_colon_path(self):
        pkg = os.path.join(self.d, "pkg")
        os.makedirs(os.path.join(pkg, "DEBIAN"))
        with open(os.path.join(pkg, "DEBIAN", "control"), "w") as fh:
            fh.write("Package: odysseus-audit-test\nVersion: 1.0\nArchitecture: all\nMaintainer: t <t@t>\n"
                     "Description: test\n")
        folder = os.path.join(self.d, "smb-share:server=nas,share=d")
        os.makedirs(folder)
        deb = os.path.join(folder, "x:y.deb")
        subprocess.run(["dpkg-deb", "-b", pkg, deb], check=True, capture_output=True)
        # chemin direct : apt ne prend pas le paquet → plus de faux « aucune dépendance »
        self.assertIsNone(debpkg.missing_dependencies(deb))
        copy = debpkg.safe_copy(deb)
        try:
            self.assertEqual(debpkg.missing_dependencies(copy), [])
            self.assertNotIn(":", copy)
        finally:
            debpkg.discard_copy(copy)
        self.assertFalse(os.path.exists(os.path.dirname(copy)))


if __name__ == "__main__":
    unittest.main()
