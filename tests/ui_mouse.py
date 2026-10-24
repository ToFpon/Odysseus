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
    os.makedirs(os.path.join(D, "taper", "Projets"))
    for n in ("Factures.ods", "fabrication.txt", "photo.jpg", "zèbre.txt", "Prix.txt"):
        open(os.path.join(D, "taper", n), "w").close()
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


def drag_hold(a, b, hold, c_point):
    """Glisser de a jusqu'à b, rester immobile « hold » secondes au-dessus de b, puis déposer en
    c_point() (calculé à ce moment-là : la vue a pu changer de dossier entre-temps)."""
    def run():
        xdo("mousemove", *a)
        time.sleep(0.2)
        xdo("mousedown", 1)
        time.sleep(0.2)
        for k in range(1, 11):
            xdo("mousemove", a[0] + (b[0] - a[0]) * k // 10, a[1] + (b[1] - a[1]) * k // 10)
            time.sleep(0.05)
        xdo("mousemove", b[0] + 12, b[1])         # bien entré dans la cible, puis immobile
        time.sleep(hold)
        c = c_point()
        for k in range(1, 6):
            xdo("mousemove", b[0] + (c[0] - b[0]) * k // 5, b[1] + (c[1] - b[1]) * k // 5)
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
    check(p.location == os.path.join(D, "un"), f"déposé sans s'attarder : le dossier ne s'ouvre pas ({p.location})")
    # comme Nautilus : tenir le fichier au-dessus d'un dossier l'ouvre au bout d'une demi-seconde,
    # et on dépose à l'intérieur (au milieu : la notification « déplacé — Annuler » du dépôt
    # précédent occupe encore le bas de la fenêtre et n'accepte pas de fichiers)
    cible = os.path.join(D, "un", "cible")
    drag_hold(label_point(win, p, "f3"), label_point(win, p, "cible"), 1.2,
              lambda: screen_point(win, p, p.get_width() / 2, p.get_height() / 2))
    for _ in range(6):
        yield
    check(p.location == cible and os.path.exists(os.path.join(cible, "f3")),
          f"glisser en s'attardant sur un dossier : il s'ouvre, on dépose dedans ({p.location})")
    p.open(os.path.join(D, "un"))
    yield
    check(not any(x.has_css_class("drop-hover") for x in win.all_panes()), "pas de cadre de dépôt resté affiché")
    # barre latérale : s'attarder sur « Dossier personnel » l'ouvre, on dépose dedans
    from odysseus.i18n import tr as _tr
    drag_hold(label_point(win, p, "f4"), label_point(win, win.sidebar, _tr("Dossier personnel")), 1.2,
              lambda: screen_point(win, p, p.get_width() / 2, p.get_height() / 2))
    for _ in range(6):
        yield
    home = os.environ["HOME"]
    check(p.location == home and os.path.exists(os.path.join(home, "f4")),
          f"barre latérale : s'attarder sur un emplacement l'ouvre ({p.location})")
    # réglage coupé : le dossier survolé ne s'ouvre plus, le dépôt se fait dessus
    from odysseus.settings import settings as _settings
    _settings["dnd_hover_open"] = False
    p.open(os.path.join(D, "un"))
    yield
    drag_hold(label_point(win, p, "f5"), label_point(win, p, "cible"), 1.2, lambda: label_point(win, p, "cible"))
    for _ in range(6):
        yield
    check(p.location == os.path.join(D, "un") and os.path.exists(os.path.join(cible, "f5")),
          f"réglage coupé : le dossier survolé ne s'ouvre pas ({p.location})")
    _settings["dnd_hover_open"] = True
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
    # clic gauche dans une zone vide : plus rien de sélectionné (comme Nautilus), en liste et en grille
    p.open(os.path.join(D, "taper"))
    yield
    yield
    for mode in ("list", "grid"):
        win.activate_action("win.view-mode", GLib.Variant.new_string(mode))
        yield
        yield
        x, y = label_point(win, p, "photo.jpg")
        xdo("mousemove", x, y, "click", 1)
        yield
        had = [r.name for r in p.selected_recs()]
        ex, ey = screen_point(win, p, p.get_width() / 2, p.get_height() - 80)
        xdo("mousemove", ex, ey, "click", 1)
        yield
        check(had == ["photo.jpg"] and not p.selected_recs(),
              f"{mode} : clic dans le vide, plus rien de sélectionné ({had} → {[r.name for r in p.selected_recs()]})")
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield

    # taper pour aller au fichier (réglage) : la liste reste entière, l'élément est sélectionné
    from odysseus.settings import settings as _st
    p.open(os.path.join(D, "taper"))
    yield
    yield
    x, y = label_point(win, p, "photo.jpg")
    xdo("mousemove", x, y, "click", 1)
    yield
    xdo("type", "--delay", "80", "p")
    yield
    check(win.search_bar.get_search_mode(), "réglage coupé : taper ouvre la recherche (comme Nautilus)")
    xdo("key", "Escape")
    yield
    _st["type_ahead"] = True
    xdo("mousemove", x, y, "click", 1)
    yield
    sel = lambda: [r.name for r in p.selected_recs()]
    xdo("type", "--delay", "80", "pro")
    yield
    check(sel() == ["Projets"] and not win.search_bar.get_search_mode() and len(p.model.items) == 6
          and p.type_ahead_label.get_visible() and p.type_ahead_label.get_label() == "pro",
          f"taper « pro » : Projets sélectionné, liste entière, pas de recherche ({sel()}, {len(p.model.items)})")
    xdo("key", "BackSpace", "BackSpace")
    yield
    check(p.type_ahead_label.get_label() == "p" and sel() == ["Projets"] and p.location == os.path.join(D, "taper"),
          f"Retour arrière corrige la frappe, sans revenir en arrière ({p.type_ahead_label.get_label()!r}, {p.location})")
    yield                                   # plus d'une seconde sans frappe (deux pas de 0,7 s)
    yield
    check(not p.type_ahead_active and not p.type_ahead_label.get_visible(), "une pause : la frappe repart de zéro")
    xdo("type", "--delay", "80", "ze")
    yield
    check(sel() == ["zèbre.txt"], f"« ze » trouve « zèbre » (accents ignorés) ({sel()})")
    xdo("key", "Escape")
    yield
    check(not p.type_ahead_active and not p.type_ahead_label.get_visible(), "Échap annule la frappe")
    xdo("type", "--delay", "80", "pr")
    yield
    xdo("key", "Return")
    yield
    yield
    check(p.location == os.path.join(D, "taper", "Projets"), f"Entrée ouvre l'élément trouvé ({p.location})")
    xdo("type", "--delay", "80", "/")
    yield
    check(win.pathbar.stack.get_visible_child_name() == "entry" and not p.type_ahead_active,
          "« / » ouvre toujours la saisie d'un emplacement")
    xdo("key", "Escape")
    yield
    _st["type_ahead"] = False

    # suggestions sous le champ, comme Nautilus : sous-dossiers qui commencent par le texte tapé
    bar = win.pathbar
    xdo("key", "ctrl+l")
    yield
    xdo("type", "--delay", "60", "/usr/share/a")
    yield
    rows = []
    r = bar.suggest_list.get_first_child()
    while r is not None:
        rows.append(r.path)
        r = r.get_next_sibling()
    want = sorted((e.name for e in os.scandir("/usr/share") if e.name.startswith("a") and e.is_dir()),
                  key=lambda n: GLib.utf8_collate_key_for_filename(n, -1))[:bar.MAX_SUGGESTIONS]
    check(bar.suggest.get_visible() and rows == ["/usr/share/" + n + "/" for n in want],
          f"barre de chemin : suggestions des sous-dossiers ({[os.path.basename(x[:-1]) for x in rows]})")
    xdo("key", "Escape")
    yield
    check(not bar.suggest.get_visible() and bar.stack.get_visible_child_name() == "entry",
          "Échap ferme d'abord la liste, la saisie continue")
    xdo("type", "--delay", "60", "l")
    yield
    xdo("key", "Down", "Return")
    yield
    yield
    first = sorted((e.name for e in os.scandir("/usr/share") if e.name.startswith("al") and e.is_dir()),
                   key=lambda n: GLib.utf8_collate_key_for_filename(n, -1))[0]
    check(p.location == "/usr/share/" + first and not bar.suggest.get_visible(),
          f"flèche bas puis Entrée : on va dans la suggestion ({p.location})")
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
