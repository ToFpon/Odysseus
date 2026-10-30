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
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

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


# Les « Gtk-CRITICAL » (widget ajouté deux fois…) ne lèvent pas d'exception Python : on relit
# notre propre sortie d'erreur pour les compter comme des échecs.
CRITICALS = []
_err_r, _err_w = os.pipe()
_err_orig = os.dup(2)
os.dup2(_err_w, 2)


def _tee_stderr():
    with os.fdopen(_err_r, "rb", 0) as f:
        for line in iter(f.readline, b""):
            os.write(_err_orig, line)
            if b"-CRITICAL **" in line:
                CRITICALS.append(line.decode("utf-8", "replace").strip())


threading.Thread(target=_tee_stderr, daemon=True).start()


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
    p = win.pane
    # attendre la fin du chargement, pas un délai fixe : machine chargée, la liste était encore vide
    yield Wait(10, until=lambda: not p.model.loading and len(p.model.items) > 0)
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
    m = win.pane.model
    check(m.sort_key == "size" and m.sort_reverse, "tri par clic sur l'en-tête")
    lv.view.sort_by_column(lv.columns["name"], Gtk.SortType.ASCENDING)
    yield pump(0.3)
    check(m.sort_key == "name" and not m.sort_reverse
          and win.pane.model.items[0].name == "dossier A", "retour au tri par nom par l'en-tête")


@step
def tri_par_dossier(win):
    """Chaque dossier garde son tri, comme dans Nautilus (mêmes métadonnées GVfs)."""
    from odysseus.backend import folder_sort
    p = win.pane
    root = p.location
    sub = os.path.join(root, "dossier A")
    win.activate_action("win.sort-by", GLib.Variant.new_string("size"))
    win.activate_action("win.sort-reverse", None)
    yield pump(0.3)
    check(folder_sort.get(root) == ("size", True), f"tri enregistré pour le dossier ({folder_sort.get(root)})")
    check((settings["sort_key"], settings["sort_reverse"]) == ("name", False), "tri par défaut inchangé")
    by = folder_sort._read_gvfs(root)[0]
    check(by == "size" or folder_sort._fb().get(root, {}).get("key") == "size",
          f"tri écrit pour Nautilus ou dans sort.json ({by})")
    p.open(sub)
    yield pump(0.5)
    check((p.model.sort_key, p.model.sort_reverse) == ("name", False), "autre dossier : tri par défaut")
    check(win.lookup_action("sort-by").get_state().get_string() == "name"
          and not win.lookup_action("sort-reverse").get_state().get_boolean(), "menu Trier : tri du dossier affiché")
    settings.data["sort_key"] = "date"
    settings["sort_reverse"] = True                 # Préférences → Tri par défaut : « Dernière modification »
    yield pump(0.3)
    check((p.model.sort_key, p.model.sort_reverse) == ("date", True), "tri par défaut suivi par un dossier sans tri")
    p.go_back()
    yield pump(0.5)
    check((p.model.sort_key, p.model.sort_reverse) == ("size", True)
          and win.lookup_action("sort-by").get_state().get_string() == "size", "retour : le dossier retrouve son tri")
    names = [r.name for r in p.model.items if not r.is_dir]
    sizes = [r.size for r in p.model.items if not r.is_dir]
    check(sizes == sorted(sizes, reverse=True), f"fichiers du plus gros au plus petit ({names[:4]})")
    # menu contextuel du fond : sous-menu « Trier »
    from odysseus.ui import menus
    m = menus._background_menu(win, p)
    labels = []
    for i in range(m.get_n_items()):
        sec = m.get_item_link(i, "section")
        for j in range(sec.get_n_items() if sec else 0):
            if sec.get_item_link(j, "submenu") is not None:
                labels.append(sec.get_item_attribute_value(j, "label", None).get_string())
    check(tr("Trier") in labels, f"sous-menu Trier dans le menu du fond ({labels})")
    win.activate_action("win.sort-by", GLib.Variant.new_string("name"))
    win.activate_action("win.sort-reverse", None)
    settings.data["sort_key"] = "name"
    settings["sort_reverse"] = False
    yield pump(0.3)
    check((p.model.sort_key, p.model.sort_reverse) == ("name", False), "tri par nom rétabli")


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
    ob = win.ops_btn
    yield pump(0.3)
    check(not ob.get_visible() or (ob.done_icon.get_visible() and ob.done_icon.get_icon_name() == "object-select-symbolic"),
          f"bouton des opérations : icône « terminé » une fois fini, jamais vide ({ob.get_visible()}, {ob.done_icon.get_icon_name()})")
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
    check(p.pages["other-locations:///"].dev_list.get_first_child() is not None, "Autres emplacements : disques listés")
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
def fenetre_archive(win):
    """Double-clic sur une archive : sa petite fenêtre (comme Archive Browser) — arbre de l'archive à
    gauche, dossier à droite ; glisser (préparé d'avance), extraire vers le dossier de droite."""
    from odysseus.ui.archive_window import ArchiveWindow
    app = win.get_application()
    p = win.pane
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)
    rec = [r for r in p.model.items if r.name == "paquet.zip"][0]
    win._on_activated(p, rec)
    yield Wait(5, until=lambda: any(isinstance(w, ArchiveWindow) for w in app.get_windows()))
    aws = [w for w in app.get_windows() if isinstance(w, ArchiveWindow)]
    check(len(aws) == 1 and p.mode == "fs" and p.location == D, "archive dans sa propre fenêtre, dossier gardé")
    if not aws:
        return
    aw = aws[0]
    yield Wait(5, until=lambda: aw.listing is not None)
    names = [aw._entry_at(i).name for i in range(aw.selection.get_n_items())]
    check(names == ["dossier A"] and aw.folder.path == D, f"arbre de l'archive, dossier de l'archive à droite ({names})")
    aw._on_activate(aw.view, 0)                       # double-clic sur un dossier : déplié sur place
    yield pump(0.3)
    names = [aw._entry_at(i).name for i in range(aw.selection.get_n_items())]
    check(names == ["dossier A", "sous"], f"dossier déplié dans l'arbre ({names})")
    aw.filter.set_text("profond")
    yield pump(0.5)
    found = [aw._entry_at(i).path for i in range(aw.selection.get_n_items())]
    check(found == ["dossier A/sous/profond.md"], f"filtre : chemin complet du résultat ({found})")
    aw.filter.set_text("")
    yield pump(0.5)
    win._on_activated(p, rec)
    yield pump(0.3)
    check(len([w for w in app.get_windows() if isinstance(w, ArchiveWindow)]) == 1,
          "même archive : la fenêtre existante revient devant")
    aw.selection.unselect_all()
    aw.select_paths(["dossier A"])
    yield Wait(8, until=lambda: aw.drag.cached("dossier A") is not None)
    cached = aw.drag.cached("dossier A")
    check(cached is not None and os.path.exists(os.path.join(cached, "sous", "profond.md")),
          "sélection extraite d'avance pour le glisser")
    paths = aw.drag.files_for(aw._items())
    check(paths == [cached], f"glisser : fichiers prêts ({paths})")
    dest = tempfile.mkdtemp(dir=D, prefix="depot ")
    aw.copy_into(paths, dest)                        # déposé dans le dossier de droite
    yield wait_jobs()
    yield pump(0.3)
    check(os.path.exists(os.path.join(dest, "dossier A", "sous", "profond.md")), "déposé dans un dossier")
    aw.folder.navigate(dest)
    yield pump(0.6)
    check(any(it.rec.name == "dossier A" for it in aw.folder.store), "dossier de droite suivi après la copie")
    aw.selection.unselect_all()
    aw.select_paths(["dossier A/sous/profond.md"])
    aw.extract(selection=True)
    yield wait_jobs()
    yield pump(0.3)
    check(os.path.isfile(os.path.join(dest, "profond.md")), f"extraire la sélection à droite ({os.listdir(dest)})")
    dest2 = tempfile.mkdtemp(dir=D, prefix="tout ")
    aw.folder.navigate(dest2)
    aw.extract(selection=False)
    yield wait_jobs()
    yield pump(0.3)
    got = [os.path.relpath(os.path.join(r, f), dest2) for r, _d, fs in os.walk(dest2) for f in fs]
    check(any(g.endswith(os.path.join("sous", "profond.md")) for g in got), f"tout extraire à droite ({got})")
    root = aw.drag.root
    aw.close()
    yield Wait(3, until=lambda: aw not in app.get_windows())
    check(aw not in app.get_windows() and os.path.exists(cached),
          "fenêtre fermée ; fichiers glissés gardés (une copie peut encore les lire)")
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    settings["archive_window"] = False
    win._on_activated(p, rec)
    yield Wait(5, until=lambda: p.mode == "archive")
    check(p.mode == "archive", "réglage coupé : l'archive s'ouvre dans le panneau")
    settings["archive_window"] = True
    win.activate_action("win.close-archive", None)
    yield pump(0.5)


