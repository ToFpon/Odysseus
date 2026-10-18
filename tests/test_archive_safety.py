"""Archives piégées : rien ne doit être écrit, modifié ou supprimé hors du dossier d'extraction.

Archives fabriquées à la main (tarfile / zipfile) : chemins « ../ » et absolus, liens symboliques
vers l'extérieur suivis d'un fichier écrit « à travers », liens durs vers l'extérieur, fichiers
spéciaux, setuid, antislash. Extraites par le vrai moteur (archive.extract → 7-Zip), dans chaque
mode de sous-dossier. Plus des archives légitimes avec liens, qui doivent s'extraire entièrement.
"""
import gzip
import io
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import archive  # noqa: E402
from odysseus.backend.engine import Job  # noqa: E402
from odysseus.backend.tools import sevenzip_path  # noqa: E402


def snapshot(root):
    out = {}
    for r, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(r, n)
            s = os.lstat(p)
            out[p] = (stat.S_IFMT(s.st_mode), s.st_size, s.st_mtime_ns, s.st_ino,
                      os.readlink(p) if os.path.islink(p) else None)
    return out


def tar_bytes(members):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for m in members:
            ti = tarfile.TarInfo(m["name"])
            kind = m.get("kind", "file")
            if kind == "file":
                data = m.get("data", b"PWNED\n")
                ti.size, ti.mode = len(data), m.get("mode", 0o644)
                t.addfile(ti, io.BytesIO(data))
                continue
            ti.type = {"sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE, "dir": tarfile.DIRTYPE,
                       "fifo": tarfile.FIFOTYPE, "chr": tarfile.CHRTYPE}[kind]
            ti.linkname = m.get("target", "")
            if kind == "chr":
                ti.devmajor, ti.devminor = 1, 3
            if kind == "dir":
                ti.mode = 0o755
            t.addfile(ti)
    return b.getvalue()


def zip_bytes(members):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for m in members:
            zi = zipfile.ZipInfo(m["name"])
            zi.create_system = 3
            if m.get("kind") == "sym":
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, m["target"])
            else:
                zi.external_attr = (stat.S_IFREG | m.get("mode", 0o644)) << 16
                z.writestr(zi, m.get("data", b"PWNED\n"))
    return b.getvalue()


@unittest.skipUnless(sevenzip_path(), "7-Zip absent")
class TrappedArchiveTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="odysseus-piege-")
        self.outside = os.path.join(self.base, "precieux")
        os.makedirs(self.outside)
        self.victim = os.path.join(self.outside, "victime.txt")
        with open(self.victim, "w") as f:
            f.write("intact\n")
        self.work = os.path.join(self.base, "telechargements")
        os.makedirs(self.work)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def extract(self, name, data, mode):
        path = os.path.join(self.work, name)
        with open(path, "wb") as f:
            f.write(data)
        before = snapshot(self.base)
        job = Job("x", lambda j: archive.extract(j, path, archive.ExtractOptions(subfolder=mode))).run_sync()
        after = snapshot(self.base)
        outside = [p for p in set(before) | set(after)
                   if p != self.work and not p.startswith(self.work + "/") and before.get(p) != after.get(p)]
        self.assertEqual(outside, [], f"{name} ({mode}) a touché hors du dossier d'extraction")
        with open(self.victim) as f:
            self.assertEqual(f.read(), "intact\n")
        vino = os.stat(self.victim).st_ino
        for p, v in after.items():
            if p.startswith(self.work + "/"):
                self.assertNotEqual(v[3], vino, f"lien dur vers un fichier extérieur : {p}")
                self.assertNotIn(v[0], (stat.S_IFCHR, stat.S_IFBLK), f"périphérique créé : {p}")
                if v[0] == stat.S_IFREG:
                    self.assertFalse(os.lstat(p).st_mode & 0o6000, f"setuid conservé : {p}")
        self.assertFalse([n for n in os.listdir(self.work) if n.startswith(".odysseus-extract-")],
                         "dossier temporaire laissé")
        return job

    def trapped(self, members, zip_too=True):
        for mode in ("smart", "always", "never"):
            for name, data in [("piege.tar", tar_bytes(members)), ("piege.tar.gz", gzip.compress(tar_bytes(members)))] \
                    + ([("piege.zip", zip_bytes(members))] if zip_too else []):
                with self.subTest(archive=name, mode=mode):
                    self.extract(name, data, mode)
                    for n in os.listdir(self.work):     # repartir d'un dossier vide
                        p = os.path.join(self.work, n)
                        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)

    def test_dot_dot(self):
        self.trapped([{"name": "ok.txt"}, {"name": "../evil1.txt"}, {"name": "a/../../evil2.txt"},
                      {"name": "../../../../../../../../tmp/evil3.txt"}])

    def test_absolute_path(self):
        self.trapped([{"name": "ok.txt"}, {"name": self.outside + "/abs.txt"}])

    def test_backslash(self):
        self.trapped([{"name": "ok.txt"}, {"name": "..\\evil.txt"}])

    def test_write_through_absolute_link(self):
        self.trapped([{"name": "lnk", "kind": "sym", "target": self.outside}, {"name": "lnk/through.txt"}])

    def test_write_through_relative_link(self):
        self.trapped([{"name": "d", "kind": "dir"}, {"name": "d/lnk", "kind": "sym", "target": "../../precieux"},
                      {"name": "d/lnk/through.txt"}], zip_too=False)

    def test_overwrite_through_file_link(self):
        self.trapped([{"name": "f", "kind": "sym", "target": self.victim}, {"name": "f", "data": b"OVERWRITTEN\n"}])

    def test_hard_links_outside(self):
        self.trapped([{"name": "h", "kind": "hard", "target": self.victim}], zip_too=False)
        self.trapped([{"name": "h", "kind": "hard", "target": "../precieux/victime.txt"}], zip_too=False)
        self.trapped([{"name": "h", "kind": "hard", "target": self.victim},
                      {"name": "h", "data": b"OVERWRITTEN\n"}], zip_too=False)

    def test_special_files_and_setuid(self):
        self.trapped([{"name": "ok.txt"}, {"name": "fifo", "kind": "fifo"}, {"name": "null", "kind": "chr"},
                      {"name": "suid", "mode": 0o4755}], zip_too=False)


    def test_existing_link_in_destination_not_followed(self):
        """Extraction sans sous-dossier dans un dossier qui contient DÉJÀ un lien « x » vers l'extérieur :
        « x/f.txt » de l'archive ne doit pas atterrir dehors en passant par ce lien."""
        os.symlink(self.outside, os.path.join(self.work, "x"))
        for name, data in [("e.tar", tar_bytes([{"name": "x/f.txt"}, {"name": "autre.txt"}])),
                           ("e.zip", zip_bytes([{"name": "x/f.txt"}, {"name": "autre.txt"}]))]:
            with self.subTest(archive=name):
                self.extract(name, data, "never")
                self.assertEqual(os.readlink(os.path.join(self.work, "x")), self.outside)


