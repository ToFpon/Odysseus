"""Installateur de paquets .deb (reprise de Deb Installer) : infos, dépendances, sortie en direct."""

from __future__ import annotations

import os
import subprocess
import threading

from gi.repository import Adw, GLib, Gtk, Pango

from ..backend import debpkg
from ..i18n import tr
from .common import DialogWindow


class DebInstallerDialog(DialogWindow):
    def __init__(self, window, path: str):
        super().__init__(title=tr("Installer un paquet"), content_width=640, content_height=600)
        self.path = path
        self.proc = None
        self.done = False
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.cancel = Gtk.Button(label=tr("Annuler"))
        self.cancel.connect("clicked", lambda *_: self._cancel())
        hb.pack_start(self.cancel)
        self.install = Gtk.Button(label=tr("Installer"), css_classes=["suggested-action"], sensitive=False)
        self.install.connect("clicked", lambda *_: self._install())
        hb.pack_end(self.install)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16, margin_end=16,
                      margin_top=12, margin_bottom=12)
        head = Gtk.Box(spacing=12)
        head.append(Gtk.Image(icon_name="application-x-deb", pixel_size=48))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(label=os.path.basename(path), xalign=0, css_classes=["title-3"], ellipsize=3)
        self.version = Gtk.Label(xalign=0, css_classes=["dim-label"])
        col.append(self.name)
        col.append(self.version)
        head.append(col)
        box.append(head)
        self.desc = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], selectable=True)
        box.append(self.desc)
        warn = Gtk.Label(label=tr("N'installez que des paquets de source sûre : un paquet a tous les droits sur le système."),
                         xalign=0, wrap=True, css_classes=["warning", "caption"])
        box.append(warn)
        self.deps = Gtk.Label(label=tr("Vérification des dépendances…"), xalign=0, wrap=True, css_classes=["caption"])
        self.deps_spin = Gtk.Spinner(spinning=True)
        dbox = Gtk.Box(spacing=6)
        dbox.append(self.deps_spin)
        dbox.append(self.deps)
        box.append(dbox)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=8,
                                 right_margin=8, css_classes=["terminal-view"])
        self.buf = self.view.get_buffer()
        self.tag_ok = self.buf.create_tag("ok", weight=Pango.Weight.BOLD)
        self.tag_err = self.buf.create_tag("err", weight=Pango.Weight.BOLD, underline=Pango.Underline.ERROR)
        self.tag_dim = self.buf.create_tag("dim", style=Pango.Style.ITALIC)
        self.scroll = Gtk.ScrolledWindow(child=self.view, vexpand=True, css_classes=["card"])
        box.append(self.scroll)
        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)
        tv.set_content(box)
        self.set_child(tv)
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        inf = debpkg.info(self.path)
        deps = debpkg.missing_dependencies(self.path)
        GLib.idle_add(self._loaded, inf, deps)

    def _loaded(self, inf, deps):
        self.name.set_text(inf.name)
        ver = tr("Version {0}").format(inf.version) if inf.version else ""
        if inf.arch:
            ver += f" · {inf.arch}"
        if inf.installed:
            ver += " · " + tr("version installée : {0}").format(inf.installed)
        self.version.set_text(ver)
        self.desc.set_text(inf.description)
        self.deps_spin.set_spinning(False)
        self.deps_spin.set_visible(False)
        if deps is None:
            self.deps.set_text(tr("Impossible de vérifier les dépendances (apt indisponible ou paquet invalide)."))
        elif not deps:
            self.deps.set_text(tr("✓ Aucune dépendance supplémentaire à installer."))
        else:
            self.deps.set_text(tr("Paquets supplémentaires qui seront installés :") + "\n  • " + "\n  • ".join(deps))
        self.install.set_sensitive(True)
        return False

    def _append(self, text, tag=None):
        end = self.buf.get_end_iter()
        if tag is not None:
            self.buf.insert_with_tags(end, text, tag)
        else:
            self.buf.insert(end, text)
        adj = self.scroll.get_vadjustment()
        GLib.idle_add(lambda: (adj.set_value(adj.get_upper()), False)[1])
        return False

    def _install(self):
        self.install.set_sensitive(False)
        cmd = debpkg.install_command(self.path)
        self._append("$ " + " ".join(cmd) + "\n", self.tag_dim)
        self.status.set_text(tr("Installation en cours… (mot de passe administrateur demandé)"))

        def work():
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
                for line in self.proc.stdout:
                    if not debpkg.is_progress_line(line):
                        GLib.idle_add(self._append, line)
                rc = self.proc.wait()
            except OSError as e:
                GLib.idle_add(self._append, f"{e}\n", self.tag_err)
                rc = -1
            GLib.idle_add(self._finished, rc)
        threading.Thread(target=work, daemon=True).start()

    def _finished(self, rc):
        self.done = True
        self.cancel.set_label(tr("Fermer"))
        if rc == 0:
            self.status.set_text(tr("✓ Paquet installé."))
            self._append("\n" + tr("✓ Paquet installé.") + "\n", self.tag_ok)
        elif rc in (126, 127):
            self.status.set_text(tr("Authentification refusée ou annulée."))
        else:
            self.status.set_text(tr("L'installation a échoué (code {0}).").format(rc))
            self._append("\n" + tr("L'installation a échoué (code {0}).").format(rc) + "\n", self.tag_err)
        return False

    def _cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.close()
