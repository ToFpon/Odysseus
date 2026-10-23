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

PLAYER_LOG = ""
if os.environ.get("ODYSSEUS_UI_MOUSE_CHILD") != "1":     # le sous-processus hérite de tout
    TMP = tempfile.mkdtemp()
    os.environ["HOME"] = os.path.join(TMP, "home")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "home", ".config")
    os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "home", ".cache")
    os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "home", ".local", "share")
    os.makedirs(os.environ["HOME"])
    # faux lecteur vidéo : une ligne par lancement, les noms reçus dans l'ordre
    PLAYER_LOG = os.path.join(TMP, "lecteur.log")
    _apps = os.path.join(os.environ["XDG_DATA_HOME"], "applications")
    os.makedirs(_apps)
    os.makedirs(os.environ["XDG_CONFIG_HOME"])
    with open(os.path.join(TMP, "lecteur.sh"), "w") as _f:
        _f.write(f'#!/bin/sh\nfor a; do printf "%s|" "$(basename "$a")"; done >> {PLAYER_LOG}\necho >> {PLAYER_LOG}\n')
    os.chmod(os.path.join(TMP, "lecteur.sh"), 0o755)
    with open(os.path.join(_apps, "lecteur-test.desktop"), "w") as _f:
        _f.write(f"[Desktop Entry]\nType=Application\nName=Lecteur\nExec={TMP}/lecteur.sh %U\nMimeType=video/mp4;\n")
    with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "mimeapps.list"), "w") as _f:
        _f.write("[Default Applications]\nvideo/mp4=lecteur-test.desktop\n")
    D = os.path.join(TMP, "data")
    for d in ("un", "deux", "vide", os.path.join("un", "cible"), "dst"):
        os.makedirs(os.path.join(D, d))
    for i in range(6):
        open(os.path.join(D, "un", f"f{i}"), "w").close()
        open(os.path.join(D, "deux", f"g{i}"), "w").close()
    os.makedirs(os.path.join(D, "videos"))
    for i in range(1, 5):
        with open(os.path.join(D, "videos", f"Clip ({i}).mp4"), "wb") as _f:
            _f.write(b"\0" * (1000 * (5 - i)))

if os.environ.get("ODYSSEUS_UI_MOUSE_CHILD") != "1":
    # relancé dans un sous-processus pour lire les avertissements GTK sur stderr
    env = dict(os.environ, ODYSSEUS_UI_MOUSE_CHILD="1", ODYSSEUS_UI_MOUSE_TMP=TMP)
    r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    broken = [line for line in r.stderr.splitlines() if "Broken accounting" in line]
    for line in broken:
        print("ÉCHEC :", line)
    if r.returncode or broken or "ÉCHEC" in r.stdout:
        if r.returncode:
            print("\n".join(r.stderr.splitlines()[-40:]))
        sys.exit(1)
    print("Aucune erreur.")
    sys.exit(0)

import faulthandler  # noqa: E402

faulthandler.enable()          # plantage natif (GTK) : la pile Python s'affiche quand même
TMP = os.environ["ODYSSEUS_UI_MOUSE_TMP"]
D = os.path.join(TMP, "data")
PLAYER_LOG = os.path.join(TMP, "lecteur.log")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
import threading  # noqa: E402
import time  # noqa: E402

from gi.repository import Graphene, GLib, Gtk  # noqa: E402

from odysseus.app import OdysseusApp  # noqa: E402

app = OdysseusApp()
failed = []


def xdo(*args):
    subprocess.run(["xdotool", *map(str, args)], check=True)


def check(cond, msg):
    print(("✓ " if cond else "ÉCHEC : ") + msg, flush=True)
    if not cond:
        failed.append(msg)


def screen_point(win, widget, x, y):
    ok, p = widget.compute_point(win, Graphene.Point().init(x, y))
    sx, sy = win.get_native().get_surface_transform()
    return int(p.x + sx), int(p.y + sy)