@step
def emblemes_droits(win):
    """Comme Nautilus : cadenas (non modifiable), croix (non lisible), lien — plusieurs à la suite,
    après le nom. Le bac à sable tourne en root (qui a tous les droits) : on se fait passer pour
    l'utilisateur 1000."""
    from odysseus.ui import icons
    folder = tempfile.mkdtemp(dir=D, prefix="droits ")
    for name, mode, owner in (("a moi.txt", 0o644, 1000), ("systeme", 0o755, 0), ("lost+found", 0o700, 0)):
        path = os.path.join(folder, name)
        if name.endswith(".txt"):
            open(path, "w").close()
        else:
            os.mkdir(path)
        os.chmod(path, mode)
        os.chown(path, owner, owner)
    os.symlink("systeme", os.path.join(folder, "lien"))
    os.chown(folder, 1000, 1000)             # dossier à l'utilisateur : comme Stockage_Linux
    # dans un dossier de root (comme /) : pas de cadenas, seulement la croix sur l'illisible
    for name, mode in (("usr", 0o755), ("secret", 0o700)):
        os.mkdir(os.path.join(folder, "systeme", name))
        os.chmod(os.path.join(folder, "systeme", name), mode)
    saved = icons._ids
    icons._ids = (1000, {1000})
    icons._parent_cache.clear()
    try:
        p = win.pane
        p.open(folder)
        yield Wait(5, until=lambda: not p.model.loading)
        yield pump(0.5)
        shown_ems = {}

        def walk(w):
            if w.has_css_class("name-cell") and getattr(w, "_rec", None) is not None:
                box = w.get_last_child()
                names, c = [], box.get_first_child()
                while c is not None:
                    if c.get_visible():
                        names.append(c.get_icon_name())
                    c = c.get_next_sibling()
                shown_ems[w._rec.name] = names if box.get_visible() else []
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(p.list_view.view)
        expected = {"a moi.txt": [], "systeme": ["emblem-readonly"],
                    "lost+found": ["emblem-readonly", "emblem-unreadable"],
                    "lien": ["emblem-symbolic-link", "emblem-readonly"]}
        check(shown_ems == expected, f"emblèmes des droits comme Nautilus ({shown_ems})")
        p.open(os.path.join(folder, "systeme"))
        yield Wait(5, until=lambda: not p.model.loading)
        yield pump(0.5)
        shown_ems.clear()
        walk(p.list_view.view)
        check(shown_ems == {"usr": [], "secret": ["emblem-unreadable"]},
              f"dossier de root : pas de cadenas, croix sur l'illisible ({shown_ems})")
    finally:
        icons._ids = saved
        icons._parent_cache.clear()


