#!/usr/bin/env python3
"""Capture de la fenêtre d'Odysseus (à lancer sous xvfb-run) pour la documentation et les tests.

    xvfb-run -a python3 tools/screenshot.py SORTIE.png [CHEMIN] [options]

Options : --dual --other=chemin --grid --columns --preview --zoom=N --select=nom1,nom2 --action=win.xxx
          --prefs=page --dark --delay=ms --size=LxH
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from odysseus.app import OdysseusApp  # noqa: E402

out = sys.argv[1]
args = [a for a in sys.argv[2:] if not a.startswith("--")]
opts = {}
for a in sys.argv[2:]:
    if a.startswith("--"):
        k, _, v = a[2:].partition("=")
        opts[k] = v
delay = int(opts.get("delay", 1800))


def snap(win):
    def do():
        target = win
        paint = Gtk.WidgetPaintable.new(target)
        w, h = target.get_width(), target.get_height()
        snapshot = Gtk.Snapshot()
        paint.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        tex = target.get_renderer().render_texture(node, None)
        tex.save_to_png(out)
        print("capture", out, w, h)
        win.get_application().quit()
        return False
    GLib.timeout_add(delay, do)


app = OdysseusApp()


def on_ready(win):
    if getattr(app, "_shot", False):
        return False
    app._shot = True
    if "dark" in opts:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    w, _, h = opts.get("size", "1280x780").partition("x")
    win.set_default_size(int(w), int(h))
    if "dual" in opts:
        win.activate_action("win.dual", None)
    for mode in ("grid", "columns"):
        if mode in opts:
            win.activate_action("win.view-mode", GLib.Variant.new_string(mode))
    if "preview" in opts:
        win.activate_action("win.preview", None)

    def later():
        if "other" in opts and win.other_pane():
            win.other_pane().open(opts["other"])
        if "select" in opts and win.pane:
            win.pane.select_names(opts["select"].split(","))
        if "action" in opts:
            GLib.timeout_add(300, lambda: (win.activate_action(opts["action"], None), False)[1])
        if "prefs" in opts:
            from odysseus.ui.dialogs import PreferencesDialog
            d = PreferencesDialog()
            if opts["prefs"]:
                d.set_visible_page_name(opts["prefs"])
            d.present(win)
        return False
    GLib.timeout_add(700, later)
    snap(win)
    return False


app.connect("window-added", lambda _a, w: GLib.idle_add(on_ready, w))
sys.exit(app.run([sys.argv[0]] + args))
