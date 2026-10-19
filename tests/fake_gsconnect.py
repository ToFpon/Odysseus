"""Faux service GSConnect pour les tests (sous dbus-run-session) : un appareil « Pixel test » qui
accepte les fichiers ; chaque URI reçue par l'action shareFile est écrite dans le fichier passé en
argument.

    python3.12 tests/fake_gsconnect.py JOURNAL
"""
import sys

from gi.repository import Gio, GLib

NAME = "org.gnome.Shell.Extensions.GSConnect"
ROOT = "/org/gnome/Shell/Extensions/GSConnect"
DEVICE = ROOT + "/Device/pixeltest"
XML = """<node><interface name="org.freedesktop.DBus.ObjectManager">
  <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
  <signal name="InterfacesAdded"><arg type="o"/><arg type="a{sa{sv}}"/></signal>
  <signal name="InterfacesRemoved"><arg type="o"/><arg type="as"/></signal>
</interface></node>"""
log = sys.argv[1]


def call(conn, sender, path, iface, method, params, inv):
    objects = {DEVICE: {"org.gnome.Shell.Extensions.GSConnect.Device": {
        "Name": GLib.Variant("s", "Pixel test"), "Connected": GLib.Variant("b", True)}}}
    inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))


def bus_acquired(conn, _name):
    conn.register_object(ROOT, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], call, None, None)
    group = Gio.SimpleActionGroup()
    share = Gio.SimpleAction.new("shareFile", GLib.VariantType.new("(sb)"))

    def received(_a, v):
        with open(log, "a") as f:
            f.write(v.unpack()[0] + "\n")
    share.connect("activate", received)
    group.add_action(share)
    conn.export_action_group(DEVICE, group)
    bus_acquired.group = group           # gardé en vie


# objets publiés dès l'obtention du bus, avant le nom : aucun client ne les demande trop tôt
Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE, bus_acquired, None, None)
GLib.MainLoop().run()