@step
def sommes_de_controle(win):
    """Fenêtre « Empreintes » (clic droit) : empreintes calculées en une lecture ; coller l'empreinte
    publiée → verdict automatique sur la pastille ; clic droit sur SHA256SUMS → « Vérifier les
    sommes de contrôle… » et fenêtre de résultats."""
    import hashlib
    from odysseus.ui import menus
    folder = tempfile.mkdtemp(dir=D, prefix="empreintes ")
    iso = os.path.join(folder, "zorin.iso")
    with open(iso, "wb") as f:
        f.write(os.urandom(2 << 20))
    data = open(iso, "rb").read()
    sha, md5 = hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()
    p = win.pane
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    p.model.select_names(["zorin.iso"])
    yield pump(0.2)
    m = menus.context_menu(win, p, True)
    actions = [m.get_item_link(i, "section").get_item_attribute_value(j, "action").unpack()
               for i in range(m.get_n_items())
               for j in range(m.get_item_link(i, "section").get_n_items())
               if m.get_item_link(i, "section").get_item_attribute_value(j, "action") is not None]
    check("win.checksums" in actions, "menu : « Empreintes et comparaison… » sur un fichier")
    before = list(Gtk.Window.list_toplevels())
    win.activate_action("win.checksums", None)
    yield pump(0.2)
    cw = [w for w in Gtk.Window.list_toplevels() if w not in before and type(w).__name__ == "ChecksumWindow"]
    check(len(cw) == 1 and not cw[0].get_modal(), "fenêtre « Empreintes » à part, non modale")
    w = cw[0]
    # collée avant la fin du calcul : pastille grise, puis verdict quand les empreintes arrivent
    w.entry.set_text(f"SHA256 (zorin.iso) = {sha}")
    yield Wait(10, until=lambda: w.verdict is True)
    check(w.verdict is True and w.badge.has_css_class("ok") and w.rows["SHA-256"].has_css_class("success"),
          f"empreinte publiée collée : verdict vert sans clic ({w.result.get_title()}, {w.badge.get_css_classes()})")
    check(w.digests.get("MD5") == md5 and w.rows["MD5"].get_subtitle() == md5,
          "empreintes MD5 / SHA-1 / SHA-256 / SHA-512 affichées")
    check(bool(w.sums.get_description()) and "…" not in w.sums.get_description(),
          f"texte d'aide mis à jour après le calcul ({w.sums.get_description()})")
    w.entry.emit("changed")
    yield pump(0.4)
    check(w.verdict is True and w.badge.has_css_class("ok"), "verdict gardé après un « changed » sans changement")
    w.entry.set_text(md5.upper())
    yield Wait(2, until=lambda: w.verdict is True and w.rows["MD5"].has_css_class("success"))
    check(w.rows["MD5"].has_css_class("success") and not w.rows["SHA-256"].has_css_class("success"),
          "MD5 en majuscules reconnu, ligne MD5 mise en avant")
    # 64 caractères faux : SHA3-256 et BLAKE2s sont calculés en plus avant de conclure
    w.entry.set_text("0" * 64)
    yield Wait(10, until=lambda: w.verdict is False)
    check(w.verdict is False and w.badge.has_css_class("ko") and "SHA3-256" in w.digests,
          f"empreinte fausse : verdict rouge ({w.result.get_title()})")
    w.entry.set_text("bonjour")
    yield pump(0.4)
    check(w.verdict == "unknown" and w.badge.has_css_class("unknown"), "empreinte non reconnue : pastille orange")
    w.entry.set_text("")
    yield pump(0.4)
    check(w.verdict is None and not w.result.get_visible(), "champ vidé : verdict effacé")
    w.close()
    yield pump(0.3)
    with open(os.path.join(folder, "SHA256SUMS"), "w") as f:
        f.write(f"{sha}  zorin.iso\n{'0' * 64}  absent.iso\n")
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    p.model.select_names(["SHA256SUMS"])
    yield pump(0.2)
    m = menus.context_menu(win, p, True)
    actions = [m.get_item_link(i, "section").get_item_attribute_value(j, "action").unpack()
               for i in range(m.get_n_items())
               for j in range(m.get_item_link(i, "section").get_n_items())
               if m.get_item_link(i, "section").get_item_attribute_value(j, "action") is not None]
    check("win.verify-checksums" in actions, "menu : « Vérifier les sommes de contrôle… » sur SHA256SUMS")
    before = list(Gtk.Window.list_toplevels())
    win.activate_action("win.verify-checksums", None)
    yield wait_jobs()
    yield pump(0.5)
    res = [w for w in Gtk.Window.list_toplevels() if w not in before and type(w).__name__ == "ChecksumResults"]
    check(len(res) == 1 and [r["status"] for r in res[0].results] == ["ok", "missing"],
          f"vérification : fenêtre de résultats ({[r['status'] for r in res[0].results] if res else None})")
    for w in res:
        w.close()
    from odysseus import features
    features.set_enabled("checksums", False)
    m = menus.context_menu(win, p, True)
    actions = [m.get_item_link(i, "section").get_item_attribute_value(j, "action").unpack()
               for i in range(m.get_n_items())
               for j in range(m.get_item_link(i, "section").get_n_items())
               if m.get_item_link(i, "section").get_item_attribute_value(j, "action") is not None]
    check("win.verify-checksums" not in actions and "win.checksums" not in actions, "sommes de contrôle désactivables")
    features.set_enabled("checksums", True)
    yield pump(0.2)


@step
def quitter_double_panneau_focus(win):
    """Sortie du double panneau, focus dans la recherche alors qu'un panneau l'avait avant
    (« Error finding last focus widget of GtkPaned ») : le focus reste dans le champ, sans
    resélectionner le texte ; focus dans le panneau retiré → rendu au panneau gardé."""
    p = win.pane
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)
    if not win.is_dual():
        win.activate_action("win.dual", None)
        yield pump(0.5)
    win.workspace.other(win.pane).focus_view()
    yield pump(0.2)
    win.search_bar.set_search_mode(True)
    win.search_entry.set_text("abc")
    win.search_entry.grab_focus()
    win.search_entry.select_region(3, 3)
    yield pump(0.3)
    win.activate_action("win.dual", None)
    yield pump(0.5)
    focus = win.get_focus()
    check(not win.is_dual() and focus is not None and focus.is_ancestor(win.search_entry),
          f"sortie du double panneau : focus gardé dans la recherche ({type(focus).__name__})")
    bounds = win.search_entry.get_selection_bounds()
    check(not bounds or bounds[0] == bounds[1], f"texte de la recherche non resélectionné ({bounds})")
    win.search_entry.set_text("")
    win.search_bar.set_search_mode(False)
    yield pump(0.3)
    win.activate_action("win.dual", None)
    yield pump(0.5)
    other = win.workspace.other(win.pane)
    other.focus_view()
    win.workspace.set_active(win.pane)
    yield pump(0.2)
    win.activate_action("win.dual", None)
    yield pump(0.5)
    focus = win.get_focus()
    check(focus is not None and focus.is_ancestor(win.pane), "focus du panneau retiré rendu au panneau gardé")


@step
def recherche_comme_nautilus(win):
    """Signalé par un utilisateur : taper « u » dans / doit sélectionner usr (le meilleur résultat),
    Entrée l'ouvre, et la recherche se termine (avant : le filtre « u » restait sur /usr, où seul
    « include » s'affichait). Dans le dossier, puis dans les sous-dossiers."""
    root = tempfile.mkdtemp(dir=D, prefix="racine ")
    for d in ("usr", "run", "bin.usr-is-merged", "lib.usr-is-merged", "etc"):
        os.makedirs(os.path.join(root, d))
    usr_children = ("bin", "games", "include", "lib", "share", "src")
    for d in usr_children:
        os.makedirs(os.path.join(root, "usr", d))
    p = win.pane
    for recursive in (False, True):
        p.open(root)
        yield Wait(5, until=lambda: not p.model.loading)
        win.recursive_btn.set_active(recursive)
        win.search_bar.set_search_mode(True)
        yield pump(0.2)
        win.search_entry.set_text("u")
        yield Wait(5, until=lambda: [r.name for r in p.selected_recs()] == ["usr"])
        how = "sous-dossiers" if recursive else "dossier"
        check([r.name for r in p.selected_recs()] == ["usr"],
              f"recherche ({how}) : « u » sélectionne usr d'office ({[r.name for r in p.selected_recs()]})")
        win.search_entry.emit("activate")
        yield Wait(5, until=lambda: p.location == os.path.join(root, "usr") and not p.model.loading)
        yield pump(0.4)
        names = sorted(r.name for r in p.model.items)
        check(p.location == os.path.join(root, "usr") and not win.search_bar.get_search_mode()
              and names == sorted(usr_children),
              f"recherche ({how}) : Entrée ouvre usr, recherche terminée, tout son contenu ({names})")
    win.recursive_btn.set_active(False)
    yield pump(0.2)