def label_point(win, root, name):
    def walk(w):
        if isinstance(w, Gtk.Label) and w.get_label() == name and w.get_mapped():
            return w
        c = w.get_first_child()
        while c is not None:
            r = walk(c)
            if r is not None:
                return r
            c = c.get_next_sibling()
    return screen_point(win, walk(root), 10, 8)


def drag(a, b):
    """Glisser réel, dans un fil : la boucle GTK doit traiter les mouvements au fil de l'eau."""
    def run():
        xdo("mousemove", *a)
        time.sleep(0.2)
        xdo("mousedown", 1)
        time.sleep(0.2)
        for k in range(1, 11):
            xdo("mousemove", a[0] + (b[0] - a[0]) * k // 10, a[1] + (b[1] - a[1]) * k // 10)
            time.sleep(0.05)
        time.sleep(0.3)
        xdo("mouseup", 1)
    threading.Thread(target=run, daemon=True).start()


def _all_popovers(win):
    out = []

    def walk(w):
        if isinstance(w, Gtk.Popover):
            out.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()
    walk(win)
    return out


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
    # glisser-déposer : sur un dossier, puis vers l'autre panneau
    p.open(os.path.join(D, "un"))
    yield
    drag(label_point(win, p, "f1"), label_point(win, p, "cible"))
    for _ in range(5):
        yield
    check(os.path.exists(os.path.join(D, "un", "cible", "f1")), "glisser un fichier sur un dossier")
    check(not any(x.has_css_class("drop-hover") for x in win.all_panes()), "pas de cadre de dépôt resté affiché")
    win.activate_action("win.dual", None)
    yield
    other = win.workspace.other(p)
    other.open(os.path.join(D, "dst"))
    yield
    drag(label_point(win, p, "f2"), screen_point(win, other, other.get_width() / 2, other.get_height() - 60))
    for _ in range(5):
        yield
    check(os.path.exists(os.path.join(D, "dst", "f2")), "glisser vers l'autre panneau")
    check(not any(x.has_css_class("drop-hover") for x in win.all_panes()), "pas de cadre resté en double panneau")

    # Touches tapées dans un champ de saisie : elles vont au texte, pas aux fichiers (GTK 4 traite les
    # raccourcis de l'application avant le widget qui a le focus)
    win.activate_action("win.dual", None)
    yield
    p = win.pane
    with open(os.path.join(D, "important.txt"), "w") as f:
        f.write("x")
    p.open(D)
    yield
    yield
    p.model.select_names(["important.txt"])
    xdo("key", "ctrl+f")
    yield
    xdo("type", "--delay", "60", "imp")
    yield
    xdo("key", "Home", "Delete")
    yield
    yield
    check(os.path.exists(os.path.join(D, "important.txt")) and win.search_entry.get_text() == "mp",
          f"Suppr dans la recherche efface un caractère, ne jette pas le fichier ({win.search_entry.get_text()!r})")
    xdo("key", "End", "BackSpace")
    yield
    check(p.location == D and win.search_entry.get_text() == "m", "Retour arrière dans la recherche efface, ne navigue pas")
    xdo("key", "Escape")
    yield
    xdo("key", "ctrl+l")
    yield
    xdo("type", "--delay", "60", "/usr/share")
    yield
    xdo("key", "Return")
    yield
    yield
    check(p.location == "/usr/share", f"Ctrl+L puis « /usr/share » au clavier ({p.location})")
    # AZERTY (Tof) et QWERTZ : « / » et « ~ » se tapent avec Maj / AltGr, le raccourci se déclenchait
    for layout in ("fr", "de"):
        if subprocess.run(["setxkbmap", layout], capture_output=True).returncode != 0:
            print(f"- disposition {layout} indisponible : sautée")
            continue
        p.open(D)
        yield
        xdo("key", "ctrl+l")
        yield
        xdo("type", "--delay", "60", "/usr/share/doc")
        yield
        xdo("key", "Return")
        yield
        yield
        check(p.location == "/usr/share/doc", f"[{layout}] Ctrl+L puis « /usr/share/doc » ({p.location})")
        p.open(D)
        yield
        xdo("key", "ctrl+f")
        yield
        xdo("type", "--delay", "60", "i/m~p")
        yield
        yield
        check(win.search_entry.get_text() == "i/m~p" and p.location == D,
              f"[{layout}] « / » et « ~ » tapés dans la recherche ({win.search_entry.get_text()!r})")
        xdo("key", "Escape")
        yield
    subprocess.run(["setxkbmap", "us"], capture_output=True)
    # Ctrl + molette : zoom de la liste puis de la grille, comme Nautilus ; sans Ctrl, ça défile
    from odysseus.settings import settings
    p.open(D)
    yield
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield
    x, y = screen_point(win, p.list_view, 200, 120)
    before = settings["list_zoom"]
    xdo("mousemove", x, y, "keydown", "ctrl", "click", 4, "keyup", "ctrl")
    yield
    check(settings["list_zoom"] != before, f"Ctrl+molette agrandit la liste ({before} → {settings['list_zoom']})")
    xdo("keydown", "ctrl", "click", 5, "keyup", "ctrl")
    yield
    check(settings["list_zoom"] == before, f"Ctrl+molette réduit la liste ({settings['list_zoom']})")
    win.activate_action("win.view-mode", GLib.Variant.new_string("grid"))
    yield
    yield
    before = settings["grid_zoom"]
    x, y = screen_point(win, p.grid_view, 200, 120)
    xdo("mousemove", x, y, "keydown", "ctrl", "click", 5, "keyup", "ctrl")
    yield
    check(settings["grid_zoom"] != before, f"Ctrl+molette réduit la grille ({before} → {settings['grid_zoom']})")
    xdo("keydown", "ctrl", "click", 4, "keyup", "ctrl")
    yield
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield
    # Menu contextuel au clic droit (liste, autre panneau, barre latérale), fermé par Échap la souris
    # dessus, puis la fenêtre remise en page : le menu détruit à la fermeture restait le widget « sous
    # le pointeur » pour GTK 4.14, qui plantait (gtk_window_native_layout, surface nulle). Les menus
    # sont gardés et réutilisés (common.popup_menu). Clic droit maintenu puis relâché sur une entrée :
    # avertissement bénin de GTK (« Broken accounting of active state »), pas testé ici.
    p.open(D)
    yield
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield

    def menu_open():
        return any(isinstance(w, Gtk.PopoverMenu) and w.get_visible() for w in _all_popovers(win))
    x, y = screen_point(win, p.list_view, 200, 60)
    xdo("mousemove", x, y, "click", 3)
    yield
    check(menu_open(), "clic droit : menu contextuel ouvert")
    xdo("key", "Escape")
    yield
    # double panneau : menu de l'autre panneau, puis panneau retiré (menu détaché avec lui)
    win.activate_action("win.dual", None)
    yield
    other = win.workspace.other(p)
    ox, oy = screen_point(win, other, other.get_width() / 2, 80)
    xdo("mousemove", ox, oy, "click", 3)
    yield
    check(menu_open(), "clic droit dans l'autre panneau : menu ouvert")
    xdo("key", "Escape")
    yield
    win.activate_action("win.dual", None)
    yield
    p = win.pane
    win.activate_action("win.view-mode", GLib.Variant.new_string("grid"))
    yield
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield
    row = win.sidebar.places_list.get_first_child()
    sx, sy = screen_point(win, row, 30, 10)
    xdo("mousemove", sx, sy, "click", 3)
    yield
    check(menu_open(), "barre latérale : menu au clic droit")
    xdo("key", "Escape")
    yield
    # Annoter une image, outil texte : un vrai clic sur l'image, puis plusieurs lettres tapées.
    # Le dialogue s'ouvrait à l'appui du bouton ; le relâchement rendait le clavier à la fenêtre
    # d'annotation et seule la première lettre arrivait dans le champ.
    import cairo
    img = os.path.join(D, "photo.png")
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 400, 300)
    surf.write_to_png(img)
    from odysseus.ui.annotate import AnnotateWindow
    aw = AnnotateWindow(win, img)
    aw.present()
    yield
    yield
    aw.tool = "text"
    x, y = screen_point(aw, aw.canvas, aw.canvas.get_width() // 2, aw.canvas.get_height() // 2)
    xdo("mousemove", x, y, "mousedown", 1)     # bouton tenu comme une vraie main…
    yield
    xdo("mouseup", 1)                          # … relâché une fois le dialogue apparu
    yield
    xdo("type", "--delay", "60", "Bonjour")
    yield
    xdo("key", "Return")
    yield
    texts = [sh.text for sh in aw.shapes]
    check(texts == ["Bonjour"], f"annotation : texte de plusieurs lettres ({texts})")
    aw.dirty = False
    aw.destroy()
    yield
    # Barre d'info du bas : s'efface sous le pointeur, revient quand il s'en va (comme Nautilus)
    bar = p.floating
    bar.set_text("« fichier.txt » sélectionné (206 octets)")
    bar.set_visible(True)
    yield
    x, y = screen_point(win, bar, bar.get_width() // 2, bar.get_height() // 2)
    xdo("mousemove", x, y)
    yield
    yield
    check(bar.has_css_class("hidden"), "barre d'info : effacée sous le pointeur")
    xdo("mousemove", x, y - 200)
    yield
    yield
    check(not bar.has_css_class("hidden"), "barre d'info : revenue quand le pointeur s'en va")
    # « Ce PC » : comme les fichiers, un clic sélectionne (sans ouvrir), un double-clic ouvre
    p.open("computer:///")
    yield
    yield
    tile = p.pages["computer:///"].sections["drives"][1].get_first_child()
    x, y = screen_point(win, tile, 40, 30)
    xdo("mousemove", x, y, "click", 1)
    yield
    yield
    check(tile.is_selected() and p.location == "computer:///", "Ce PC : un clic sélectionne sans ouvrir")
    xdo("click", "--repeat", "2", "--delay", "80", 1)
    yield
    yield
    check(p.location == "/", f"Ce PC : double-clic ouvre ({p.location})")

    # double-clic sur un fichier d'une sélection multiple : TOUTE la sélection s'ouvre, en un seul
    # lancement, dans l'ordre des noms (GTK 4.14 réduisait la sélection au premier clic) ; un
    # simple clic, lui, réduit la sélection au fichier cliqué
    p.open(os.path.join(D, "videos"))
    yield
    yield
    x, y = label_point(win, win, "Clip (3).mp4")
    xdo("mousemove", x, y, "click", 1)
    yield
    xdo("key", "ctrl+a")
    yield
    open(PLAYER_LOG, "w").close()
    xdo("mousemove", x, y, "click", "--repeat", "2", "--delay", "80", 1)
    yield
    yield
    yield
    runs = open(PLAYER_LOG).read().splitlines()
    check(runs == ["Clip (1).mp4|Clip (2).mp4|Clip (3).mp4|Clip (4).mp4|"] and len(p.selected_recs()) == 4,
          f"double-clic dans une sélection : toute la sélection, un seul lecteur ({runs})")
    xdo("mousemove", x, y, "click", 1)
    yield
    yield
    check(len(p.selected_recs()) == 1 and p.selected_recs()[0].name == "Clip (3).mp4",
          f"simple clic dans une sélection : lui seul ({[r.name for r in p.selected_recs()]})")


def on_window(_a, win):
    if getattr(app, "_started", False):     # fenêtres ouvertes par les étapes (annotation…)
        return
    app._started = True
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