@unittest.skipUnless(sevenzip_path(), "7-Zip absent")
class LegitArchiveWithLinksTest(unittest.TestCase):
    """Les liens d'une archive normale ne doivent ni faire échouer l'extraction, ni être abîmés."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="odysseus-liens-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_extract(self, name, data, mode="smart"):
        path = os.path.join(self.d, name)
        with open(path, "wb") as f:
            f.write(data)
        return Job("x", lambda j: archive.extract(j, path, archive.ExtractOptions(subfolder=mode))).run_sync()

    def test_tar_internal_and_absolute_links(self):
        members = [{"name": "pkg", "kind": "dir"}, {"name": "pkg/lib", "kind": "dir"},
                   {"name": "pkg/lib/libx.so.1", "data": b"lib"},
                   {"name": "pkg/lib/libx.so", "kind": "sym", "target": "libx.so.1"},
                   {"name": "pkg/zone", "kind": "sym", "target": "/usr/share/zoneinfo"}]
        for mode in ("smart", "never"):
            with self.subTest(mode=mode):
                j = self.run_extract(f"pkg-{mode}.tar.gz", gzip.compress(tar_bytes(members)), mode)
                self.assertEqual(j.state, "done", j.error)
                pkg = os.path.join(self.d, "pkg")
                self.assertEqual(os.readlink(os.path.join(pkg, "lib", "libx.so")), "libx.so.1")
                # lien absolu rendu tel quel (7-Zip l'avait réécrit vers notre dossier temporaire)
                self.assertEqual(os.readlink(os.path.join(pkg, "zone")), "/usr/share/zoneinfo")
                shutil.rmtree(pkg)

    def test_zip_with_one_refused_link_keeps_the_rest(self):
        members = [{"name": "pkg/a.txt", "data": b"a"}, {"name": "pkg/b.txt", "data": b"b"},
                   {"name": "pkg/zone", "kind": "sym", "target": "/usr/share/zoneinfo"}]
        j = self.run_extract("pkg.zip", zip_bytes(members))
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "pkg"))), ["a.txt", "b.txt"])
        self.assertEqual(getattr(j, "skipped_links", []), [("pkg/zone", "/usr/share/zoneinfo")])

    def test_real_error_still_fails(self):
        path = os.path.join(self.d, "casse.zip")
        good = zip_bytes([{"name": "a.txt", "data": b"x" * 5000}])
        with open(path, "wb") as f:
            f.write(good[:200] + b"\0" * 100 + good[300:])
        j = Job("x", lambda j: archive.extract(j, path, archive.ExtractOptions(subfolder="never"))).run_sync()
        self.assertEqual(j.state, "failed")


if __name__ == "__main__":
    unittest.main()
