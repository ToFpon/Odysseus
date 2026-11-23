"""Annuler / rétablir (audit de novembre 2026) : les cas de undo_audit_cases.py tournent dans un processus
à part. Ils changent HOME et les dossiers XDG (corbeille jetable, gvfsd lancé dessus) : dans le même
processus, la corbeille des autres tests aurait changé de place.

    dbus-run-session -- python3.12 -m unittest tests.test_undo_audit
"""

import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class UndoAuditProcessTest(unittest.TestCase):
    def test_cases(self):
        cmd = [sys.executable, "-m", "unittest", "-v", "undo_audit_cases"]
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("dbus-run-session"):
            # son propre bus : un gvfsd déjà lancé par d'autres tests garde le vrai HOME (autre corbeille)
            cmd = ["dbus-run-session", "--"] + cmd
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out[-4000:])
        if "skipped" in out and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            print("\n" + "\n".join(line for line in out.splitlines() if "skipped" in line))


if __name__ == "__main__":
    unittest.main()
