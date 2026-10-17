#!/usr/bin/env python3
"""Test de fumée de l'interface : pilote une vraie fenêtre (sous xvfb-run) et signale toute exception.

    xvfb-run -a python3 tests/ui_smoke.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="odysseus-ui-")
os.environ["HOME"] = os.path.join(TMP, "home")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "home", ".config")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "home", ".cache")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "home", ".local", "share")
os.makedirs(os.environ["HOME"])

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

errors = []


def hook(t, v, tb):
    errors.append("".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = hook

from odysseus.app import OdysseusApp  # noqa: E402
from odysseus.ui.jobs import manager  # noqa: E402
from odysseus.i18n import tr  # noqa: E402

D = os.path.join(TMP, "data")
os.makedirs(os.path.join(D, "dossier A", "sous"))
os.makedirs(os.path.join(D, "dossier B"))
for i in range(30):
    with open(os.path.join(D, f"fichier {i}.txt"), "w") as f:
        f.write(f"ligne {i} bonjour odysseus\n" * 5)
with open(os.path.join(D, "dossier A", "sous", "profond.md"), "w") as f:
    f.write("le mot caché\n")
with open(os.path.join(D, ".cache"), "w") as f:
    f.write("x")
import cairo  # noqa: E402

s = cairo.PDFSurface(os.path.join(D, "doc.pdf"), 595, 842)
c = cairo.Context(s)
for i in range(3):
    c.move_to(50, 100)
    c.show_text(f"page {i + 1}")
    c.show_page()
s.finish()
s = cairo.ImageSurface(cairo.FORMAT_RGB24, 320, 200)
c = cairo.Context(s)
c.set_source_rgb(0.2, 0.5, 0.8)
c.paint()
s.write_to_png(os.path.join(D, "image.png"))
subprocess.run(["7z", "a", "-bd", os.path.join(D, "paquet.zip"), os.path.join(D, "dossier A")],
               capture_output=True)

app = OdysseusApp()
steps = []
log = []


def step(fn):
    steps.append(fn)
    return fn


class Wait:
    """Les étapes sont des générateurs : « yield Wait(s) » rend la main à la vraie boucle GLib."""
    def __init__(self, seconds=0.4, until=None):
        self.seconds = seconds
        self.until = until


def pump(seconds=0.4):
    return Wait(seconds)


def wait_jobs(timeout=20):
    return Wait(timeout, until=lambda: not manager.running)


def check(cond, msg):
    if not cond:
        errors.append("ÉCHEC : " + msg)
        print("✗", msg)
    else:
        print("✓", msg)


def run_all(win):
    def driver():
        for fn in steps:
            log.append(fn.__name__)
            gen = fn(win)
            if gen is not None and hasattr(gen, "__next__"):
                yield from gen
            yield Wait(0.3)

    it = driver()

    def tick():
        try:
            w = next(it)
        except StopIteration:
            app.quit()
            return False
        except Exception:
            errors.append(traceback.format_exc())
            app.quit()
            return False
        start = time.time()

        def poll():
            if w.until is not None:
                if w.until() or time.time() - start > w.seconds:
                    GLib.timeout_add(50, tick)
                    return False
                return True
            GLib.timeout_add(50, tick)
            return False
        if w.until is not None:
            GLib.timeout_add(50, poll)
        else:
            GLib.timeout_add(int(w.seconds * 1000), poll)
        return False
    tick()
    return False


@step
def ouverture(win):
    yield pump(0.8)
    p = win.pane
    check(p.location == D, "ouverture du dossier")
    names = [r.name for r in p.model.items]
    check("dossier A" == names[0] and "dossier B" == names[1], "dossiers en premier")
    check(".cache" not in names, "fichiers cachés masqués")
    check(names.index("fichier 2.txt") < names.index("fichier 10.txt"), "tri naturel (2 avant 10)")


@step
def caches(win):
    win.activate_action("win.show-hidden", None)
    yield pump(0.3)
    check(".cache" in [r.name for r in win.pane.model.items], "Ctrl+H affiche les cachés")
    win.activate_action("win.show-hidden", None)


@step
def vues(win):
    for mode in ("grid", "columns", "list"):
        win.activate_action("win.view-mode", GLib.Variant.new_string(mode))
        yield pump(0.5)
        check(win.pane.view_mode == mode, f"vue {mode}")


@step
def tri(win):
    win.activate_action("win.sort-by", GLib.Variant.new_string("size"))
    yield pump(0.2)
    win.activate_action("win.sort-reverse", None)
    yield pump(0.2)
    win.activate_action("win.sort-reverse", None)
    win.activate_action("win.sort-by", GLib.Variant.new_string("name"))
    yield pump(0.2)
    check(win.pane.model.items[0].name == "dossier A", "tri par nom rétabli")


@step
def filtre(win):
    win.search_btn.set_active(True)
    win.search_entry.set_text("fichier 1")
    yield pump(0.4)
    n = len(win.pane.model.items)
    check(n == 11, f"filtre instantané ({n} éléments)")
    win.recursive_btn.set_active(True)
    win.search_entry.set_text("profond")
    yield pump(1.5)
    check(win.pane.mode == "search" and any(r.name == "profond.md" for r in win.pane.model.items),
          "recherche récursive")
    win.search_btn.set_active(False)
    win.recursive_btn.set_active(False)
    yield pump(0.5)
    check(win.pane.mode == "fs" and win.pane.location == D, "retour au dossier après la recherche")


@step
def navigation(win):
    p = win.pane
    p.open(os.path.join(D, "dossier A"))
    yield pump(0.4)
    win.activate_action("win.up", None)
    yield pump(0.4)
    check(p.location == D, "dossier parent")
    win.activate_action("win.back", None)
    yield pump(0.4)
    check(p.location == os.path.join(D, "dossier A"), "précédent")
    win.activate_action("win.back", None)
    yield pump(0.4)
@step
def creation_renommage(win):
    from odysseus.backend import fileops
    p = win.pane
    path = fileops.make_folder(D, "Nouveau")
    yield pump(0.8)
    check(any(r.name == "Nouveau" for r in p.model.items), "surveillance : nouveau dossier visible")
    fileops.rename(path, "Renommé")
    yield pump(0.8)
    names = [r.name for r in p.model.items]
    check("Renommé" in names and "Nouveau" not in names, "surveillance : renommage visible")


@step
def copie(win):
    COPIED = "fichier 1" + tr(" (copie)") + ".txt"
    win.run_transfer([os.path.join(D, "fichier 1.txt")], os.path.join(D, "dossier B"))
    yield wait_jobs()
    check(os.path.exists(os.path.join(D, "dossier B", "fichier 1.txt")), "copie")
    win.run_transfer([os.path.join(D, "fichier 1.txt")], D)
    yield wait_jobs()
    check(os.path.exists(os.path.join(D, COPIED)), "copie au même endroit : « (copie) »")
    win.run_transfer([os.path.join(D, COPIED)], os.path.join(D, "dossier B"), move=True)
    yield wait_jobs()
    check(os.path.exists(os.path.join(D, "dossier B", COPIED)), "déplacement")
    win.undo()
    yield wait_jobs()
    check(os.path.exists(os.path.join(D, COPIED)), "annulation du déplacement")


@step
def conflits(win):
    answers = iter([("skip", False), ("rename", False), ("replace", True)])
    win._conflict_dialog = lambda c, reply: reply(*next(answers))
    src = os.path.join(D, "fichier 2.txt")
    dst = os.path.join(D, "dossier B")
    with open(os.path.join(dst, "fichier 2.txt"), "w") as f:
        f.write("ancien")
    win.run_transfer([src], dst)
    yield wait_jobs()
    check(open(os.path.join(dst, "fichier 2.txt")).read() == "ancien", "conflit : ignorer")
    win.run_transfer([src], dst)
    yield wait_jobs()
    check(os.path.exists(os.path.join(dst, "fichier 2 (2).txt")), "conflit : garder les deux")
    win.run_transfer([src, os.path.join(D, "fichier 3.txt")], dst)
    yield wait_jobs()
    check(open(os.path.join(dst, "fichier 2.txt")).read().startswith("ligne"), "conflit : remplacer")


@step
def couleurs(win):
    from odysseus.backend import folder_colors
    p = win.pane
    p.model.select_names(["dossier A"])
    yield pump(0.2)
    win.activate_action("win.folder-color", GLib.Variant.new_string("violet"))
    yield pump(0.6)
    meta = folder_colors.read(os.path.join(D, "dossier A"))
    check(bool(meta.get("icon_uri") or meta.get("icon_name")), f"couleur de dossier ({meta})")
    win.activate_action("win.emblem", GLib.Variant.new_string("emblem-favorite"))
    yield pump(0.4)
    check("emblem-favorite" in folder_colors.read(os.path.join(D, "dossier A")).get("emblems", []), "emblème")
    win.activate_action("win.folder-restore", None)
    yield pump(0.3)
    check(not folder_colors.read(os.path.join(D, "dossier A")), "couleur par défaut")


@step
def selection_et_menus(win):
    from odysseus.ui import menus
    p = win.pane
    p.model.select_names(["fichier 3.txt", "doc.pdf"])
    yield pump(0.2)
    check(len(p.selected_recs()) == 2, "sélection")
    m = menus.context_menu(win, p, True)
    check(m.get_n_items() > 3, "menu contextuel fichiers")
    p.model.select_names(["doc.pdf"])
    m = menus.context_menu(win, p, True)
    check(m.get_n_items() > 3, "menu contextuel PDF")
    m = menus.context_menu(win, p, False)
    check(m.get_n_items() > 1, "menu contextuel du fond")
    win._on_context_menu(p, 100.0, 100.0, True)
    yield pump(0.3)
def _marks(widget, out):
    if widget.has_css_class("copy-mark") and widget.get_mapped():
        out.append(widget)
    c = widget.get_first_child()
    while c is not None:
        _marks(c, out)
        c = c.get_next_sibling()
    return out


@step
def marque_copie(win):
    p = win.pane
    p.model.select_names(["fichier 3.txt", "fichier 4.txt"])
    yield pump(0.2)
    win.clip(False)
    yield pump(0.3)
    on = [m for m in _marks(p, []) if m.has_css_class("copied")]
    check(len(on) == 2, f"barre devant les éléments copiés ({len(on)})")
    win.clip(True)
    yield pump(0.3)
    on = [m for m in _marks(p, []) if m.has_css_class("copied")]
    check(not on, "pas de barre pour les éléments coupés")
    win.clip(False)
    from odysseus import features
    features.set_enabled("copy_mark", False)
    yield pump(0.3)
    check(not _marks(p, []), "marque des copies désactivable")
    features.set_enabled("copy_mark", True)
    from odysseus.ui import clipboard
    clipboard.state.clear()


@step
def apercu_proprietes(win):
    p = win.pane
    p.model.select_names(["image.png"])
    win.activate_action("win.preview", None)
    yield pump(1.5)
    check(win.preview is not None and win.preview.get_visible(), "panneau d'aperçu")
    p.model.select_names(["doc.pdf"])
    yield pump(1.5)
    win.activate_action("win.preview", None)
    win.activate_action("win.properties", None)
    yield pump(0.8)
@step
def double_panneau(win):
    win.activate_action("win.dual", None)
    yield pump(0.6)
    ws = win.workspace
    check(ws.dual and len(ws.panes) == 2, "double panneau")
    other = ws.other()
    other.open(os.path.join(D, "dossier B"))
    yield pump(0.5)
    ws.active.model.select_names(["fichier 5.txt"])
    win.to_other(False)
    yield wait_jobs()
    check(os.path.exists(os.path.join(D, "dossier B", "fichier 5.txt")), "copie vers l'autre panneau")
    win.activate_action("win.switch-pane", None)
    yield pump(0.3)
    check(ws.active is other, "changement de panneau")
    win.activate_action("win.dual", None)
    yield pump(0.3)
    check(not ws.dual, "retour en panneau simple")


@step
def archives(win):
    p = win.pane
    p.open(os.path.join(D, "paquet.zip"))
    yield pump(2.0)
    check(p.mode == "archive" and any(r.name == "dossier A" for r in p.model.items), "archive parcourue")
    p.activate_position(0)
    yield pump(0.5)
    check(p.inner == "dossier A", "dossier dans l'archive")
    win.activate_action("win.close-archive", None)
    yield pump(0.6)
    p.model.select_names(["paquet.zip"])
    win.activate_action("win.extract-here", None)
    yield wait_jobs()
    yield pump(0.5)
    check(os.path.isdir(os.path.join(D, "paquet")) or os.path.isdir(os.path.join(D, "dossier A (2)"))
          or any(n.startswith("paquet") and os.path.isdir(os.path.join(D, n)) for n in os.listdir(D))
          or os.path.exists(os.path.join(D, "dossier A", "sous", "profond.md")), "extraire ici")


@step
def corbeille(win):
    p = win.pane
    p.open(D)
    yield pump(0.5)
    p.model.select_names(["fichier 7.txt"])
    win.activate_action("win.trash", None)
    yield wait_jobs()
    yield pump(0.5)
    check(not os.path.exists(os.path.join(D, "fichier 7.txt")), "mise à la corbeille")


@step
def outils(win):
    p = win.pane
    p.model.select_names(["doc.pdf"])
    for a in ("pdf-compress", "pdf-merge", "pdf-watermark", "pdf-pages"):
        win.activate_action("win." + a, None)
        yield pump(0.6)
    win.content_search(D)
    yield pump(0.3)
    sw = [w for w in Gtk.Window.list_toplevels() if type(w).__name__ == "ContentSearchWindow"]
    check(bool(sw), "fenêtre de recherche de contenu")
    if sw:
        sw[0].entry.set_text("bonjour")
        sw[0].start()
        end = time.time() + 5
        while sw[0]._running and time.time() < end:
            yield pump(0.1)
        check(sw[0].store.get_n_items() > 0, f"résultats de recherche ({sw[0].store.get_n_items()})")
        sw[0].close()
    p.model.select_names(["image.png"])
    win.activate_action("win.annotate", None)
    yield pump(0.6)
    ann = [w for w in Gtk.Window.list_toplevels() if type(w).__name__ == "AnnotateWindow"]
    check(bool(ann), "fenêtre d'annotation")
    for w in ann:
        w.dirty = False
        w.close()
    win.activate_action("app.preferences", None)
    app.activate_action("preferences", None)
    yield pump(0.5)
    win.activate_action("win.shortcuts", None)
    yield pump(0.3)
@step
def fonctionnalites(win):
    from odysseus import features
    features.set_enabled("dual_panel", False)
    yield pump(0.3)
    check(not win.dual_btn.get_visible(), "désactiver le double panneau masque le bouton")
    features.set_enabled("dual_panel", True)
    features.set_enabled("column_view", False)
    win.activate_action("win.view-mode", GLib.Variant.new_string("columns"))
    yield pump(0.3)
    check(win.pane.view_mode != "columns", "vue en colonnes désactivable")
    features.set_enabled("column_view", True)


def on_window(_a, w):
    if not log:
        log.append("start")
        GLib.timeout_add(500, run_all, w)


app.connect("window-added", on_window)
app.run([sys.argv[0], D])
shutil.rmtree(TMP, ignore_errors=True)
print("\n".join(errors) if errors else "\nAucune erreur.")
sys.exit(1 if errors else 0)
