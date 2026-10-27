"""Faux Sushi pour les tests (sous dbus-run-session) : interface org.gnome.NautilusPreviewer2 de
gnome-sushi. Chaque appel (ShowFile uri fermer / Close) est écrit dans le journal ; la méthode de test
Arrow(direction) émet SelectionEvent, comme une flèche tapée dans la fenêtre de Sushi.

    python3.12 tests/fake_sushi.py JOURNAL [--muet]
"""
import sys

from gi.repository import Gio, GLib

NAME = "org.gnome.NautilusPreviewer"
PATH = "/org/gnome/NautilusPreviewer"
IFACE = "org.gnome.NautilusPreviewer2"
XML = f"""<node><interface name="{IFACE}">
  <method name="ShowFile"><arg type="s"/><arg type="s"/><arg type="b"/></method>
  <method name="Close"/>
  <method name="Arrow"><arg type="u"/></method>
  <property name="Visible" type="b" access="read"/>
  <signal name="SelectionEvent"><arg type="u"/></signal>
</interface></node>"""
log = sys.argv[1]
# Comme le vrai Sushi 46 (src/ui/application.js) : il annonce Visible (PropertiesChanged) mais ne répond
# pas à Properties.Get (pas de lecteur pour la propriété) — vu chez Tof avec dbus-monitor.
# --muet : pas non plus d'annonce (cas extrême : seules les flèches de Sushi disent qu'il est ouvert).
QUIET = "--muet" in sys.argv
state = {"visible": False, "conn": None}


def write(line):
    with open(log, "a") as f:
        f.write(line + "\n")


def set_visible(v):
    state["visible"] = v
    if QUIET:
        return
    state["conn"].emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", (IFACE, {"Visible": GLib.Variant("b", v)}, [])))


def call(conn, sender, path, iface, method, params, inv):
    if method == "ShowFile":
        uri, handle, close = params.unpack()
        write(f"show {uri} {handle.split(':')[0]} {int(close)}")
        set_visible(not (close and state["visible"]))
    elif method == "Close":
        write("close")
        set_visible(False)
    elif method == "Arrow":
        conn.emit_signal(None, PATH, IFACE, "SelectionEvent", params)
    inv.return_value(None)


def get_prop(conn, sender, path, iface, name):
    write("get " + name)
    return None                                  # Sushi : pas de lecteur → erreur pour l'appelant


def bus_acquired(conn, _name):
    state["conn"] = conn
    conn.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], call, get_prop, None)


Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE, bus_acquired, None, None)
GLib.MainLoop().run()