@step
def signets_doublons_thunar(win):
    """Thunar met souvent les dossiers de l'utilisateur en signets : comme Nautilus, ils ne sont pas
    affichés une seconde fois, et le fichier des signets (partagé avec Thunar) n'est pas touché."""
    from odysseus.backend import bookmarks
    home = os.environ["HOME"]
    docs, projets = os.path.join(home, "Documents"), os.path.join(home, "Projets")
    os.makedirs(docs, exist_ok=True)
    os.makedirs(projets, exist_ok=True)
    os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)
    with open(os.path.join(os.environ["XDG_CONFIG_HOME"], "user-dirs.dirs"), "w") as f:
        f.write('XDG_DOCUMENTS_DIR="$HOME/Documents"\n')
    GLib.reload_user_special_dirs_cache()
    os.makedirs(os.path.dirname(bookmarks.BOOKMARKS_FILE), exist_ok=True)
    content = f"{GLib.filename_to_uri(docs)}\n{GLib.filename_to_uri(projets)}\n"
    with open(bookmarks.BOOKMARKS_FILE, "w") as f:
        f.write(content)
    sb = win.sidebar
    sb.fill_places()
    sb._fill_bookmarks()
    yield pump(0.5)

    def targets(lb):
        out, r = [], lb.get_first_child()
        while r is not None:
            out.append(getattr(r, "target", None))
            r = r.get_next_sibling()
        return out
    check(docs in targets(sb.places_list), "Documents parmi les emplacements")
    check(targets(sb.bm_list) == [projets],
          f"signets : Documents (doublon de Thunar) masqué, Projets affiché ({targets(sb.bm_list)})")
    check(open(bookmarks.BOOKMARKS_FILE).read() == content, "fichier des signets intact (Thunar garde les siens)")
    os.unlink(bookmarks.BOOKMARKS_FILE)
    os.unlink(os.path.join(os.environ["XDG_CONFIG_HOME"], "user-dirs.dirs"))
    GLib.reload_user_special_dirs_cache()
    sb.fill_places()
    yield pump(0.3)


@step
def ce_pc_et_bureau(win):
    """Barre latérale : Bureau et « Ce PC » en option, suivis en direct ; la page « Ce PC »
    montre dossiers et disques, avec l'espace libre lu en asynchrone."""
    from odysseus.settings import settings
    from odysseus.ui.sidebar import SidebarRow

    def targets():
        out, row = [], win.sidebar.places_list.get_first_child()
        while row is not None:
            if isinstance(row, SidebarRow) and row.get_visible():
                out.append(row.target)
            row = row.get_next_sibling()
        return out
    desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
    check("computer:///" not in targets() and desktop not in targets(), "par défaut : ni « Ce PC » ni Bureau")
    settings["sidebar_this_pc"] = True
    settings["sidebar_desktop"] = True
    yield pump(0.3)
    t = targets()
    check(t and t[0] == "computer:///", f"« Ce PC » en tête de la barre latérale ({t[:2]})")
    check(desktop in t or not os.path.isdir(desktop or "/nulle/part"), "Bureau affiché sur demande")
    p = win.pane
    p.open("computer:///")
    yield pump(1.0)
    check(p.stack.get_visible_child_name() == "computer", "page « Ce PC » affichée")
    page = p.pages["computer:///"]
    folders = page.sections["folders"][1]
    drives = page.sections["drives"][1]
    n_folders = 0
    c = folders.get_first_child()
    while c is not None:
        n_folders += 1
        c = c.get_next_sibling()
    root = drives.get_first_child()
    check(n_folders >= 1 and root is not None and root.place.target == "/", "Ce PC : dossiers et disque système")
    check(root.bar.get_visible() and 0 < root.bar.get_value() <= 1,
          f"Ce PC : espace du disque système ({root.info.get_text()})")
    check(win.sidebar.current == "computer:///", "barre latérale : « Ce PC » sélectionné")
    check(not win.sidebar.other_list.get_visible(), "« Ce PC » affiché : « Autres emplacements » masqué")
    check(hasattr(page, "address") and page.address.get_ancestor(type(page)) is page,
          "Ce PC : barre « Se connecter à un serveur »")
    # emplacement réseau découvert (Freebox…) : tuile dans « Emplacements réseau », un clic l'ouvre
    from odysseus.backend.scanner import Rec
    rec = Rec("Freebox", None, True)
    rec.uri = "smb://freebox/"
    opened = []
    real_open = p.open
    p.open = lambda loc, *a, **k: opened.append(loc)
    try:
        page._net_recs = [rec]
        page._fill()
        net = page.sections["network"][1]
        tile = net.get_first_child()
        while tile is not None and tile.place.target != "smb://freebox/":
            tile = tile.get_next_sibling()
        check(tile is not None and page.sections["network"][0].get_visible(), "Ce PC : réseau découvert affiché")
        if tile is not None:
            page._activate(tile)
        check(opened == ["smb://freebox/"], f"Ce PC : clic sur un emplacement réseau ({opened})")
    finally:
        p.open = real_open
        page._net_recs = []
    folders.emit("child-activated", folders.get_first_child())
    yield pump(0.5)
    check(p.mode == "fs" and p.location == os.path.expanduser("~"), "Ce PC : clic sur un dossier l'ouvre")
    # menu de l'espace vide : « Ouvrir en tant qu'administrateur » (admin:// absent du bac à sable :
    # la fonctionnalité est déclarée disponible le temps du test)
    from odysseus.ui import menus
    from odysseus import features
    real = features.available
    features.available = lambda fid: True if fid == "admin" else real(fid)
    try:
        m = menus._background_menu(win, p)
    finally:
        features.available = real
    actions = [a.unpack() for i in range(m.get_n_items())
               for j in range(m.get_item_link(i, "section").get_n_items())
               if (a := m.get_item_link(i, "section").get_item_attribute_value(j, "action")) is not None]
    check("win.open-admin-folder" in actions, "menu de l'espace vide : « Ouvrir en tant qu'administrateur »")
    settings["sidebar_this_pc"] = False
    settings["sidebar_desktop"] = False
    yield pump(0.3)
    check("computer:///" not in targets() and desktop not in targets(), "options coupées : retirés en direct")
    check(win.sidebar.other_list.get_visible(), "« Ce PC » coupé : « Autres emplacements » revient")
    # libellés fixes lisibles en entier, quelle que soit la langue (« Autres emplacements »…), même
    # avec une police plus grande (Zorin : texte agrandi), et suivis en direct
    gs = Gtk.Settings.get_default()
    font = gs.get_property("gtk-font-name")
    gs.set_property("gtk-font-name", "Sans 15")
    yield pump(0.6)
    short = []
    for lb in (win.sidebar.places_list, win.sidebar.other_list):
        r = lb.get_first_child()
        while r is not None:
            if hasattr(r, "label") and r.label.get_layout().is_ellipsized():
                short.append(r.label.get_text())
            r = r.get_next_sibling()
    check(not short, f"barre latérale : libellés fixes en entier ({short or 'aucun abrégé'})")
    gs.set_property("gtk-font-name", font)
    yield pump(0.3)


