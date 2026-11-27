"""Audit 2026 (propriétés) : « Changer les permissions des fichiers inclus » ne suit jamais un lien, même glissé
pendant le parcours ; un dossier dont le propriétaire perd l'accès est changé après son contenu."""

import os
import shutil
import stat as st
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from odysseus.ui import properties  # noqa: E402

JOB = types.SimpleNamespace(check_cancel=lambda: None, progress=lambda *a: None)


class ChmodTreeTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "partage", "sous"))
        open(os.path.join(self.d, "partage", "sous", "a.txt"), "w").close()
        os.makedirs(os.path.join(self.d, "victime"))
        open(os.path.join(self.d, "victime", "id_rsa"), "w").close()
        os.chmod(os.path.join(self.d, "victime", "id_rsa"), 0o600)
        os.chmod(os.path.join(self.d, "victime"), 0o700)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def mode(self, *p):
        return st.S_IMODE(os.lstat(os.path.join(self.d, *p)).st_mode)

    def test_symlink_swapped_during_walk_is_not_followed(self):
        real_open = os.open
        sub = os.path.join(self.d, "partage", "sous")

        def swapping_open(path, flags, *a, **kw):
            if path == "sous" and os.path.isdir(sub) and not os.path.islink(sub):
                shutil.rmtree(sub)                    # l'autre utilisateur glisse son lien juste avant l'ouverture
                os.symlink(os.path.join(self.d, "victime"), sub)
            return real_open(path, flags, *a, **kw)
        os.open = swapping_open
        try:
            res = properties.chmod_tree(JOB, os.path.join(self.d, "partage"), 0o666, 0o666, 0o777, 0o777)
        finally:
            os.open = real_open
        self.assertEqual(self.mode("victime"), 0o700)
        self.assertEqual(self.mode("victime", "id_rsa"), 0o600)
        self.assertTrue(res.errors)                   # le lien est signalé, pas suivi

    def test_files_and_folders_changed(self):
        res = properties.chmod_tree(JOB, os.path.join(self.d, "partage"), 0o664, 0o666, 0o775, 0o777)
        self.assertEqual((self.mode("partage"), self.mode("partage", "sous"), self.mode("partage", "sous", "a.txt")),
                         (0o775, 0o775, 0o664))
        self.assertEqual(res.errors, [])
        self.assertEqual(res.count, 3)

    def test_owner_losing_access_changes_folder_last(self):
        res = properties.chmod_tree(JOB, os.path.join(self.d, "partage"), 0o444, 0o666, 0o444, 0o777)
        self.assertEqual(res.errors, [])
        self.assertEqual(self.mode("partage"), 0o444)
        os.chmod(os.path.join(self.d, "partage"), 0o755)
        os.chmod(os.path.join(self.d, "partage", "sous"), 0o755)
        self.assertEqual(self.mode("partage", "sous", "a.txt"), 0o444)

    def test_link_inside_left_alone(self):
        os.symlink(os.path.join(self.d, "victime", "id_rsa"), os.path.join(self.d, "partage", "lien"))
        properties.chmod_tree(JOB, os.path.join(self.d, "partage"), 0o666, 0o666, 0o777, 0o777)
        self.assertEqual(self.mode("victime", "id_rsa"), 0o600)


if __name__ == "__main__":
    unittest.main()
