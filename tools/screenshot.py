#!/usr/bin/env python3
"""Capture de la fenêtre d'Odysseus (à lancer sous xvfb-run) pour la documentation et les tests.

    xvfb-run -a python3 tools/screenshot.py SORTIE.png [CHEMIN] [--dual] [--grid] [--columns] [--preview]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from odysseus.app import OdysseusApp  # noqa: E402

out = sys.argv[1]
args = [a for a in sys.argv[2:] if not a.startswith("--")]
flags = {a for a in sys.argv[2:] if a.startswith("--")}
delay = 1800


def snap(win):
    def do():
        paint = Gtk.WidgetPaintable.new(win)
        w, h = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        paint.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        renderer = win.get_renderer()
        tex = renderer.render_texture(node, None)
        tex.save_to_png(out)
        print("capture", out, w, h)
        win.get_application().quit()
        return False
    GLib.timeout_add(delay, do)


app = OdysseusApp()


def on_ready(win):
    win.set_default_size(1280, 780)
    if "--dual" in flags:
        win.activate_action("win.dual", None)
    if "--grid" in flags:
        win.activate_action("win.view-mode", GLib.Variant.new_string("grid"))
    if "--columns" in flags:
        win.activate_action("win.view-mode", GLib.Variant.new_string("columns"))
    if "--preview" in flags:
        win.activate_action("win.preview", None)
    if "--select" in flags and win.pane:
        GLib.timeout_add(600, lambda: (win.pane.selection.select_item(min(3, len(win.pane.model.items) - 1), True), False)[1])
    snap(win)
    return False


app.connect("window-added", lambda _a, w: GLib.idle_add(on_ready, w))
sys.exit(app.run([sys.argv[0]] + args))