@step
def espace_sous_le_dernier(win):
    """Comme Nautilus : défilé tout en bas, il reste sous le dernier élément de quoi loger la
    barre d'info (elle ne cache pas la dernière ligne)."""
    folder = tempfile.mkdtemp(dir=D, prefix="beaucoup ")
    for i in range(150):
        open(os.path.join(folder, f"fichier {i:03}.txt"), "w").close()
    p = win.pane
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    for mode, cls in (("list", "row"), ("grid", "child"), ("columns", "row")):
        win.activate_action("win.view-mode", GLib.Variant.new_string(mode))
        yield pump(0.6)
        sws = []

        def walk(w):
            if isinstance(w, Gtk.ScrolledWindow) and w.get_mapped():
                sws.append(w)
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(p)
        sw = max(sws, key=lambda w: w.get_vadjustment().get_upper())
        adj = sw.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        yield pump(0.6)
        bottom = 0

        def rows(w):
            nonlocal bottom
            if w.get_css_name() == cls and w.get_mapped():
                ok, b = w.compute_bounds(sw)
                if ok:
                    bottom = max(bottom, b.get_y() + b.get_height())
            c = w.get_first_child()
            while c is not None:
                rows(c)
                c = c.get_next_sibling()
        rows(sw)
        gap = sw.get_height() - bottom
        check(gap >= 30, f"vue {mode} : espace sous le dernier élément ({gap:.0f} px)")
    win.activate_action("win.view-mode", GLib.Variant.new_string("list"))
    yield pump(0.3)


@step
def extractions_un_seul_toast(win):
    """Plusieurs archives extraites d'un coup : un seul toast à la fin, pas un par archive."""
    import zipfile
    from odysseus.ui import actions_archives
    folder = tempfile.mkdtemp(dir=D, prefix="lot archives ")
    names = [f"archive {i}.zip" for i in range(1, 4)]
    for n in names:
        with zipfile.ZipFile(os.path.join(folder, n), "w") as z:
            z.writestr("a.txt", "a")
            z.writestr("b.txt", "b")
    p = win.pane
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    shown_toasts = []
    real = actions_archives.toast
    actions_archives.toast = lambda _w, text, *a, **k: shown_toasts.append(text)
    try:
        p.model.select_names(names)
        win.activate_action("win.extract-here", None)
        yield wait_jobs()
        yield pump(0.5)
    finally:
        actions_archives.toast = real
    dirs = sorted(n for n in os.listdir(folder) if os.path.isdir(os.path.join(folder, n)))
    check(dirs == ["archive 1", "archive 2", "archive 3"], f"extraire ici × 3 ({dirs})")
    check(len(shown_toasts) == 1, f"extractions : un seul toast ({shown_toasts})")


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
    # onglet Composants : programmes groupés par usage, une commande pour tout ce qui manque
    from odysseus import features
    from odysseus.ui.dialogs import PreferencesDialog
    real = features.present
    features.present = lambda name: name not in ("qpdf", "lz4") and real(name)
    try:
        prefs = PreferencesDialog()
        cmd = getattr(prefs, "install_command", "")
        check("qpdf" in cmd and "lz4" in cmd and cmd.startswith("sudo apt install "),
              f"composants : commande d'installation de ce qui manque ({cmd})")
        prefs.destroy()
    finally:
        features.present = real
    # listes déroulantes : choix longs en entier (libadwaita les abrégeait), coche sur le choix actuel
    from odysseus.ui.common import combo_row
    cw = Adw.PreferencesWindow(transient_for=win)
    cg = Adw.PreferencesGroup()
    crow = combo_row("Standard-Unterordner", ["Intelligent (bei mehreren Elementen)", "Immer", "Nie"], 0)
    cg.add(crow)
    cpage = Adw.PreferencesPage()
    cpage.add(cg)
    cw.add(cpage)
    cw.present()
    yield pump(0.5)
    crow.activate()
    yield pump(0.6)

    def combo_state():
        out = []

        def walk(x):
            if hasattr(x, "_check"):
                out.append((x._label.get_label(), x._label.get_layout().is_ellipsized(), x._check.get_opacity()))
            c = x.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(crow)
        return out
    st = combo_state()
    check(len(st) == 3 and not any(e for _l, e, _o in st) and [o for *_x, o in st] == [1, 0, 0],
          f"liste déroulante : texte entier, coche sur le choix actuel ({st})")
    crow.set_selected(2)
    yield pump(0.3)
    st = combo_state()
    check([o for *_x, o in st] == [0, 0, 1], f"liste déroulante : la coche suit le choix ({st})")
    cw.destroy()
    check(features.install_command("pdf_merge") == "" or "qpdf" in features.install_command("pdf_merge"),
          f"fusion PDF : jamais « python3-pypdf » réclamé ({features.install_command('pdf_merge')})")
    win.activate_action("win.shortcuts", None)
    yield pump(0.3)
@step
def telechargement_change_de_type(win):
    """Firefox : « x.zip » vide d'abord, puis « x.zip.part » renommé par-dessus. La ligne doit
    prendre le type (et l'icône) du vrai fichier, pas rester « fichier vide »."""
    import zipfile
    folder = tempfile.mkdtemp(dir=D, prefix="telechargements ")
    p = win.pane
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    open(os.path.join(folder, "paquet.zip"), "w").close()
    part = os.path.join(folder, "paquet.zip.part")
    with zipfile.ZipFile(part, "w") as z:
        z.writestr("lisezmoi.txt", "bonjour")
    yield pump(1.0)
    rec = next((r for r in p.model.items if r.name == "paquet.zip"), None)
    check(rec is not None and rec.content_type == "application/x-zerosize", "téléchargement : zip vide d'abord")
    os.replace(part, os.path.join(folder, "paquet.zip"))
    yield pump(1.0)
    rec = next((r for r in p.model.items if r.name == "paquet.zip"), None)
    names = sorted(r.name for r in p.model.items)
    check(rec is not None and rec.content_type == "application/zip" and names == ["paquet.zip"],
          f"téléchargement terminé : type du zip ({rec and rec.content_type}, {names})")


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
def grille_noms_dans_la_case(win):
    """Vue grille : un nom sur plusieurs lignes reste dans la case en surbrillance. La boîte centrée ne
    recevait que sa largeur naturelle, plus étroite que celle de la mesure : une ligne de plus,
    qui débordait sous la sélection (selon la police : reproduit en « Sans 12 »)."""
    folder = tempfile.mkdtemp(dir=D, prefix="grille ")
    for n in ("un-deux-trois-quatre-cinq-six.txt", "photo 2026-10-24 12-30-45.png", "Orchis-theme-master.zip",
              "Aurores boréales dans la région de Sápmi en Finlande.jpg", "x-y.txt",
              "a-b-c-d-e-f-g-h-i-j-k-l-m-n-o.txt", "Arromanches-les-Bains, Normandie.jpg",
              "Mes documents importants-copie.tar.gz", "Facture-2026-10-24-électricité.pdf",
              "Très-long-nom-de-fichier-avec-des-tirets-partout.odt", "IMG_20261024_123045_HDR_portrait.jpg"):
        open(os.path.join(folder, n), "w").close()
    gs = Gtk.Settings.get_default()
    font = gs.get_property("gtk-font-name")
    p = win.pane
    mode = p.view_mode
    try:
        for f in ("Sans 8", "Sans 9", "Sans 10", "Sans 11", "Sans 12", "Sans 13", "Sans 14"):
            gs.set_property("gtk-font-name", f)
            p.open(folder)
            yield Wait(5, until=lambda: not p.model.loading)
            win.activate_action("win.view-mode", GLib.Variant.new_string("grid"))
            yield pump(1.0)
            bad = []

            def walk(w):
                if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_wrap() and w.get_ancestor(Gtk.GridView):
                    child = w.get_parent().get_parent()
                    ok, b = w.compute_bounds(child)
                    if ok and b.get_y() + b.get_height() > child.get_height() + 0.5:
                        bad.append(w.get_label())
                c = w.get_first_child()
                while c is not None:
                    walk(c)
                    c = c.get_next_sibling()
            walk(p)
            check(not bad, f"grille ({f}) : les noms restent dans leur case ({bad or 'aucun ne déborde'})")
    finally:
        gs.set_property("gtk-font-name", font)
        win.activate_action("win.view-mode", GLib.Variant.new_string(mode))
    yield pump(0.3)


