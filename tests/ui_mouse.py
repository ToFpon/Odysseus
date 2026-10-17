"""Boutons latéraux de la souris avec de vrais clics X11 (xdotool), sous xvfb :

    xvfb-run -a -s "-screen 0 1600x1000x24" python3 tests/ui_mouse.py

Vérifie la navigation (8 = précédent, 9 = suivant), l'absence d'avertissement GTK
« Broken accounting of active state » et que les clics gauche et le clavier marchent ensuite.
"""
import os
import shutil
import subprocess
import sys
import tempfile

if not shutil.which("xdotool"):
    print("xdotool absent : test ignoré.")
    sys.exit(0)

TMP = tempfile.mkdtemp()
os.environ["HOME"] = os.path.join(TMP, "home")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "home", ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "home", ".cache")
os.makedirs(os.environ["HOME"])
D = os.path.join(TMP, "data")
for d in ("un", "deux", "vide"):
    os.makedirs(os.path.join(D, d))
for i in range(6):
    open(os.path.join(D, "un", f"f{i}"), "w").close()
    open(os.path.join(D, "deux", f"g{i}"), "w").close()

if os.environ.get("ODYSSEUS_UI_MOUSE_CHILD") != "1":
    # relancé dans un sous-processus pour lire les avertissements GTK sur stderr
    env = dict(os.environ, ODYSSEUS_UI_MOUSE_CHILD="1", ODYSSEUS_UI_MOUSE_TMP=TMP)
    r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    broken = [line for line in r.stderr.splitlines() if "Broken accounting" in line]
    for line in broken:
        print("ÉCHEC :", line)
    if r.returncode or broken or "ÉCHEC" in r.stdout:
        sys.exit(1)
    print("Aucune erreur.")
    sys.exit(0)

TMP = os.environ["ODYSSEUS_UI_MOUSE_TMP"]
D = os.path.join(TMP, "data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from odysseus.app import OdysseusApp  # noqa: E402

app = OdysseusApp()
failed = []


def xdo(*args):
    subprocess.run(["xdotool", *map(str, args)], check=True)


def check(cond, msg):
    print(("✓ " if cond else "ÉCHEC : ") + msg, flush=True)
    if not cond:
        failed.append(msg)


def steps(win):
    p = win.pane
    for d in ("un", "vide", "deux"):
        p.open(os.path.join(D, d))
        yield
    for button, expected in ((8, "vide"), (8, "un"), (9, "vide"), (9, "deux"), (8, "vide"), (9, "deux")):
        xdo("mousemove", 700, 160, "click", button)
        yield
        check(os.path.basename(p.location) == expected, f"bouton {button} → {expected}")
    xdo("mousemove", 700, 160, "click", 1)
    yield
    check(len(p.selected_recs()) == 1, "clic gauche après les boutons latéraux")
    before = [r.name for r in p.selected_recs()]
    xdo("key", "Down")
    yield
    check([r.name for r in p.selected_recs()] != before, "clavier après les boutons latéraux")


def on_window(_a, win):
    it = steps(win)

    def tick():
        try:
            next(it)
        except StopIteration:
            app.quit()
            return False
        return True
    GLib.timeout_add(1500, lambda: GLib.timeout_add(700, tick) and False)


app.connect("window-added", on_window)
app.run([sys.argv[0]])
sys.exit(1 if failed else 0)
