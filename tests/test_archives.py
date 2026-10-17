import os
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