@step
def plusieurs_fichiers_un_lecteur(win):
    """Plusieurs vidéos / morceaux ouverts ensemble : UN seul lancement du lecteur par défaut (VLC…)
    avec tous les fichiers, dans l'ordre naturel des noms ((2) avant (10), pistes 01, 02…)."""
    from odysseus.ui.common import launch_defaults
    log = os.path.join(TMP, "lecteur.log")
    apps = os.path.join(os.environ["XDG_DATA_HOME"], "applications")
    os.makedirs(apps, exist_ok=True)
    script = os.path.join(TMP, "lecteur.sh")       # une ligne par lancement, les noms reçus dans l'ordre
    with open(script, "w") as f:
        f.write(f'#!/bin/sh\nfor a; do printf "%s|" "$(basename "$a")"; done >> {log}\necho >> {log}\n')
    os.chmod(script, 0o755)
    with open(os.path.join(apps, "lecteur-test.desktop"), "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Lecteur test\n"
                f"Exec={script} %F\nMimeType=video/mp4;audio/mpeg;\n")
    mimeapps = os.path.join(os.environ["XDG_CONFIG_HOME"], "mimeapps.list")
    old = open(mimeapps).read() if os.path.exists(mimeapps) else "[Default Applications]\n"
    with open(mimeapps, "w") as f:
        f.write(old.rstrip("\n") + "\nvideo/mp4=lecteur-test.desktop\naudio/mpeg=lecteur-test.desktop\n")
    subprocess.run(["update-desktop-database", apps], capture_output=True)
    folder = tempfile.mkdtemp(dir=D, prefix="videos ")
    names = ["Clip (10).mp4", "Clip (2).mp4", "Clip (1).mp4", "02 - Deux.mp3", "01 - Un.mp3"]
    for i, n in enumerate(names):
        with open(os.path.join(folder, n), "wb") as f:
            f.write(b"\0" * (100 * (i + 1)))
    open(log, "w").close()
    files = [Gio.File.new_for_path(os.path.join(folder, n)) for n in names]
    left = launch_defaults(win, files)
    yield Wait(5, until=lambda: open(log).read().count("\n") >= 1)
    yield pump(0.5)
    runs = [line for line in open(log).read().splitlines() if line]
    want = "01 - Un.mp3|02 - Deux.mp3|Clip (1).mp4|Clip (2).mp4|Clip (10).mp4|"
    check(not left and runs == [want],
          f"plusieurs fichiers : un seul lancement du lecteur, dans l'ordre des noms ({runs}, restés : {len(left)})")
    # par l'interface : sélection (ordre du modèle trié par taille) puis Entrée
    p = win.pane
    p.open(folder)
    yield Wait(5, until=lambda: not p.model.loading)
    open(log, "w").close()
    p.model.select_names(names)
    win.open_selected()
    yield Wait(5, until=lambda: open(log).read().count("\n") >= 1)
    yield pump(0.5)
    runs = [line for line in open(log).read().splitlines() if line]
    check(runs == [want], f"Entrée sur la sélection : un seul lecteur, ordre des noms ({runs})")
    open(log, "w").close()
    p.model.select_names(names)
    win._on_pane_activated(p, [r for r in p.model.items if r.name == "Clip (2).mp4"][0])
    yield Wait(5, until=lambda: open(log).read().count("\n") >= 1)
    yield pump(0.5)
    runs = [line for line in open(log).read().splitlines() if line]
    check(runs == [want], f"double-clic dans la sélection : toute la sélection, un seul lecteur ({runs})")
    # VLC (« une seule instance ») : une liste de lecture .m3u, lue depuis le début dans l'ordre
    vlc_log = os.path.join(TMP, "vlc.log")
    vscript = os.path.join(TMP, "vlc.sh")
    with open(vscript, "w") as f:
        f.write(f'#!/bin/sh\necho "$#|$1|$2" >> {vlc_log}\ncat "$2" >> {vlc_log}\n')
    os.chmod(vscript, 0o755)
    with open(os.path.join(apps, "vlc-test.desktop"), "w") as f:
        f.write(f"[Desktop Entry]\nType=Application\nName=VLC test\nExec={vscript} --started-from-file %U\n"
                "MimeType=video/x-matroska;\n")
    with open(mimeapps, "a") as f:
        f.write("video/x-matroska=vlc-test.desktop\n")
    subprocess.run(["update-desktop-database", apps], capture_output=True)
    mkv = ["Épisode 10.mkv", "Épisode 2.mkv", "Épisode 1.mkv"]
    for n in mkv:
        with open(os.path.join(folder, n), "wb") as f:
            f.write(b"\x1a\x45\xdf\xa3" + b"\0" * 64)
    open(vlc_log, "w").close()
    left = launch_defaults(win, [Gio.File.new_for_path(os.path.join(folder, n)) for n in mkv])
    yield Wait(5, until=lambda: "#EXTM3U" in open(vlc_log).read())
    yield pump(0.3)
    lines = open(vlc_log).read().splitlines()
    got = [GLib.filename_from_uri(line)[0].rsplit("/", 1)[-1] for line in lines if line.startswith("file://")]
    check(not left and lines and lines[0].startswith("2|--started-from-file|") and lines[0].endswith(".m3u")
          and got == ["Épisode 1.mkv", "Épisode 2.mkv", "Épisode 10.mkv"],
          f"VLC : une liste de lecture .m3u dans l'ordre des noms ({lines[:1]}, {got})")


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
    for name, is_dir, mode, row, value, expected in (("d754", True, 0o754, 0, 2, 0o554),
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
    # disposition de Nautilus : résumé en tête, permissions sur une page à part, fichiers inclus
    tree = os.path.join(base, "arbre")
    os.makedirs(os.path.join(tree, "sous"))
    for f, m in (("a.txt", 0o600), ("sous/b.sh", 0o700)):
        with open(os.path.join(tree, f), "w") as fh:
            fh.write("x")
        os.chmod(os.path.join(tree, f), m)
    os.chmod(os.path.join(tree, "sous"), 0o700)
    os.symlink(tgt, os.path.join(tree, "lien"))
    d = PropertiesDialog(win, [tree])
    d.present(win)
    yield Wait(3, until=lambda: "…" not in d.summary.get_label())
    from odysseus.backend.engine import human_size
    size = sum(os.lstat(os.path.join(tree, f)).st_size for f in ("a.txt", "sous/b.sh", "lien"))
    want = ntr("{0} élément, de taille {1}", "{0} éléments, totalisant {1}", 4).format("4", human_size(size))
    check(d.summary.get_label() == want, f"résumé sous le nom ({d.summary.get_label()!r})")
    check(d.perm_summary.get_label() == tr("Création et suppression des fichiers"),
          f"ligne Permissions : accès du propriétaire ({d.perm_summary.get_label()!r})")
    d.nav.push_by_tag("permissions")
    yield pump(0.4)
    check(d.nav.get_visible_page().get_tag() == "permissions", "page « Définir des permissions personnalisées »")
    d.enclosed_button.emit("clicked")
    yield pump(0.4)
    rows = d._enclosed_rows
    for (kind, shift), (r, choices) in rows.items():          # fichiers : rw-r--r-- ; dossiers : rwxr-xr-x
        want_bits = {("files", 6): 6, ("files", 3): 4, ("files", 0): 4,
                     ("dirs", 6): 7, ("dirs", 3): 5, ("dirs", 0): 5}[(kind, shift)]
        r.set_selected([b for _l, b in choices].index(want_bits))
    d._enclosed_go()
    yield Wait(5, until=lambda: stat.S_IMODE(os.stat(os.path.join(tree, "sous", "b.sh")).st_mode) == 0o744)
    got = {f: oct(stat.S_IMODE(os.lstat(os.path.join(tree, f)).st_mode)) for f in ("", "a.txt", "sous", "sous/b.sh")}
    check(got == {"": "0o755", "a.txt": "0o644", "sous": "0o755", "sous/b.sh": "0o744"},
          f"permissions des fichiers inclus (exécution des fichiers gardée) ({got})")
    check(stat.S_IMODE(os.stat(tgt).st_mode) == 0o600, "fichiers inclus : la cible d'un lien n'est pas touchée")
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
def renommage_multiple(win):
    """Plusieurs éléments + F2 : le dialogue « Renommer » de Nautilus (modèle, numérotation, rechercher /
    remplacer, conflits), renommage en une opération, Ctrl+Z / Ctrl+Maj+Z."""
    from odysseus.backend import batchrename as br
    base = os.path.join(D, "multi")
    os.makedirs(os.path.join(base, "Dossier"))
    for n in ("IMG_2.jpg", "IMG_10.jpg", "IMG_1.jpg", "garde.txt"):
        open(os.path.join(base, n), "w").close()
    p = win.pane
    p.open(base)
    yield Wait(5, until=lambda: not p.model.loading)
    p.model.select_names(["IMG_2.jpg", "IMG_10.jpg", "IMG_1.jpg", "Dossier"])
    yield pump(0.2)
    check(win.lookup_action("rename").get_enabled(), "renommer actif avec plusieurs éléments")
    before = list(Gtk.Window.list_toplevels())
    win.activate_action("win.rename", None)
    yield pump(0.5)
    dlg = [w for w in Gtk.Window.list_toplevels() if w not in before and type(w).__name__ == "BatchRenameDialog"]
    check(len(dlg) == 1, "F2 sur plusieurs éléments : dialogue de renommage multiple")
    if not dlg:
        return
    d = dlg[0]
    check(not d.rename_button.get_sensitive(), "rien ne change : « Renommer » grisé")
    d.name_entry.set_text("Vacances ")
    d.name_entry.set_position(-1)
    d.activate_action("rename.tag-num2", None)
    yield pump(0.3)
    check(d.name_entry.get_text() == "Vacances " + br.tag_text("num2") and d.numbering.get_reveal_child(),
          f"balise ajoutée, ordre de numérotation proposé ({d.name_entry.get_text()!r})")
    shown_new = [r._new.get_text() for r in d.rows]
    check(shown_new == ["Vacances 01", "Vacances 02.jpg", "Vacances 03.jpg", "Vacances 04.jpg"],
          f"aperçu dans l'ordre naturel, extensions gardées ({shown_new})")
    # effacer dans une balise l'efface d'un bloc
    n = len(d.name_entry.get_text())
    d.name_entry.get_delegate().delete_text(n - 2, n - 1)
    yield pump(0.2)
    check(d.name_entry.get_text() == "Vacances ", f"balise effacée d'un bloc ({d.name_entry.get_text()!r})")
    # conflit avec un fichier qui n'est pas renommé
    d.replace_mode.set_active(True)
    yield pump(0.2)
    d.find_entry.set_text("IMG_1.jpg")
    d.replace_entry.set_text("garde.txt")
    yield pump(0.3)
    check(d.conflict_box.get_visible() and not d.rename_button.get_sensitive(),
          f"conflit signalé, « Renommer » grisé ({d.conflict_label.get_text()!r})")
    d.find_entry.set_text("IMG")
    d.replace_entry.set_text("Photo")
    yield pump(0.3)
    check(not d.conflict_box.get_visible() and d.rename_button.get_sensitive(), "rechercher / remplacer sans conflit")
    d.rename_button.emit("clicked")
    yield Wait(5, until=lambda: os.path.exists(os.path.join(base, "Photo_10.jpg")))
    yield pump(0.5)
    names = sorted(os.listdir(base))
    check(names == ["Dossier", "Photo_1.jpg", "Photo_10.jpg", "Photo_2.jpg", "garde.txt"], f"renommés ({names})")
    win.activate_action("win.undo", None)
    yield Wait(5, until=lambda: os.path.exists(os.path.join(base, "IMG_10.jpg")))
    names = sorted(os.listdir(base))
    check(names == ["Dossier", "IMG_1.jpg", "IMG_10.jpg", "IMG_2.jpg", "garde.txt"], f"annulé d'un coup ({names})")
    yield pump(0.3)
    win.activate_action("win.redo", None)
    yield Wait(5, until=lambda: os.path.exists(os.path.join(base, "Photo_10.jpg")))
    names = sorted(os.listdir(base))
    check(names == ["Dossier", "Photo_1.jpg", "Photo_10.jpg", "Photo_2.jpg", "garde.txt"], f"rétabli ({names})")
    yield pump(0.3)


@step
def outils_image(win):
    """Outils image : menu sur des images, redimensionner + convertir en une fois, originaux intacts."""
    import shutil as _sh
    import subprocess as _sp
    if not (_sh.which("convert") and _sh.which("identify")):
        print("(outils image : ImageMagick absent, sauté)")
        return
    from odysseus.ui import menus
    base = os.path.join(D, "images")
    os.makedirs(base)
    for n in ("a.png", "b [1].png"):
        _sp.run(["convert", "-size", "200x100", "xc:red", os.path.join(base, n)], check=True)
    p = win.pane
    p.open(base)
    yield Wait(5, until=lambda: not p.model.loading)
    p.model.select_names(["a.png", "b [1].png"])
    yield pump(0.2)
    m = menus.context_menu(win, p, True)
    labels = []

    def walk(model):
        for i in range(model.get_n_items()):
            v = model.get_item_attribute_value(i, "label", None)
            if v:
                labels.append(v.get_string())
            for link in ("section", "submenu"):
                sub = model.get_item_link(i, link)
                if sub:
                    walk(sub)
    walk(m)
    check(tr("Outils image") in labels and tr("Convertir…") in labels, "menu « Outils image » sur des images")
    before = list(Gtk.Window.list_toplevels())
    win.activate_action("win.image-resize", None)
    yield pump(0.5)
    dlg = [w for w in Gtk.Window.list_toplevels() if w not in before and w.get_visible()]
    check(len(dlg) == 1, "fenêtre « Redimensionner les images »")
    from odysseus.ui import image_dialog
    d = image_dialog._last
    d.scale.set_value(50)
    d.convert.set_enable_expansion(True)
    d.fmt.set_selected(0)          # JPEG
    yield pump(0.2)
    check(d.quality.get_sensitive() and d.action.get_sensitive(), "qualité réglable vers JPEG, bouton actif")
    d.action.emit("clicked")
    suf = tr(" (redimensionnée)")
    yield Wait(10, until=lambda: os.path.exists(os.path.join(base, f"b [1]{suf}.jpg")))
    yield pump(0.5)
    names = sorted(os.listdir(base))
    check(names == sorted([f"a{suf}.jpg", "a.png", f"b [1]{suf}.jpg", "b [1].png"]),
          f"images enregistrées à côté, originaux intacts ({names})")
    out = _sp.run(["identify", "-format", "%w %h %m", os.path.join(base, f"a{suf}.jpg")],
                  capture_output=True, text=True).stdout
    check(out == "100 50 JPEG", f"redimensionnée et convertie ({out})")
    yield pump(0.3)


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
def apercu_rapide(win):
    """Espace : aperçu rapide par Sushi (faux Sushi D-Bus), comme Nautilus : Espace ouvre / referme,
    l'aperçu suit la sélection, une flèche tapée dans Sushi déplace la sélection d'Odysseus."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        print("- pas de bus de session : test de l'aperçu rapide sauté")
        return
    from odysseus import features as feat
    from odysseus.ui import sushi
    base = os.path.join(D, "apercu")
    os.makedirs(base)
    for n in ("a.txt", "b.txt", "c.txt"):
        open(os.path.join(base, n), "w").close()
    marker = os.path.join(TMP, "org.gnome.NautilusPreviewer.service")
    open(marker, "w").close()
    saved = feat.LIBS["lib:sushi"]
    feat.LIBS["lib:sushi"] = marker                    # « Sushi installé »
    journal = os.path.join(TMP, "sushi.log")
    fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_sushi.py"), journal])
    uri = lambda n: Gio.File.new_for_path(os.path.join(base, n)).get_uri()  # noqa: E731
    lines = lambda: [line for line in (open(journal).read().splitlines() if os.path.exists(journal) else [])  # noqa: E731
                     if not line.startswith("get ")]
    try:
        yield pump(0.8)
        p = win.pane
        p.open(base)
        yield Wait(5, until=lambda: not p.model.loading)
        p.model.select_names(["a.txt"])
        p.focus_view()
        yield pump(0.3)
        win.present()
        check(win._on_type_ahead(None, Gdk.KEY_space, 0, 0), "Espace pris par l'aperçu rapide")
        yield Wait(5, until=lambda: lines())
        check(lines()[:1] == [f"show {uri('a.txt')} x11 1"], f"Sushi ouvert sur le fichier sélectionné ({lines()})")
        yield pump(0.5)
        p.model.select_names(["b.txt"])
        yield Wait(3, until=lambda: len(lines()) >= 2)
        check(lines()[1:2] == [f"show {uri('b.txt')} x11 0"], f"l'aperçu suit la sélection ({lines()})")
        sushi.previewer.proxy.call_sync("Arrow", GLib.Variant("(u)", (int(Gtk.DirectionType.DOWN),)),
                                        Gio.DBusCallFlags.NONE, -1, None)
        yield Wait(3, until=lambda: len(lines()) >= 3)
        check([r.name for r in p.selected_recs()] == ["c.txt"] and lines()[2:3] == [f"show {uri('c.txt')} x11 0"],
              f"flèche dans Sushi : élément suivant, montré dans l'aperçu ({lines()[2:]})")
        win._on_type_ahead(None, Gdk.KEY_space, 0, 0)
        yield Wait(3, until=lambda: len(lines()) >= 4)
        check(lines()[3:4] == [f"show {uri('c.txt')} x11 1"], f"Espace referme l'aperçu ({lines()})")
        yield pump(0.3)
        n = len(lines())
        p.model.select_names(["a.txt"])
        yield pump(0.8)
        check(len(lines()) == n, f"aperçu fermé : ne suit plus la sélection ({lines()[n:]})")
        feat.set_enabled("quick_look", False)
        check(not win._on_type_ahead(None, Gdk.KEY_space, 0, 0), "aperçu rapide désactivable à chaud")
        feat.set_enabled("quick_look", True)
    finally:
        fake.terminate()
        feat.LIBS["lib:sushi"] = saved
    p.open(D)
    yield Wait(5, until=lambda: not p.model.loading)


@step
def icone_personnalisee(win):
    """Propriétés d'un dossier : icône personnalisée (crayon), retirée (croix) puis « Annuler »,
    enregistrée dans les métadonnées de Nautilus (metadata::custom-icon)."""
    from odysseus.backend import folder_colors
    from odysseus.ui.properties import PropertiesDialog
    base = os.path.join(D, "icone-perso")
    os.makedirs(os.path.join(base, "Boulot"))
    img = os.path.join(base, "logo.png")
    Gtk.IconTheme.get_for_display(win.get_display())          # (thème chargé)
    with open(img, "wb") as f:
        f.write(bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                              "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    target = os.path.join(base, "Boulot")
    d = PropertiesDialog(win, [target])
    d.present(win)
    yield pump(0.3)
    check(d.icon_edit.get_visible() and not d.icon_reset.get_visible(), "propriétés d'un dossier : crayon, pas de croix")
    folder_colors.set_custom_icon(target, Gio.File.new_for_path(img).get_uri())
    d._changed(d.recs[0])
    yield pump(0.2)
    check(folder_colors.read(target).get("icon_uri", "").endswith("logo.png") and d.icon_reset.get_visible(),
          "icône personnalisée posée, croix affichée")
    d._reset_icon(d.recs[0])
    yield pump(0.2)
    check(not folder_colors.read(target) and not d.icon_reset.get_visible(), "croix : icône personnalisée retirée")
    folder_colors.restore_icon(target, {"icon_uri": Gio.File.new_for_path(img).get_uri()})
    check(folder_colors.read(target).get("icon_uri", "").endswith("logo.png"), "« Annuler » remet l'image")
    d.close()
    yield pump(0.2)


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
sys.stderr.flush()
time.sleep(0.2)                 # le fil de relecture finit de lire la sortie d'erreur
for line in CRITICALS:
    errors.append("ÉCHEC : avertissement critique de GTK / GLib : " + line)
print("\n".join(errors) if errors else "\nAucune erreur.")
sys.exit(1 if errors else 0)
