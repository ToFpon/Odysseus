#!/usr/bin/env python3
"""Test de fumée de l'interface : pilote une vraie fenêtre (sous xvfb-run) et signale toute exception.

    xvfb-run -a python3 tests/ui_smoke.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
GVFSD = None
if os.environ.get("ODYSSEUS_UI_GVFS") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    # corbeille GVfs (trash:///) sur le HOME jetable : gvfsd lancé ici, avec cet environnement
    # (lancé à part, il garde le vrai HOME et montre la vraie corbeille). Sous dbus-run-session.
    GVFSD = subprocess.Popen(["/usr/libexec/gvfsd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402

errors = []


def hook(t, v, tb):
    errors.append("".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = hook

from odysseus.app import OdysseusApp  # noqa: E402
from odysseus.ui.jobs import manager  # noqa: E402
from odysseus.i18n import ntr, tr  # noqa: E402
from odysseus.settings import settings  # noqa: E402

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
def bouton_colonnes(win):
    win.columns_btn.set_active(True)
    yield pump(0.5)
    check(win.pane.view_mode == "columns", "bouton vue en colonnes")
    win.columns_btn.set_active(False)
    yield pump(0.5)
    check(win.pane.view_mode == "list" and not win.columns_btn.get_active(), "retour à la liste par le bouton")


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
    lv = win.pane.list_view
    lv.view.sort_by_column(lv.columns["size"], Gtk.SortType.DESCENDING)   # clic sur l'en-tête « Taille »
    yield pump(0.3)
    check(settings["sort_key"] == "size" and settings["sort_reverse"], "tri par clic sur l'en-tête")
    lv.view.sort_by_column(lv.columns["name"], Gtk.SortType.ASCENDING)
    yield pump(0.3)
    check(settings["sort_key"] == "name" and not settings["sort_reverse"]
          and win.pane.model.items[0].name == "dossier A", "retour au tri par nom par l'en-tête")


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
    # boutons latéraux de la souris (8 / 9)
    before = p.location
    p.open(os.path.join(D, "dossier B"))
    yield pump(0.4)
    win._mouse_nav(8, 400.0, 300.0)
    yield pump(0.4)
    check(p.location == before, "bouton « précédent » de la souris")
    win._mouse_nav(9, 400.0, 300.0)
    yield pump(0.4)
    check(p.location == os.path.join(D, "dossier B"), "bouton « suivant » de la souris")
    p.open(D)
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
def annuler_retablir(win):
    p = win.pane
    src, dst = os.path.join(D, "fichier 7.txt"), os.path.join(D, "dossier B", "fichier 7.txt")
    win.run_transfer([src], os.path.join(D, "dossier B"), move=True)
    yield wait_jobs()
    check(os.path.exists(dst), "déplacement à annuler")
    check(win.lookup_action("undo").get_enabled() and win.undo_label(win.undo_stack[-1]) ==
          tr("Annuler le déplacement"), "menu : « Annuler le déplacement »")
    win.undo()
    yield wait_jobs()
    check(os.path.exists(src) and win.lookup_action("redo").get_enabled(), "annulation, Rétablir disponible")
    win.redo()
    yield wait_jobs()
    check(os.path.exists(dst) and not os.path.exists(src), "rétablir refait le déplacement")
    win.undo()
    yield wait_jobs()
    check(os.path.exists(src), "nouvelle annulation après rétablissement")


@step
def dossier_bloque(win):
    """Un dossier qui ne répond plus (stat figé sur une clé ou un montage réseau) ne doit pas geler
    l'autre panneau : l'attente du verrou de lecture est plafonnée."""
    from odysseus.backend import scanner
    gate = threading.Event()
    real = scanner.scan_local
    lent = os.path.join(D, "dossier A")

    def scan(path, cancel, with_stat=True):
        if path == lent:
            gate.wait(20)          # simule un périphérique qui ne répond pas
        return real(path, cancel, with_stat)
    scanner.scan_local = scan
    try:
        win.activate_action("win.dual", None)
        yield pump(0.5)
        p = win.pane
        other = win.workspace.other(p)
        p.open(lent)
        yield pump(0.2)
        t0 = time.monotonic()
        other.open(os.path.join(D, "dossier B"))
        yield Wait(5, until=lambda: not other.model.loading)
        waited = time.monotonic() - t0
        check(not other.model.loading and waited < 2, f"panneau non gelé par un dossier bloqué ({waited:.1f} s)")
        check(p.model.loading, "le dossier bloqué attend toujours (normal)")
    finally:
        gate.set()
        scanner.scan_local = real
    yield pump(0.5)
    win.activate_action("win.dual", None)
    yield pump(0.3)
    win.pane.open(D)
    yield pump(0.5)


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
def autres_emplacements_et_barre_info(win):
    from odysseus.ui import menus
    p = win.pane
    p.model.select_names(["dossier A", "dossier B"])
    yield pump(0.8)
    check(p.status_text().startswith(ntr("{0} dossier sélectionné", "{0} dossiers sélectionnés", 2).format(2)),
          "barre d'info : dossiers sélectionnés")
    p.model.select_names(["dossier A", "fichier 3.txt"])
    yield pump(0.3)
    check(", " in p.status_text(), "barre d'info : dossiers et autres éléments")
    check(menus.folder_menu().get_n_items() >= 3, "menu ⋮ de la barre de chemin")
    p.open("other-locations:///")
    yield pump(0.8)
    check(p.mode == "other" and p.stack.get_visible_child_name() == "other", "page Autres emplacements")
    check(p.other_page.dev_list.get_first_child() is not None, "Autres emplacements : disques listés")
    win.activate_action("win.back", None)
    yield pump(0.6)
    check(p.mode == "fs" and p.location == D, "retour depuis Autres emplacements")


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
def copie_partielle_rafraichit(win):
    """Copie / déplacement terminés avec des erreurs (ici un fichier spécial) : la vue de la
    destination et celle de la source doivent quand même se mettre à jour."""
    src = os.path.join(D, "lot partiel")
    os.makedirs(src)
    for n in ("un.txt", "deux.txt"):
        with open(os.path.join(src, n), "w") as f:
            f.write(n)
    os.mkfifo(os.path.join(src, "tube"))
    dest = tempfile.mkdtemp(dir=D, prefix="cle vide ")     # comme une clé vide
    p = win.pane
    p.open(dest)
    yield Wait(5, until=lambda: not p.model.loading)
    check(p.stack.get_visible_child_name() == "empty", "dossier vide affiché")
    win.run_transfer([src], dest)
    yield wait_jobs()
    yield pump(1.0)
    _close_alerts()
    check("lot partiel" in [r.name for r in p.model.items] and p.stack.get_visible_child_name() == "list",
          f"copie dans un dossier vide : la liste apparaît ({p.stack.get_visible_child_name()})")
    shutil.rmtree(os.path.join(dest, "lot partiel"))
    yield pump(1.0)
    check(p.stack.get_visible_child_name() == "empty", "dernier élément supprimé : « Dossier vide »")
    p.open(src)
    yield Wait(5, until=lambda: not p.model.loading)
    # autre système de fichiers (comme une clé) : le tube ne peut pas être copié, il reste
    other = tempfile.mkdtemp(dir="/dev/shm") if os.path.isdir("/dev/shm") else tempfile.mkdtemp()
    win.run_transfer([os.path.join(src, n) for n in ("un.txt", "deux.txt", "tube")], other, move=True)
    yield wait_jobs()
    yield pump(1.0)
    _close_alerts()
    names = sorted(r.name for r in p.model.items)
    check(names == ["tube"], f"déplacement avec erreurs : source rafraîchie ({names})")
    shutil.rmtree(other, ignore_errors=True)
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


def _close_alerts():
    for w in Gtk.Window.list_toplevels():
        if type(w).__name__ in ("MessageDialog", "AlertDialog") or isinstance(w, Adw.MessageDialog):
            w.close()


@step
def dialogues_fenetres(win):
    """Les dialogues sont de vraies fenêtres rattachées à la fenêtre principale (comme Nautilus 46 :
    le thème du système s'y applique), et la question « Remplacer ? » n'est pas cachée par la
    fenêtre de progression."""
    p = win.pane
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)

    def opened(before):
        return [w for w in Gtk.Window.list_toplevels() if w not in before and w.get_visible()]
    cases = [("win.properties", ["doc.pdf"]), ("win.compress", ["doc.pdf"]), ("win.pdf-watermark", ["doc.pdf"]),
             ("win.rename", ["doc.pdf"]), ("win.delete", ["doc.pdf"]), ("win.shortcuts", []),
             ("win.open-with", ["image.png"]), ("app.preferences", []), ("app.about", [])]
    for action, names in cases:
        p.model.select_names(names) if names else p.unselect_all()
        before = list(Gtk.Window.list_toplevels())
        if action.startswith("app."):
            app.activate_action(action[4:], None)
        else:
            win.activate_action(action, None)
        yield pump(0.6)
        new = opened(before)
        check(len(new) == 1 and new[0].get_transient_for() is win,
              f"{action} : vraie fenêtre posée sur la fenêtre principale ({[type(w).__name__ for w in new]})")
        for w in new:
            w.close()
        yield pump(0.3)
    check(os.path.exists(os.path.join(D, "doc.pdf")), "fermer la confirmation ne supprime rien")

    from odysseus.backend import fileops
    from odysseus.ui.jobs import ProgressWindow
    if win.progress_window is None:
        win.progress_window = ProgressWindow(win)
    pw = win.progress_window
    pw.present()
    yield pump(0.3)
    replies = []
    c = fileops.Conflict(os.path.join(D, "image.png"), os.path.join(D, "doc.pdf"), False, False, False)
    before = list(Gtk.Window.list_toplevels())
    type(win)._conflict_dialog(win, c, lambda r, a: replies.append(r))   # (remplacée plus haut)
    yield pump(0.4)
    q = [w for w in opened(before) if w is not pw]
    check(len(q) == 1 and not pw.get_visible(), "« Remplacer ? » : la fenêtre de progression s'efface")
    for w in q:
        w.response("skip")
    yield pump(0.3)
    check(replies == ["skip"], f"réponse transmise ({replies})")
    pw.set_visible(False)


@step
def application_par_defaut(win):
    """Ouvrir un fichier lance directement l'application par défaut de son type (comme Nautilus),
    sans passer par le choix « Ouvrir avec… » du portail."""
    marker = os.path.join(TMP, "lance.txt")
    apps = os.path.join(os.environ["XDG_DATA_HOME"], "applications")
    os.makedirs(apps, exist_ok=True)
    with open(os.path.join(apps, "visionneuse-test.desktop"), "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Visionneuse test\n"
                f"Exec=sh -c 'echo \"$1\" > {marker}' sh %f\nMimeType=image/png;\n")
    with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "mimeapps.list"), "w") as f:
        f.write("[Default Applications]\nimage/png=visionneuse-test.desktop\n")
    subprocess.run(["update-desktop-database", apps], capture_output=True)
    p = win.pane
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)
    before = list(Gtk.Window.list_toplevels())
    rec = [r for r in p.model.items if r.name == "image.png"][0]
    win._on_activated(p, rec)
    yield Wait(5, until=lambda: os.path.exists(marker))
    check(os.path.exists(marker) and open(marker).read().strip() == os.path.join(D, "image.png"),
          "double-clic : l'application par défaut est lancée directement")
    new = [w for w in Gtk.Window.list_toplevels() if w not in before and w.get_visible()]
    check(not new, f"pas de fenêtre « Ouvrir avec… » ({[type(w).__name__ for w in new]})")
    from odysseus.ui.dialogs import AppChooser
    ch = AppChooser(win, "image/png", [os.path.join(D, "image.png")])
    row = ch.default_row
    check(row.get_active() and not row.get_sensitive(),
          "« Ouvrir avec… » : application par défaut choisie → interrupteur allumé et grisé")
    others = [a for a in Gio.AppInfo.get_all() if a.get_id() != "visionneuse-test.desktop" and a.should_show()]
    if others:
        ch._selected(ch.chooser, others[0])
        check(not row.get_active() and row.get_sensitive(), "autre application → interrupteur éteint et actif")
    ch.destroy()
    from odysseus.ui.menus import context_menu
    p.model.select_names(["image.png"])
    m = context_menu(win, p, True)
    first = m.get_item_link(0, "section")
    labels = [first.get_item_attribute_value(i, "label").get_string() for i in range(first.get_n_items())]
    check(labels[:2] == [tr("Ouvrir avec {0}").format("Visionneuse test"), tr("Ouvrir avec…")],
          f"menu comme Nautilus : {labels[:2]}")


@step
def annuler_sans_ecraser(win):
    """Annuler ne doit jamais écraser ni emporter ce qui existait (retours de l'agent de test)."""
    from odysseus.backend import fileops
    base = os.path.join(D, "annulation")
    os.makedirs(os.path.join(base, "A", "sous"))
    os.makedirs(os.path.join(base, "B", "sous"))
    for path, text in ((os.path.join(base, "a.txt"), "original"), (os.path.join(base, "A", "sous", "nouveau.txt"), "n"),
                       (os.path.join(base, "B", "sous", "garde.txt"), "à garder")):
        with open(path, "w") as f:
            f.write(text)
    # renommage puis réutilisation de l'ancien nom
    target = fileops.rename(os.path.join(base, "a.txt"), "b.txt")
    win._push_undo(("rename", os.path.join(base, "a.txt"), target))
    with open(os.path.join(base, "a.txt"), "w") as f:
        f.write("créé entre-temps")
    win.undo()
    yield pump(0.4)
    _close_alerts()
    check(open(os.path.join(base, "a.txt")).read() == "créé entre-temps" and os.path.exists(target),
          "annuler un renommage n'écrase pas un fichier créé entre-temps")
    # copie avec fusion, puis annulation
    win._conflict_dialog = lambda c, reply: reply("merge", True)
    win.run_transfer([os.path.join(base, "A", "sous")], os.path.join(base, "B"))
    yield wait_jobs()
    check(os.path.exists(os.path.join(base, "B", "sous", "nouveau.txt")), "fusion faite")
    win.undo()
    yield wait_jobs()
    yield pump(0.4)
    check(os.path.exists(os.path.join(base, "B", "sous", "garde.txt")) and
          not os.path.exists(os.path.join(base, "B", "sous", "nouveau.txt")),
          "annuler une fusion ne retire que ce qui a été ajouté")
    del win._conflict_dialog


@step
def droits(win):
    """Propriétés : un lien ne change jamais les droits de sa cible ; le bit d'exécution d'une
    catégorie n'est pas ajouté en changeant l'accès d'une autre."""
    import stat
    from odysseus.ui.properties import PropertiesDialog
    base = os.path.join(D, "droits")
    os.makedirs(base)
    tgt = os.path.join(base, "secret.txt")
    with open(tgt, "w") as f:
        f.write("clé")
    os.chmod(tgt, 0o600)
    os.symlink(tgt, os.path.join(base, "lien"))
    script = os.path.join(base, "script.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(script, 0o744)
    d = PropertiesDialog(win, [os.path.join(base, "lien")])
    d.present(win)
    yield pump(0.8)
    for r in getattr(d, "_access_rows", []):
        r.set_selected(2)
    yield pump(0.3)
    check(stat.S_IMODE(os.stat(tgt).st_mode) == 0o600, "propriétés d'un lien : la cible garde ses droits")
    d.close()
    d = PropertiesDialog(win, [script])
    d.present(win)
    yield pump(0.8)
    d._access_rows[2].set_selected(0)
    yield pump(0.3)
    check(stat.S_IMODE(os.stat(script).st_mode) == 0o740,
          f"0744, « autres : aucun » → 0740 ({oct(stat.S_IMODE(os.stat(script).st_mode))})")
    d.close()
    # une seule catégorie change, les autres gardent leurs bits exacts (même « -wx », « -w- »)
    for name, is_dir, mode, row, value, expected in (("d754", True, 0o754, 0, 1, 0o554),
                                                       ("d733", True, 0o733, 1, 0, 0o703),
                                                       ("f620", False, 0o620, 2, 1, 0o624)):
        path = os.path.join(base, name)
        if is_dir:
            os.makedirs(path)
        else:
            open(path, "w").close()
        os.chmod(path, mode)
        d = PropertiesDialog(win, [path])
        d.present(win)
        yield pump(0.5)
        d._access_rows[row].set_selected(value)
        yield pump(0.2)
        got = stat.S_IMODE(os.stat(path).st_mode)
        check(got == expected, f"{oct(mode)}, ligne {row} → {value} : {oct(got)} (attendu {oct(expected)})")
        d.close()
    d = PropertiesDialog(win, [script])
    d.present(win)
    yield pump(0.5)
    d.exec_row.set_active(False)
    yield pump(0.2)
    check(stat.S_IMODE(os.stat(script).st_mode) == 0o640, f"exécution retirée : 0640 ({oct(os.stat(script).st_mode & 0o777)})")
    d.close()


@step
def noms_non_utf8(win):
    """Noms de fichiers non UTF-8 (anciennes clés, archives Windows) : affichés avec « � », sans
    exception (avant : lignes vides, titre figé, propriétés et copie impossibles)."""
    base = os.path.join(D, "non-utf8")
    os.makedirs(base)
    bad_dir = os.path.join(os.fsencode(base), b"dossier-\xe9t\xe9")
    os.makedirs(bad_dir)
    with open(os.path.join(bad_dir, b"f\xe9.txt"), "w") as f:
        f.write("x")
    p = win.pane
    p.open(os.fsdecode(bad_dir))
    yield Wait(5, until=lambda: not p.model.loading)
    names = [r.name for r in p.model.items]
    check(len(names) == 1, f"dossier non UTF-8 lu ({len(names)} élément)")
    p.model.select_names(names)
    yield pump(0.3)
    win.activate_action("win.copy", None)
    win.activate_action("win.properties", None)
    yield pump(0.6)
    for w in Gtk.Window.list_toplevels():
        if type(w).__name__ == "PropertiesDialog":
            w.close()
    check("�" in win.get_title(), f"titre affichable ({win.get_title()!r})")
    # F2 puis « Renommer » sans rien changer : le champ montre « f�.txt », le vrai nom doit rester
    win.activate_action("win.rename", None)
    yield pump(0.4)
    for w in Gtk.Window.list_toplevels():
        if isinstance(w, Adw.MessageDialog) and w.get_heading() == tr("Renommer"):
            w.response("ok")
    yield pump(0.4)
    check(os.listdir(bad_dir) == [b"f\xe9.txt"], f"renommer sans changement garde le vrai nom ({os.listdir(bad_dir)})")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def precedent_avec_focus_texte(win):
    """Le renvoi des touches au champ de saisie se fait au clavier seulement : avec le focus dans la
    recherche, l'action « Précédent » (bouton, souris) navigue toujours."""
    p = win.pane
    p.open(os.path.join(D, "dossier A"))
    yield Wait(5, until=lambda: not p.model.loading)
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)
    win.activate_action("win.search", None)
    yield pump(0.3)
    win.search_entry.grab_focus()
    win.activate_action("win.back", None)
    yield pump(0.6)
    check(p.location == os.path.join(D, "dossier A"), f"Précédent navigue malgré le focus texte ({p.location})")
    win.search_entry.set_text("")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def panneau_suit_le_dossier(win):
    """Un panneau ouvert dans un dossier renommé (par Odysseus) le suit ; mis à la corbeille, il remonte."""
    base = os.path.join(D, "suivre")
    os.makedirs(os.path.join(base, "Projet", "src"))
    os.makedirs(os.path.join(base, "Vieux"))
    p = win.pane
    p.open(base)
    yield Wait(5, until=lambda: not p.model.loading)
    win.activate_action("win.dual", None)
    yield pump(0.6)
    other = win.workspace.other(p)
    other.open(os.path.join(base, "Projet", "src"))
    yield pump(0.6)
    p.model.select_names(["Projet"])
    real = win.ask_name
    win.ask_name = lambda heading, initial, action, callback, *a, **k: callback("Projet 2026")
    win.activate_action("win.rename", None)
    win.ask_name = real
    yield pump(0.8)
    check(other.location == os.path.join(base, "Projet 2026", "src"), f"renommage suivi ({other.location})")
    win.undo()
    yield pump(0.6)
    check(other.location == os.path.join(base, "Projet", "src"), f"annuler le renommage : suivi ({other.location})")
    win.redo()
    yield pump(0.6)
    check(other.location == os.path.join(base, "Projet 2026", "src"), f"rétablir le renommage : suivi ({other.location})")
    other.open(os.path.join(base, "Vieux"))
    yield pump(0.6)
    win._trash_paths([os.path.join(base, "Vieux")])
    yield wait_jobs()
    yield pump(1.0)
    check(other.location == base, f"dossier mis à la corbeille : le panneau remonte ({other.location})")
    win.activate_action("win.dual", None)
    yield pump(0.4)
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def nouveau_dossier_depuis_le_debut(win):
    """Ouvrir un dossier après avoir défilé dans un autre : la vue repart du début (Gtk.ListView
    gardait la position de défilement du dossier précédent)."""
    base = os.path.join(D, "defiler")
    for sub in ("A", "B"):
        os.makedirs(os.path.join(base, sub))
        for i in range(300):
            open(os.path.join(base, sub, f"f{i:03d}.txt"), "w").close()
    p = win.pane
    p.open(os.path.join(base, "A"))
    yield Wait(5, until=lambda: not p.model.loading)
    yield pump(0.5)
    adj = p.list_view.get_vadjustment()
    adj.set_value(adj.get_upper())
    yield pump(0.3)
    check(adj.get_value() > 0, "défilé vers le bas")
    p.open(os.path.join(base, "B"))
    yield Wait(5, until=lambda: not p.model.loading)
    yield pump(0.5)
    check(p.list_view.get_vadjustment().get_value() == 0,
          f"nouveau dossier affiché depuis le début ({p.list_view.get_vadjustment().get_value()})")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def executer_comme_programme(win):
    """Script exécutable : « Exécuter comme un programme » dans le menu, lancé dans un terminal depuis
    son dossier ; pas pour un fichier non exécutable."""
    import odysseus.ui.actions_files as af
    from odysseus.ui import menus
    base = os.path.join(D, "scripts")
    os.makedirs(base)
    script = os.path.join(base, "mon script.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\necho bonjour\n")
    os.chmod(script, 0o755)
    with open(os.path.join(base, "notes.sh"), "w") as f:
        f.write("#!/bin/sh\n")
    p = win.pane
    p.open(base)
    yield Wait(5, until=lambda: not p.model.loading)

    def labels():
        m = menus.context_menu(win, p, True)
        out = []
        def walk(model):
            for i in range(model.get_n_items()):
                lbl = model.get_item_attribute_value(i, "label", None)
                if lbl is not None:
                    out.append(lbl.unpack())
                for link in ("section", "submenu"):
                    sub = model.get_item_link(i, link)
                    if sub is not None:
                        walk(sub)
        walk(m)
        return out
    p.model.select_names(["mon script.sh"])
    yield pump(0.2)
    check(tr("Exécuter comme un programme") in labels(), "menu : « Exécuter comme un programme » (script exécutable)")
    launched = []
    real_popen, real_find = af.subprocess.Popen, af.find_terminal
    af.subprocess.Popen = lambda argv, **kw: launched.append((argv, kw))
    af.find_terminal = lambda: "gnome-terminal"
    try:
        win.activate_action("win.run-program", None)
    finally:
        af.subprocess.Popen, af.find_terminal = real_popen, real_find
    check(launched and launched[0][0][:2] == ["gnome-terminal", "--"] and script in launched[0][0]
          and launched[0][1].get("cwd") == base, f"lancé dans un terminal depuis son dossier ({launched})")
    p.model.select_names(["notes.sh"])
    yield pump(0.2)
    check(tr("Exécuter comme un programme") not in labels(), "pas pour un script non exécutable")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def menu_barre_laterale(win):
    """Menu contextuel de la barre latérale : les entrées n'étaient pas grisées par erreur (actions
    posées sur la liste, menu attaché à la barre) ; ouvrir dans un onglet / une nouvelle fenêtre."""
    from odysseus.ui.sidebar import SidebarRow
    sb = win.sidebar
    row = sb.places_list.get_first_child()
    while row is not None and not (isinstance(row, SidebarRow) and row.target and "://" not in row.target):
        row = row.get_next_sibling()

    class Fake:
        def set_state(self, _s):
            pass
    ok, pt = row.compute_point(sb.places_list, Graphene.Point().init(5, 5))
    sb._on_menu(Fake(), 1, pt.x, pt.y, sb.places_list)
    yield pump(0.3)
    pages = win.tab_view.get_n_pages()
    check(sb.activate_action("side.tab", None), "barre latérale : « Ouvrir dans un nouvel onglet » actif")
    yield pump(0.4)
    check(win.tab_view.get_n_pages() == pages + 1, "nouvel onglet ouvert depuis la barre latérale")
    win.activate_action("win.close-tab", None)
    nwin = len(win.get_application().get_windows())
    check(sb.activate_action("side.window", None), "barre latérale : « Ouvrir dans une nouvelle fenêtre » actif")
    yield pump(0.6)
    wins = win.get_application().get_windows()
    check(len(wins) == nwin + 1, "nouvelle fenêtre ouverte")
    for w in wins:
        if w is not win and type(w).__name__ == "MainWindow":
            w.destroy()
    for w in Gtk.Window.list_toplevels():
        if isinstance(w, Gtk.PopoverMenu):
            w.popdown()
    yield pump(0.3)
    check(win.lookup_action("open-window") is not None, "menu des dossiers : « Ouvrir dans une nouvelle fenêtre »")
    if sb.recent_row is None or getattr(sb, "_privacy", None) is None:
        print("- « Récents » ou réglage de GNOME indisponible : test sauté")
    else:
        priv = sb._privacy
        was = priv.get_boolean("remember-recent-files")
        priv.set_boolean("remember-recent-files", False)
        yield pump(0.3)
        check(not sb.recent_row.get_visible(), "« Récents » masqué quand l'historique des fichiers est désactivé")
        priv.set_boolean("remember-recent-files", True)
        yield pump(0.3)
        check(sb.recent_row.get_visible(), "« Récents » réaffiché avec l'historique")
        priv.set_boolean("remember-recent-files", was)


@step
def courriel_et_telephone(win):
    """« Courriel… » (xdg-email avec pièces jointes) pour des fichiers ; « Envoyer vers <téléphone> »
    avec GSConnect (faux service D-Bus), comme l'extension de GSConnect pour Nautilus."""
    import odysseus.ui.actions_files as af
    from odysseus.backend import gsconnect
    from odysseus.ui import menus
    base = os.path.join(D, "envoi")
    os.makedirs(os.path.join(base, "dossier"))
    for n in ("photo.jpg", "notes.txt"):
        with open(os.path.join(base, n), "w") as f:
            f.write("x")
    p = win.pane
    p.open(base)
    yield Wait(5, until=lambda: not p.model.loading)

    def labels():
        out = []

        def walk(model):
            for i in range(model.get_n_items()):
                lbl = model.get_item_attribute_value(i, "label", None)
                if lbl is not None:
                    out.append(lbl.unpack())
                for link in ("section", "submenu"):
                    sub = model.get_item_link(i, link)
                    if sub is not None:
                        walk(sub)
        walk(menus.context_menu(win, p, True))
        return out
    p.model.select_names(["photo.jpg", "notes.txt"])
    yield pump(0.2)
    check(tr("Courriel…") in labels(), "menu : « Courriel… » pour des fichiers")
    launched = []
    real, real_which = af.subprocess.Popen, af.shutil.which
    af.subprocess.Popen = lambda argv, **kw: launched.append(argv)
    af.shutil.which = lambda name: "/usr/bin/" + name      # xdg-email absent du bac à sable
    try:
        win.activate_action("win.email", None)
    finally:
        af.subprocess.Popen, af.shutil.which = real, real_which
    check(launched and launched[0][0] == "xdg-email" and launched[0].count("--attach") == 2,
          f"courriel : xdg-email avec les deux pièces jointes ({launched})")
    p.model.select_names(["dossier"])
    yield pump(0.2)
    check(tr("Courriel…") not in labels(), "pas de « Courriel… » pour un dossier")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        print("- pas de bus de session : test GSConnect sauté")
    else:
        journal = os.path.join(TMP, "gsconnect.log")
        fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_gsconnect.py"), journal])
        try:
            yield Wait(8, until=lambda: bool(gsconnect.devices.capable()))
            check(gsconnect.devices.capable() == [(gsconnect.SERVICE_PATH + "/Device/pixeltest", "Pixel test")],
                  f"appareil GSConnect détecté en direct ({gsconnect.devices.capable()})")
            p.model.select_names(["photo.jpg"])
            yield pump(0.2)
            check(tr("Envoyer vers {0}").format("Pixel test") in labels(), "menu : « Envoyer vers Pixel test »")
            win.activate_action("win.send-to-device",
                                GLib.Variant.new_string(gsconnect.SERVICE_PATH + "/Device/pixeltest"))
            yield Wait(5, until=lambda: os.path.exists(journal))
            yield pump(0.3)
            got = open(journal).read().split() if os.path.exists(journal) else []
            check(got == [Gio.File.new_for_path(os.path.join(base, "photo.jpg")).get_uri()],
                  f"fichier envoyé au téléphone ({got})")
            features_mod = __import__("odysseus.features", fromlist=["x"])
            features_mod.set_enabled("gsconnect", False)
            check(tr("Envoyer vers {0}").format("Pixel test") not in labels(), "GSConnect désactivable à chaud")
            features_mod.set_enabled("gsconnect", True)
        finally:
            fake.terminate()
        yield Wait(5, until=lambda: not gsconnect.devices.capable())
        check(not gsconnect.devices.capable(), "téléphone parti : plus proposé")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def chemin_depuis_le_disque(win):
    """Comme Nautilus : un disque monté commence la barre de chemin (son nom, son icône), au lieu de
    « Zorin OS / mnt / Stockage_Linux »."""
    from odysseus.ui import pathbar
    disk = os.path.join(D, "mnt", "Stockage_Linux")
    os.makedirs(os.path.join(disk, "Images"))
    real = pathbar.mount_roots
    pathbar.mount_roots = lambda: {disk: ("Stockage_Linux", "drive-harddisk-symbolic")}
    try:
        segs = win.pathbar._segments(os.path.join(disk, "Images"), "")
    finally:
        pathbar.mount_roots = real
    check([(lbl, tgt) for lbl, tgt, _i in segs] == [("Stockage_Linux", disk), ("Images", os.path.join(disk, "Images"))],
          f"chemin d'un disque monté : Stockage_Linux / Images ({[s[0] for s in segs]})")
    segs = win.pathbar._segments(os.path.join(D, "mnt"), "")
    check(segs[0][1] in ("/", os.path.expanduser("~")), "hors disque monté : chemin habituel")
    yield pump(0.1)


@step
def focus_une_seule_fois(win):
    """GLib.idle_add(champ.grab_focus) rappelait grab_focus sans fin (il renvoie True) : le texte du
    champ était resélectionné en boucle et chaque lettre remplaçait la précédente (annoter, texte)."""
    from odysseus.ui.common import focus_later
    e = Gtk.Entry()
    calls = []
    e.grab_focus = lambda: (calls.append(1), True)[1]
    focus_later(e)
    yield pump(0.5)
    check(len(calls) == 1, f"focus donné une seule fois ({len(calls)} appels)")


@step
def fichier_cache_choisi(win):
    """Ouvrir le dossier d'un fichier caché pour le sélectionner (ShowItems) : il est montré et choisi ;
    le dossier suivant reprend la préférence (cachés masqués)."""
    base = os.path.join(D, "caches-choisis")
    os.makedirs(base)
    open(os.path.join(base, ".secret"), "w").close()
    open(os.path.join(base, "visible"), "w").close()
    p = win.pane
    p.open(base, select=[".secret"])
    yield Wait(5, until=lambda: not p.model.loading)
    yield pump(0.3)
    check([r.name for r in p.model.selected_recs()] == [".secret"], "fichier caché demandé : montré et sélectionné")
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)
    check(not any(r.name.startswith(".") for r in p.model.items), "dossier suivant : cachés masqués")


@step
def corbeille_retablir(win):
    """Annuler une mise à la corbeille alors qu'un nouveau fichier a pris le nom (l'original revient en
    « a (2).txt »), puis Rétablir : c'est l'original qui repart, jamais le nouveau."""
    from odysseus.backend import fileops
    base = os.path.join(D, "retablir")
    os.makedirs(base)
    a = os.path.join(base, "a.txt")
    with open(a, "w") as f:
        f.write("original")
    win._trash_paths([a])
    yield wait_jobs()
    if not any(i.get_attribute_byte_string("trash::orig-path") == a for i, _f in fileops.trash_items(None)):
        print("- trash:/// ne voit pas la corbeille de test (ODYSSEUS_UI_GVFS=1 sous dbus-run-session) : test sauté")
        return
    with open(a, "w") as f:
        f.write("nouveau")
    win.undo()
    yield wait_jobs()
    yield pump(0.5)
    _close_alerts()
    check(sorted(os.listdir(base)) == ["a (2).txt", "a.txt"], f"annuler : original à côté ({os.listdir(base)})")
    win.redo()
    yield wait_jobs()
    yield pump(0.5)
    check(os.listdir(base) == ["a.txt"] and open(a).read() == "nouveau",
          f"rétablir renvoie l'original à la corbeille, pas le nouveau ({os.listdir(base)})")


@step
def corbeille_relance_gvfs(win):
    """Si trash:/// montre moins d'éléments que les corbeilles n'en contiennent (gvfsd-trash a perdu
    un disque), Odysseus relance gvfsd-trash puis relit la corbeille ; sinon, rien."""
    from odysseus.backend import trashwatch
    calls = []
    real_count, real_restart = trashwatch.expected_count, trashwatch.restart_gvfs_trash
    trashwatch.restart_gvfs_trash = lambda: (calls.append(1), True)[1]
    p = win.pane
    try:
        trashwatch.expected_count = lambda: 0
        p.open("trash:///")
        yield Wait(5, until=lambda: not p.model.loading)
        if p.model.error:       # pas de GVfs (session sans gvfsd) : trash:/// illisible
            print("- corbeille illisible sans GVfs : test de relance sauté")
        else:
            yield pump(0.6)
            check(calls == [], "corbeille cohérente : aucune relance")
            trashwatch.expected_count = lambda: 10 ** 6
            p.refresh()
            yield Wait(5, until=lambda: not p.model.loading)
            yield pump(0.6)
            check(len(calls) == 1, f"éléments manquants : gvfsd-trash relancé ({len(calls)})")
    finally:
        trashwatch.expected_count, trashwatch.restart_gvfs_trash = real_count, real_restart
    yield pump(2.0)
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


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
if GVFSD is not None:
    GVFSD.terminate()
shutil.rmtree(TMP, ignore_errors=True)
print("\n".join(errors) if errors else "\nAucune erreur.")
sys.exit(1 if errors else 0)
