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

    def test_existing_zip_and_7z_not_updated(self):
        """ZIP / 7Z : par défaut, une archive du même nom n'est pas modifiée (« a » de 7-Zip la mettait
        à jour en silence) : « projet (2).zip ». La mise à jour reste possible si on la demande."""
        for fmt in ("zip", "7z"):
            with self.subTest(fmt=fmt):
                out = os.path.join(self.root, "projet." + fmt)
                first = run(lambda j: archive.create(j, [self.src], out, archive.CreateOptions(fmt=fmt)))
                self.assertEqual(first, out)
                before = open(out, "rb").read()
                made = run(lambda j: archive.create(j, [self.src], out, archive.CreateOptions(fmt=fmt)))
                self.assertEqual(os.path.basename(made), f"projet (2).{fmt}")
                self.assertEqual(open(out, "rb").read(), before)
                same = run(lambda j: archive.create(j, [self.src], out,
                                                    archive.CreateOptions(fmt=fmt, update_mode="add")))
                self.assertEqual(same, out)

    def test_volumes_never_overwrite_previous_set(self):
        for fmt in ("tar.gz", "7z"):
            with self.subTest(fmt=fmt):
                out = os.path.join(self.root, "vol." + fmt)
                o = archive.CreateOptions(fmt=fmt, level=0, volume_size="20k")
                run(lambda j: archive.create(j, [self.src], out, o))
                first = {n: open(os.path.join(self.root, n), "rb").read()
                         for n in os.listdir(self.root) if n.startswith("vol." + fmt + ".")}
                self.assertIn(f"vol.{fmt}.001", first)
                made = run(lambda j: archive.create(j, [self.src], out, o))
                self.assertEqual(os.path.basename(made), f"vol (2).{fmt}")
                for n, data in first.items():
                    self.assertEqual(open(os.path.join(self.root, n), "rb").read(), data, n)

    def test_part_file_left_alone(self):
        """« X.part » d'un téléchargement en cours ne sert jamais de fichier temporaire."""
        out = os.path.join(self.root, "projet.tar.gz")
        with open(out + ".part", "w") as f:
            f.write("téléchargement")
        run(lambda j: archive.create(j, [self.src], out, archive.CreateOptions(fmt="tar.gz")))
        self.assertEqual(open(out + ".part").read(), "téléchargement")

    def test_extract_skip_keeps_archive_to_delete(self):
        """« Ignorer » + « Supprimer l'archive » : rien n'est extrait, l'archive est la seule copie,
        elle reste."""
        arc = os.path.join(self.root, "projet.zip")
        run(lambda j: archive.create(j, [self.src], arc, archive.CreateOptions(fmt="zip")))
        for mode in ("smart", "never"):
            with self.subTest(mode=mode):
                j = Job("x", lambda j: archive.extract(j, arc, archive.ExtractOptions(
                    subfolder=mode, overwrite="skip", delete_after=True))).run_sync()
                self.assertEqual(j.state, "done", j.error)
                self.assertTrue(os.path.exists(arc), "archive supprimée alors que rien n'a été extrait")
                self.assertTrue(getattr(j, "archive_kept", False))
        out = os.path.join(self.root, "ailleurs")
        run(lambda j: archive.extract(j, arc, archive.ExtractOptions(dest=out, delete_after=True)))
        self.assertFalse(os.path.exists(arc), "tout extrait : l'archive est supprimée comme demandé")

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

    def test_delete_after_keeps_unreadable_files(self):
        """« Supprimer les fichiers après compression » : un fichier illisible, absent de l'archive,
        reste sur le disque (en tar.* il était supprimé)."""
        import subprocess as sp
        if not shutil.which("setpriv") or os.getuid() != 0:
            self.skipTest("setpriv / root requis pour rendre un fichier illisible")
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from odysseus.backend import archive\nfrom odysseus.backend.engine import Job\n"
                "j = Job('x', lambda j: None)\n"
                "archive.create(j, [%r], %r, archive.CreateOptions(fmt=%r, delete_after=True))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fmt in ("tar.gz", "zip", "7z"):
            with self.subTest(fmt=fmt):
                src = os.path.join(self.root, "a-" + fmt.replace(".", ""))
                os.makedirs(os.path.join(src, "sous"))
                for n in ("lu.txt", "sous/lu2.txt", "sous/secret.txt"):
                    with open(os.path.join(src, n), "w") as f:
                        f.write("x")
                os.chmod(os.path.join(src, "sous", "secret.txt"), 0)
                out = src + "." + fmt
                r = sp.run(["setpriv", "--bounding-set", "-dac_override,-dac_read_search,-fowner", sys.executable,
                            "-c", code % (root, src, out, fmt)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr[-500:])
                self.assertTrue(os.path.exists(os.path.join(src, "sous", "secret.txt")), "fichier illisible supprimé")
                self.assertFalse(os.path.exists(os.path.join(src, "lu.txt")), "fichier archivé non supprimé")

    def test_extract_into_existing_folder_and_selected_items(self):
        """« Extraire dans un dossier » : un dossier neuf, jamais celui du même nom qui existe ; éléments
        choisis : posés sans leur chemin dans l'archive (pas dans le dossier source à côté)."""
        arc = os.path.join(self.root, "projet.zip")
        run(lambda j: archive.create(j, [self.src], arc, archive.CreateOptions(fmt="zip")))
        with open(os.path.join(self.src, "lisez-moi.txt"), "w") as f:
            f.write("MODIFIÉ DEPUIS")
        for over in ("rename", "overwrite"):
            dest = run(lambda j: archive.extract(j, arc, archive.ExtractOptions(subfolder="always", overwrite=over)))
            self.assertNotEqual(dest, self.src)
        self.assertEqual(open(os.path.join(self.src, "lisez-moi.txt")).read(), "MODIFIÉ DEPUIS")
        before = sorted(os.listdir(self.src))
        out = os.path.join(self.root, "choix")
        os.makedirs(out)
        run(lambda j: archive.extract(j, arc, archive.ExtractOptions(dest=out, subfolder="never",
                                                                      files=["projet/lisez-moi.txt"])))
        self.assertEqual(os.listdir(out), ["lisez-moi.txt"])
        self.assertEqual(sorted(os.listdir(self.src)), before)
        tmp = run(lambda j: archive.extract_to_temp(j, arc, ["projet/sous dossier"]))
        self.assertEqual(os.listdir(tmp), ["sous dossier"])

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


class AuditArchiveTest(unittest.TestCase):
    """Trouvailles de l'audit (novembre 2026) : « supprimer après » ne supprime que ce qui est sûr
    d'être dans l'archive, l'archive elle-même n'est jamais remplacée, les noms sont exacts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.src = os.path.join(self.root, "proj")
        os.makedirs(self.src)
        for n, t in (("garde.txt", "a"), ("important.log", "b")):
            with open(os.path.join(self.src, n), "w") as f:
                f.write(t)

    def tearDown(self):
        self.tmp.cleanup()

    def test_excludes_keep_sources(self):
        for fmt in ("7z", "tar.gz"):
            with self.subTest(fmt=fmt):
                out = os.path.join(self.root, "x." + fmt)
                j = Job("c", lambda j: archive.create(j, [self.src], out, archive.CreateOptions(
                    fmt=fmt, excludes=["*.log"], delete_after=True))).run_sync()
                self.assertEqual(j.state, "done", j.error)
                self.assertTrue(os.path.exists(os.path.join(self.src, "important.log")),
                                "fichier exclu supprimé alors qu'il n'est pas dans l'archive")
                self.assertTrue(getattr(j, "sources_kept_reason", ""))

    def test_fresh_keeps_sources(self):
        out = os.path.join(self.root, "bk.7z")
        run(lambda j: archive.create(j, [os.path.join(self.src, "garde.txt")], out,
                                     archive.CreateOptions(fmt="7z")))
        both = [os.path.join(self.src, "garde.txt"), os.path.join(self.src, "important.log")]
        run(lambda j: archive.create(j, both, out, archive.CreateOptions(
            fmt="7z", update_mode="fresh", delete_after=True)))
        self.assertTrue(all(os.path.exists(p) for p in both))

    def test_output_inside_source(self):
        out = os.path.join(self.src, "proj.7z")
        j = Job("c", lambda j: archive.create(j, [self.src], out, archive.CreateOptions(
            fmt="7z", delete_after=True))).run_sync()
        self.assertEqual(j.state, "done", j.error)
        self.assertTrue(os.path.exists(j.result) and os.path.exists(os.path.join(self.src, "garde.txt")))

    def test_archive_containing_itself(self):
        """« data.zip » qui contient « data.zip » : l'archive n'est jamais écrasée, et « supprimer
        après » ne retire qu'elle, jamais la copie extraite."""
        inner_dir = os.path.join(self.root, "inner")
        os.makedirs(inner_dir)
        with open(os.path.join(inner_dir, "data.zip"), "w") as f:
            f.write("contenu intérieur")
        arc = os.path.join(self.root, "data.zip")
        run(lambda j: archive.create(j, [os.path.join(inner_dir, "data.zip")], arc + ".tmp.zip",
                                     archive.CreateOptions(fmt="zip")))
        for mode in ("overwrite", "rename_existing"):
            with self.subTest(mode=mode):
                shutil.copy(arc + ".tmp.zip", arc)
                j = Job("x", lambda j: archive.extract(j, arc, archive.ExtractOptions(
                    subfolder="smart", overwrite=mode, delete_after=True))).run_sync()
                self.assertEqual(j.state, "done", j.error)
                left = [n for n in os.listdir(self.root) if n.startswith("data")]
                contents = [open(os.path.join(self.root, n)).read() for n in left if n != "data.zip.tmp.zip"]
                self.assertIn("contenu intérieur", contents, f"copie extraite perdue ({left})")
                for n in left:
                    if n != "data.zip.tmp.zip":
                        os.remove(os.path.join(self.root, n))

    def test_names_are_not_wildcards(self):
        os.makedirs(os.path.join(self.root, "w"))
        for n in ("*.txt", "abc.txt"):
            with open(os.path.join(self.root, "w", n), "w") as f:
                f.write(n)
        arc = os.path.join(self.root, "w.7z")
        run(lambda j: archive.create(j, [os.path.join(self.root, "w", "*.txt"),
                                         os.path.join(self.root, "w", "abc.txt")], arc,
                                     archive.CreateOptions(fmt="7z")))
        run(lambda j: archive.delete_from_archive(j, arc, ["*.txt"]))
        names = {e.path for e in archive.list_archive(arc).entries}
        self.assertEqual(names, {"abc.txt"})


class DotTarDeleteTest(unittest.TestCase):
    """tar fait avec « . » (tar -cf x.tar .) : 7-Zip « supprimait » sans rien supprimer et répondait OK.
    Suppression par GNU tar (noms exacts, sans jokers), et filet : un élément resté = une erreur."""

    def setUp(self):
        if not shutil.which("tar"):
            self.skipTest("GNU tar indisponible")
        self.root = tempfile.mkdtemp(prefix="odysseus-dottar-")
        self.addCleanup(shutil.rmtree, self.root, True)
        src = os.path.join(self.root, "src")
        os.makedirs(os.path.join(src, "sous"))
        for name, text in (("a.txt", "a"), ("b.txt", "b"), ("*.txt", "joker"), ("sous/c.txt", "c")):
            with open(os.path.join(src, name), "w") as f:
                f.write(text)
        self.tar = os.path.join(self.root, "x.tar")
        import subprocess
        subprocess.run(["tar", "-cf", self.tar, "."], cwd=src, check=True)

    def names(self):
        return {e.path for e in archive.list_archive(self.tar).entries}

    def test_delete_files_and_folder(self):
        self.assertIn("./a.txt", self.names())
        run(lambda j: archive.delete_from_archive(j, self.tar, ["./a.txt", "./sous", "./*.txt"]))
        left = self.names()
        self.assertNotIn("./a.txt", left)
        self.assertFalse(any(n.startswith("./sous") for n in left), left)
        self.assertNotIn("./*.txt", left)
        self.assertIn("./b.txt", left)              # « *.txt » est un nom, pas un joker
        self.assertEqual(sorted(os.listdir(self.root)), ["src", "x.tar"])     # aucune copie laissée

    def test_silent_noop_is_reported(self):
        """Filet : 7-Zip sans la correction (sa réponse « OK » sans rien supprimer) → erreur, pas un succès."""
        real = archive._dot_tar
        archive._dot_tar = lambda *_a: False
        try:
            j = Job("test", lambda j: archive.delete_from_archive(j, self.tar, ["./a.txt"])).run_sync()
        finally:
            archive._dot_tar = real
        self.assertNotEqual(j.state, "done")
        self.assertIn("a.txt", str(j.error))
        self.assertIn("./a.txt", self.names())      # rien n'a bougé


class AuditListingTest(unittest.TestCase):
    """Audit : les noms de la liste sont ceux de l'archive (espaces, octets non UTF-8, « ./ »)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _roundtrip(self, arc, src_dir, names):
        listing = archive.list_archive(arc)
        paths = {e.name: e.path for e in listing.entries}
        for n in names:
            with self.subTest(name=n):
                self.assertIn(n, paths, sorted(paths))
                out = tempfile.mkdtemp(dir=self.root)
                run(lambda j: archive.extract(j, arc, archive.ExtractOptions(
                    dest=out, subfolder="never", files=[paths[n]])))
                self.assertEqual(os.listdir(out), [n])

    def test_spaces_and_bytes(self):
        src = os.path.join(self.root, "src")
        os.makedirs(src)
        names = ["  lead.txt", "trail.txt ", os.fsdecode(b"caf\xe9.txt")]
        for n in names:
            with open(os.path.join(src, n), "w") as f:
                f.write("x")
        tar = os.path.join(self.root, "n.tar")
        import subprocess
        subprocess.run(["tar", "-cf", tar, "-C", src] + names, check=True)
        self._roundtrip(tar, src, names)
        arc7 = os.path.join(self.root, "n.7z")
        run(lambda j: archive.create(j, [os.path.join(src, n) for n in names[:2]], arc7,
                                     archive.CreateOptions(fmt="7z")))
        self._roundtrip(arc7, src, names[:2])

    def test_dot_prefix_tar(self):
        """« tar -cf x.tar . » : pas de faux dossier « . », ses éléments s'extraient."""
        src = os.path.join(self.root, "src")
        os.makedirs(os.path.join(src, "sub"))
        for n in ("abc.txt", "sub/in.txt"):
            with open(os.path.join(src, n), "w") as f:
                f.write("x")
        tar = os.path.join(self.root, "dot.tar")
        import subprocess
        subprocess.run(["tar", "-cf", tar, "-C", src, "."], check=True)
        listing = archive.list_archive(tar)
        self.assertEqual(sorted(e.name for e in listing.top_level()), ["abc.txt", "sub"])
        self.assertEqual([e.name for e in listing.children("./sub")], ["in.txt"])
        self._roundtrip(tar, src, ["abc.txt", "sub"])
        # « Remonter » depuis « ./sub » arrive sur « . » : c'est la racine, pas une page vide
        self.assertEqual(sorted(e.name for e in listing.children(".")), ["abc.txt", "sub"])


class AuditLinksTest(unittest.TestCase):
    def test_links_stored_as_links(self):
        """Audit : un lien « up -> .. » dans le dossier compressé n'est pas suivi (archive sans fin)."""
        with tempfile.TemporaryDirectory() as root:
            src = os.path.join(root, "proj")
            os.makedirs(src)
            with open(os.path.join(src, "a.txt"), "w") as f:
                f.write("a")
            os.symlink("..", os.path.join(src, "up"))
            os.symlink("a.txt", os.path.join(src, "lien"))
            for fmt in ("7z", "zip"):
                with self.subTest(fmt=fmt):
                    out = os.path.join(root, "p." + fmt)
                    run(lambda j: archive.create(j, [src], out, archive.CreateOptions(fmt=fmt)))
                    names = sorted(e.path for e in archive.list_archive(out).entries)
                    self.assertEqual(names, ["proj", "proj/a.txt", "proj/lien", "proj/up"])
                    dest = tempfile.mkdtemp(dir=root)
                    run(lambda j: archive.extract(j, out, archive.ExtractOptions(dest=dest, subfolder="never")))
                    # « up -> .. » est écarté à l'extraction (lien dangereux, voir test_archive_safety)
                    self.assertEqual(os.readlink(os.path.join(dest, "proj", "lien")), "a.txt")


class AuditDragCacheTest(unittest.TestCase):
    def test_cache_keeps_only_selection_and_given(self):
        """Audit : changer de sélection ne fait plus grossir le cache du glisser sans fin."""
        from odysseus.ui.archive_drag import DragCache
        with tempfile.TemporaryDirectory() as root:
            def extracted(name):
                folder = tempfile.mkdtemp(dir=root)
                p = os.path.join(folder, name)
                with open(p, "w") as f:
                    f.write(name)
                return p
            d = DragCache(lambda: (None, "", None), lambda m: None)
            a, b, c = extracted("a"), extracted("b"), extracted("c")
            d._store({"a": a}, keep=["a"])
            d.given.add(a)                       # déjà glissé : Nautilus peut encore le copier
            d._store({"b": b}, keep=["b"])
            d._store({"c": c}, keep=["c"])
            self.assertTrue(os.path.exists(a), "élément glissé gardé")
            self.assertFalse(os.path.exists(b), "ancienne sélection jamais glissée : retirée")
            self.assertTrue(os.path.exists(c))
            self.assertEqual(set(d.cache), {"a", "c"})


class AuditVolumesTest(unittest.TestCase):
    def test_volume_sets(self):
        with tempfile.TemporaryDirectory() as d:
            def touch(*names):
                for n in names:
                    open(os.path.join(d, n), "w").close()
                return [os.path.join(d, n) for n in names]
            self.assertEqual(archive.volume_set(touch("a.7z.001", "a.7z.002")[0]), touch("a.7z.001", "a.7z.002"))
            parts = touch("mv.part1.rar", "mv.part2.rar", "mv.part3.rar")
            self.assertEqual(archive.volume_set(parts[0]), parts)
            parts = touch("w.part01.rar", "w.part02.rar")
            self.assertEqual(archive.volume_set(parts[0]), parts)
            z = touch("sp.z01", "sp.z02", "sp.zip")
            self.assertEqual(archive.volume_set(z[2]), z)
            old = touch("o.rar", "o.r00", "o.r01")
            self.assertEqual(archive.volume_set(old[0]), old)
            self.assertEqual(archive.volume_set(touch("seul.zip")[0]), [os.path.join(d, "seul.zip")])


class AuditFailedExtractTest(unittest.TestCase):
    def test_failed_extract_leaves_no_empty_folder(self):
        with tempfile.TemporaryDirectory() as d:
            arc = os.path.join(d, "abime.7z")
            with open(arc, "wb") as f:
                f.write(b"7z\xbc\xaf\x27\x1c" + os.urandom(200))
            j = Job("x", lambda j: archive.extract(j, arc, archive.ExtractOptions(subfolder="always"))).run_sync()
            self.assertNotEqual(j.state, "done")
            self.assertEqual(os.listdir(d), ["abime.7z"])
