"""Point d'entrée de l'application Odysseus."""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION  # noqa: E402
from .i18n import tr  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _inside_archive(path: str) -> bool:
    """« /chemin/archive.7z/dossier » : chemin dans une archive, ouvert par le panneau."""
    probe = os.path.dirname(path)
    while probe and probe != "/":
        if os.path.isfile(probe):
            return True
        probe = os.path.dirname(probe)
    return False


class OdysseusApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.add_main_option("new-window", ord("w"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             tr("Ouvrir une nouvelle fenêtre"), None)
        self.add_main_option("dual", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             tr("Ouvrir en double panneau"), None)
        self.add_main_option("search-content", ord("s"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             tr("Rechercher dans le contenu du dossier donné"), None)
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             tr("Afficher la version"), None)

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        # Mise en page seulement : les couleurs viennent du thème GTK actif.
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(DATA_DIR, "base.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icons = os.path.join(DATA_DIR, "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(display).add_search_path(icons)
        from .ui.dialogs import apply_theme
        apply_theme()
        from .backend import gsconnect
        gsconnect.devices.start()           # appareils GSConnect suivis dès le départ, pour le menu
        for name, cb, accels in (("preferences", self._prefs, ["<Ctrl>comma"]),
                                 ("features", lambda *_: self._prefs(page="features"), []),
                                 ("about", self._about, []),
                                 ("quit", self._quit, ["<Ctrl>q"])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        from .settings import settings
        if settings["dbus_file_manager"]:
            from .dbus import FileManager1
            self._fm1 = FileManager1(self)
            self._fm1.start()

    def _quit(self):
        for w in list(self.get_windows()):
            w.close()

    def _new_window(self, location=None, dual=None):
        from .ui.window import MainWindow
        win = MainWindow(self, location, dual=dual)
        win.present()
        return win

    def _window(self):
        win = self.get_active_window()
        if win is None:
            from .ui.window import MainWindow
            wins = [w for w in self.get_windows() if isinstance(w, MainWindow)]
            win = wins[0] if wins else None
        return win

    def do_activate(self):
        win = self._window()
        if win is None:
            win = self._new_window()
        win.present()

    def do_open(self, files, n, hint):
        win = self._window() or self._new_window(files[0].get_path() or files[0].get_uri())
        for i, f in enumerate(files):
            loc = f.get_path() or f.get_uri()
            if i == 0 and win.pane:
                win.open_location(loc)
            else:
                win.new_tab(loc)
        win.present()

    def do_command_line(self, cmd):
        opts = cmd.get_options_dict().end().unpack()
        if opts.get("version"):
            cmd.print_literal(f"{APP_NAME} {VERSION}\n")
            return 0
        cwd = cmd.get_cwd() or os.getcwd()
        args = cmd.get_arguments()[1:]
        locs, missing = [], []
        for a in args:
            if "://" in a and not a.startswith("file://"):
                locs.append(a)
            else:
                f = Gio.File.new_for_commandline_arg_and_cwd(a, cwd)
                path = f.get_path()
                if path and not os.path.lexists(path) and not _inside_archive(path):
                    # comme Nautilus : on le dit, au lieu d'un onglet muet sur le dossier personnel
                    missing.append(path)
                    cmd.printerr_literal(tr("Emplacement introuvable : {0}").format(path) + "\n")
                    continue
                locs.append(path or f.get_uri())
        dual = True if opts.get("dual") else None
        win = self._window()
        pair = locs[1] if dual and len(locs) >= 2 else None    # --dual A B : un seul onglet A | B
        if win is None or opts.get("new-window"):
            win = self._new_window(locs[0] if locs else None, dual=dual)
            rest = locs[2:] if pair else locs[1:]
        else:
            if dual and locs:
                win.new_tab(locs[0])
                rest = locs[2:] if pair else locs[1:]
            else:
                rest = locs
            if dual and not win.is_dual():
                # pas l'action « dual » : elle bascule un état commun à la fenêtre et pouvait ÉTEINDRE le
                # double panneau du nouvel onglet ; _set_dual l'active sur l'onglet courant et resynchronise
                win._set_dual(True)
        if pair and win.is_dual():
            other = win.other_pane()
            if other:
                other.open(pair)
        for loc in rest:
            win.new_tab(loc)
        win.present()
        if missing:
            from .ui.common import toast
            msg = tr("Emplacement introuvable : {0}").format(", ".join(missing))
            GLib.timeout_add(300, lambda: (toast(win, msg), False)[1])
        if opts.get("search-content"):
            win.content_search(locs[0] if locs else "")
        return 0

    def _prefs(self, page: str = ""):
        from .ui.dialogs import PreferencesDialog
        d = PreferencesDialog()
        if page:
            d.set_visible_page_name(page)
        d.set_transient_for(self._window())
        d.present()

    def _about(self):
        about = Adw.AboutWindow(transient_for=self._window(), modal=True,
            application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
            developer_name="Tof",
            comments=tr("Gestionnaire de fichiers pour GNOME, inspiré de Nautilus.\n"
                        "Double panneau, colonnes, aperçu, archives, outils PDF et bien plus, intégrés."),
            website="https://github.com/ToFpon/Odysseus",
            issue_url="https://github.com/ToFpon/Odysseus/issues",
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_credit_section(tr("Basé sur"), [tr("Les extensions Nautilus de Tof https://github.com/ToFpon/Nautilus-Extensions"),
                                                  tr("Le moteur d'archives de Pressoir https://github.com/ToFpon/Pressoir")])
        about.add_acknowledgement_section(tr("Moteurs"), ["7-Zip", "Ghostscript", "qpdf", "FFmpeg", "ripgrep",
                                                          "Poppler"])
        about.present()


def main(argv=None) -> int:
    # En cas de plantage natif (GTK, GLib, Poppler…), afficher la pile Python de chaque fil
    import faulthandler
    faulthandler.enable(all_threads=True)
    app = OdysseusApp()
    return app.run(argv if argv is not None else sys.argv)
