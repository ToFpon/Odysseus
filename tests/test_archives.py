import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odysseus.backend import archive, formats, tools  # noqa: E402
from odysseus.backend.engine import Job, PasswordRequired  # noqa: E402


def run(func):
    j = Job("test", func).run_sync()
    if j.state != "done":
        raise AssertionError(f"{j.state}: {j.error}\n{j.log}")
    return j.result


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "projet")
        os.makedirs(os.path.join(self.src, "sous dossier"))
        with open(os.path.join(self.src, "lisez-moi.txt"), "w") as f:
            f.write("Bonjour é à ü\n" * 100)
        with open(os.path.join(self.src, "sous dossier", "data.bin"), "wb") as f:
            f.write(os.urandom(50000))

    def tearDown(self):
        self.tmp.cleanup()

    def roundtrip(self, fmt, **kw):
        o = archive.CreateOptions(fmt=fmt, **kw)
        out = archive.default_output([self.src], fmt)
        run(lambda j: archive.create(j, [self.src], out, o))
        self.assertTrue(os.path.exists(out), out)
        listing = archive.list_archive(out, kw.get("password"))
        names = {e.path for e in listing.entries}
        self.assertIn("projet/sous dossier/data.bin", names)
        run(lambda j: archive.test(j, out, kw.get("password", "")))
        dest = os.path.join(self.root, "out-" + fmt)
        run(lambda j: archive.extract(j, out, archive.ExtractOptions(dest=dest, password=kw.get("password", ""))))
        with open(os.path.join(dest, "projet", "lisez-moi.txt")) as f:
            self.assertIn("Bonjour é", f.read())
        return out

    def test_formats(self):
        for fmt in ("7z", "zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tar.br", "tar.lz4", "wim"):
            with self.subTest(fmt=fmt):
                self.roundtrip(fmt)

    def test_encrypted(self):
        out = self.roundtrip("7z", password="s3cret", encrypt_names=True)
        with self.assertRaises(PasswordRequired):
            archive.list_archive(out)
        self.assertTrue(archive.needs_password(out))
        j = Job("x", lambda j: archive.extract(j, out, archive.ExtractOptions(dest=self.root + "/bad", password="nope"))).run_sync()
        self.assertEqual(j.state, "failed")
        self.roundtrip("zip", password="s3cret")

    def test_stream_single(self):
        f = os.path.join(self.src, "lisez-moi.txt")
        for fmt in ("gz", "zst", "br", "lz4", "xz", "bz2"):
            with self.subTest(fmt=fmt):
                out = f"{f}.{fmt}"
                run(lambda j: archive.create(j, [f], out, archive.CreateOptions(fmt=fmt, level=3)))
                lst = archive.list_archive(out)
                self.assertEqual(len(lst.top_level()), 1)
                dest = os.path.join(self.root, "s-" + fmt)
                run(lambda j: archive.extract(j, out, archive.ExtractOptions(dest=dest)))
                self.assertTrue(os.path.exists(os.path.join(dest, "lisez-moi.txt")), os.listdir(dest))

    def test_modify(self):
        out = self.roundtrip("7z")
        extra = os.path.join(self.root, "ajout.txt")
        open(extra, "w").write("x")
        run(lambda j: archive.add_to_archive(j, out, [extra], inner_dir="projet/sous dossier"))
        self.assertIsNotNone(archive.list_archive(out).get("projet/sous dossier/ajout.txt"))
        run(lambda j: archive.rename_in_archive(j, out, "projet/lisez-moi.txt", "projet/README.txt"))
        run(lambda j: archive.delete_from_archive(j, out, ["projet/sous dossier"]))
        names = {e.path for e in archive.list_archive(out).entries}
        self.assertEqual(names, {"projet", "projet/README.txt"})

    def test_convert_and_volumes(self):
        out = self.roundtrip("zip")
        conv = os.path.join(self.root, "conv.tar.zst")
        run(lambda j: archive.convert(j, out, conv, archive.CreateOptions(fmt="tar.zst")))
        self.assertIsNotNone(archive.list_archive(conv).get("projet/lisez-moi.txt"))
        vol = os.path.join(self.root, "vol.7z")
        run(lambda j: archive.create(j, [self.src], vol, archive.CreateOptions(fmt="7z", level=0, volume_size="20k")))
        self.assertTrue(os.path.exists(vol + ".002"))
        run(lambda j: archive.extract(j, vol + ".001", archive.ExtractOptions(dest=self.root + "/v")))
        self.assertTrue(os.path.exists(self.root + "/v/projet/sous dossier/data.bin"))

    def test_existing_tar_archive_not_overwritten(self):
        """TAR.GZ & co. : une archive du même nom (une sauvegarde…) ne doit jamais être écrasée."""
        out = os.path.join(self.root, "projet.tar.gz")
        with open(out, "wb") as f:
            f.write(b"ANCIENNE SAUVEGARDE")
        made = run(lambda j: archive.create(j, [self.src], out, archive.CreateOptions(fmt="tar.gz")))
        self.assertEqual(open(out, "rb").read(), b"ANCIENNE SAUVEGARDE")
        self.assertEqual(os.path.basename(made), "projet (2).tar.gz")
        self.assertIn("projet/lisez-moi.txt", {e.path for e in archive.list_archive(made).entries})

    def test_smart_folder_permissions(self):
        """Extraction intelligente à plusieurs éléments : dossier en droits ordinaires (pas 0700)."""
        import stat
        from odysseus.backend.engine import UMASK
        arc = os.path.join(self.root, "deux.zip")
        run(lambda j: archive.create(j, [os.path.join(self.src, "lisez-moi.txt"),
                                         os.path.join(self.src, "sous dossier")], arc,
                                     archive.CreateOptions(fmt="zip")))
        out = os.path.join(self.root, "sortie")
        os.makedirs(out)
        dest = run(lambda j: archive.extract(j, arc, archive.ExtractOptions(dest=out, subfolder="smart")))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o777 & ~UMASK)

    def test_unreadable_file_reported(self):
        """Un fichier illisible est sauté : l'archive est gardée et le fichier signalé, en 7z comme en
        tar.gz (avant : succès silencieux en 7z / zip, échec complet en tar.gz)."""
        import subprocess as sp
        if not shutil.which("setpriv") or os.getuid() != 0:
            self.skipTest("setpriv / root requis pour rendre un fichier illisible")
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from odysseus.backend import archive\nfrom odysseus.backend.engine import Job\n"
                "j = Job('x', lambda j: None)\n"
                "out = archive.create(j, [%r], %r, archive.CreateOptions(fmt=%r))\n"
                "print([p.rsplit('/', 1)[-1] for p, _ in j.skipped_files])")
        secret = os.path.join(self.src, "secret.txt")
        with open(secret, "w") as f:
            f.write("x")
        os.chmod(secret, 0)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fmt in ("7z", "tar.gz"):
            out = os.path.join(self.root, "illisible." + fmt)
            r = sp.run(["setpriv", "--bounding-set", "-dac_override,-dac_read_search,-fowner", sys.executable,
                        "-c", code % (root, self.src, out, fmt)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            self.assertEqual(r.stdout.strip(), "['secret.txt']", fmt)
            self.assertTrue(os.path.exists(out), fmt)

    def test_smart_destination(self):
        out = self.roundtrip("7z")
        o = archive.ExtractOptions(dest=self.root, subfolder="smart")
        self.assertEqual(archive.resolve_destination(out, o), self.root)

    def test_tools(self):
        res = run(lambda j: tools.hash_files(j, [self.src], ["CRC32", "SHA-256", "CRC64"]))
        self.assertEqual(len(res), 2)
        hf = os.path.join(self.root, "projet.sha256")
        open(hf, "w").write(tools.format_hash_file(res, "SHA-256", self.root))
        checked = run(lambda j: tools.verify_hash_file(j, hf))
        self.assertTrue(all(c["status"] == "ok" for c in checked), checked)
        self.assertEqual(tools.hash_text("123456789", "CRC64"), "995dc9bbdf1939fa")
        big = os.path.join(self.src, "sous dossier", "data.bin")
        parts = run(lambda j: tools.split_file(j, big, 7000))
        self.assertEqual(len(parts), 8)
        os.rename(big, big + ".orig")
        joined = run(lambda j: tools.join_files(j, parts[0]))
        self.assertEqual(open(joined, "rb").read(), open(big + ".orig", "rb").read())
        dups = run(lambda j: tools.find_duplicates(j, [self.src]))
        self.assertEqual(len(dups), 1)
        cmp = run(lambda j: tools.compare_files(j, joined, big + ".orig"))
        self.assertTrue(cmp.identical)
        victim = os.path.join(self.root, "victim")
        os.makedirs(victim + "/a")
        open(victim + "/a/x", "w").write("secret")
        run(lambda j: tools.secure_delete(j, [victim], passes=2))
        self.assertFalse(os.path.exists(victim))
        pw = tools.generate_password(20)
        self.assertEqual(len(pw), 20)
        self.assertEqual(tools.parse_size("4,7g"), int(4.7 * 1024 ** 3))

    def test_mounted_disks(self):
        from odysseus.backend import devices
        fake = os.path.join(self.root, "mounts")
        with open(fake, "w") as fh:
            fh.write("""/dev/sda2 / ext4 rw 0 0
/dev/sda1 /boot/efi vfat rw 0 0
/dev/sda3 /home ext4 rw 0 0
/dev/sdb1 /mnt/Donn\\040es ext4 rw 0 0
/dev/sdb1 /srv/bind ext4 rw 0 0
/dev/sdc1 /run/media/tof/CLE_USB vfat rw 0 0
/dev/loop3 /snap/core/1 squashfs ro 0 0
tmpfs /tmp tmpfs rw 0 0
/dev/nvme0n1p4 /data btrfs rw 0 0
""")
        disks = devices.mounted_disks(fake, home="/home/tof")
        self.assertEqual([d.path for d in disks], ["/mnt/Donn es", "/run/media/tof/CLE_USB", "/data"])
        self.assertEqual(disks[0].name, "Donn es")
        fstab = os.path.join(self.root, "fstab")
        with open(fstab, "w") as fh:
            fh.write("# commentaire\nUUID=abcd / ext4 defaults 0 1\n/dev/sdb1 /mnt/Donn\\040es ext4 defaults 0 2\n"
                     "LABEL=Data /data btrfs defaults 0 0\n")
        points, devs = devices.fstab_entries(fstab)
        self.assertEqual(points, {"/", "/mnt/Donn es", "/data"})
        self.assertIn(os.path.realpath("/dev/sdb1"), devs)

    def test_detect(self):
        self.assertEqual(formats.split_archive_name("a.b.tar.gz"), ("a.b", "tar.gz"))
        self.assertEqual(formats.detect("x.tgz").kind, "tar-stream")
        self.assertEqual(formats.detect("x.7z.001").kind, "7z")
        self.assertEqual(formats.detect("x.txt").kind, "none")


if __name__ == "__main__":
    unittest.main()


class SmartExtractTest(unittest.TestCase):
    """« Extraire ici » (dossier intelligent) : une seule passe, rangement, noms déjà pris."""

    def setUp(self):
        import shutil
        import subprocess
        self.d = tempfile.mkdtemp()
        if not shutil.which("bzip2") or not shutil.which("tar"):
            self.skipTest("bzip2 / tar absents")
        with open(os.path.join(self.d, "solo.txt"), "w") as fh:
            fh.write("contenu\n")
        subprocess.run(["bzip2", "-k", os.path.join(self.d, "solo.txt")], check=True)
        os.makedirs(os.path.join(self.d, "racine"))
        with open(os.path.join(self.d, "racine", "a.txt"), "w") as fh:
            fh.write("a\n")
        subprocess.run(["tar", "-cjf", os.path.join(self.d, "un.tar.bz2"), "-C", self.d, "racine"], check=True)

    def _extract(self, name):
        o = archive.ExtractOptions(subfolder="smart", overwrite="rename")
        j = Job("x", lambda j: archive.extract(j, os.path.join(self.d, name), o)).run_sync()
        self.assertEqual(j.state, "done", j.error)
        return j.result

    def test_single_file_next_to_existing(self):
        self._extract("solo.txt.bz2")      # solo.txt existe déjà : pas de dossier « solo.txt »
        self.assertTrue(os.path.isfile(os.path.join(self.d, "solo (2).txt")))

    def test_tar_single_root_twice(self):
        self.assertEqual(self._extract("un.tar.bz2"), os.path.join(self.d, "racine (2)"))
        self.assertEqual(self._extract("un.tar.bz2"), os.path.join(self.d, "racine (3)"))
        self.assertFalse([n for n in os.listdir(self.d) if n.startswith(".odysseus-extract")])


class GioOpsTest(unittest.TestCase):
    """Opérations par GIO (emplacements sans chemin local), vérifiées ici sur des fichiers locaux."""

    def test_copy_rename_delete(self):
        from odysseus.backend import gioops
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "src", "sous"))
        with open(os.path.join(d, "src", "sous", "f.txt"), "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(d, "dst"))
        res = Job("c", lambda j: gioops.transfer(j, [os.path.join(d, "src")], os.path.join(d, "dst"))).run_sync().result
        self.assertFalse(res.errors)
        self.assertTrue(os.path.isfile(os.path.join(d, "dst", "src", "sous", "f.txt")))
        gioops.transfer(Job("c", lambda j: None), [os.path.join(d, "src")], os.path.join(d, "dst"))
        self.assertEqual(len(os.listdir(os.path.join(d, "dst"))), 2)        # « src (copie) »
        new = gioops.rename(os.path.join(d, "dst", "src"), "renomme")
        self.assertTrue(new.endswith("renomme"))
        gioops.make_folder(os.path.join(d, "dst"), "vide")
        gioops.delete(Job("d", lambda j: None), [new, os.path.join(d, "dst", "vide")])
        self.assertEqual(len(os.listdir(os.path.join(d, "dst"))), 1)
