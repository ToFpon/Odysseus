"""« Options de partage… » d'un dossier (Samba), comme nautilus-share : partager ce dossier, nom du partage,
commentaire, autoriser les autres à créer et supprimer des fichiers, accès invité. Moteur :
backend/usershare.py (`net usershare`). Droits manquants du dossier : ajoutés après accord, comme
nautilus-share (« … doit ajouter des permissions à votre dossier »), retirés quand le partage s'arrête."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, Gtk

from ..backend import usershare
from ..i18n import tr
from .common import alert, dialog_shell, toast

_refreshing = False
_last = None          # pour les tests


def refresh_soon(on_change):
    """Relit les partages en arrière-plan (au plus toutes les 10 s) ; on_change() si la liste a changé
    (emblèmes « partagé » à redessiner). Jamais dans le fil de l'interface : `net` peut être lent."""
    global _refreshing
    if _refreshing or not usershare.available() or not usershare.stale():
        return
    _refreshing = True
    before = {p: s.name for p, s in usershare.cached().items()}

    def work():
        global _refreshing
        try:
            usershare.refresh()
        except usershare.ShareError:
            usershare.mark_read()
        finally:
            _refreshing = False
        if {p: s.name for p, s in usershare.cached().items()} != before:
            GLib.idle_add(lambda: (on_change(), False)[1])
    threading.Thread(target=work, daemon=True).start()


def error_text(e: usershare.ShareError) -> str:
    """Message à afficher : la marche à suivre pour les deux cas courants sous Ubuntu / Zorin."""
    if e.kind == "group":
        return (tr("Pour partager des dossiers, votre compte doit faire partie du groupe « sambashare ». "
                   "Tapez dans un terminal :") + "\n\nsudo usermod -aG sambashare $USER\n\n"
                + tr("puis fermez la session et reconnectez-vous."))
    if e.kind == "service":
        return (tr("Le service de partage de Samba (smbd) n'est pas lancé. Tapez dans un terminal :")
                + "\n\nsudo systemctl enable --now smbd")
    return str(e)


class ShareDialog:
    def __init__(self, window, path: str):
        global _last
        _last = self
        self.window = window
        self.path = os.path.normpath(path)
        self.old = None
        read_error = None
        try:
            self.old = usershare.refresh().get(self.path)
        except usershare.ShareError as e:
            read_error = e
        self.guests = usershare.guests_allowed()
        label = tr("Modifier le partage") if self.old else tr("Créer un partage")
        self.dialog, page, self.action = dialog_shell(tr("Partage de dossier"), label, 560, 470)
        g = Adw.PreferencesGroup(title=os.path.basename(self.path) or self.path, description=self.path)
        self.share_row = Adw.SwitchRow(title=tr("Partager ce dossier"), active=self.old is not None)
        g.add(self.share_row)
        self.name_row = Adw.EntryRow(title=tr("Nom du partage"))
        self.name_row.set_text(self.old.name if self.old else os.path.basename(self.path) or "share")
        g.add(self.name_row)
        self.comment_row = Adw.EntryRow(title=tr("Commentaire"))
        self.comment_row.set_text(self.old.comment if self.old else "")
        g.add(self.comment_row)
        self.write_row = Adw.SwitchRow(
            title=tr("Permettre à d'autres de créer et supprimer des fichiers dans ce dossier"),
            active=bool(self.old and self.old.writable))
        self.write_row.set_title_lines(2)
        g.add(self.write_row)
        self.guest_row = Adw.SwitchRow(
            title=tr("Accès invité"),
            subtitle=tr("Pour les personnes sans compte utilisateur") if self.guests
            else tr("Désactivé dans la configuration de Samba"),
            active=bool(self.old and self.old.guest_ok and self.guests))
        g.add(self.guest_row)
        page.add(g)
        self.status = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False, margin_start=12,
                                margin_end=12)
        page.add(self._wrap(self.status))
        for row, sig in ((self.share_row, "notify::active"), (self.name_row, "changed"),
                         (self.write_row, "notify::active"), (self.guest_row, "notify::active")):
            row.connect(sig, lambda *_: self._sync())
        self.action.connect("clicked", lambda *_: self._go())
        self._sync()
        if read_error is not None:
            self._show(error_text(read_error), "error")

    @staticmethod
    def _wrap(widget) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup()
        g.add(widget)
        return g

    def present(self, parent):
        self.dialog.present(parent)

    def _show(self, text: str, kind: str = ""):
        for c in ("error", "warning"):
            self.status.remove_css_class(c)
        if kind:
            self.status.add_css_class(kind)
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def _sync(self):
        on = self.share_row.get_active()
        for r in (self.name_row, self.comment_row, self.write_row):
            r.set_sensitive(on)
        self.guest_row.set_sensitive(on and self.guests)
        if on:
            err, warn = usershare.validate_name(self.name_row.get_text(), self.path)
            self._show(err or warn, "error" if err else "warning" if warn else "")
            self.action.set_sensitive(not err)
        else:
            self._show("")
            self.action.set_sensitive(self.old is not None)      # arrêter un partage existant

    def _wanted(self):
        if not self.share_row.get_active():
            return None
        return usershare.Share(self.name_row.get_text().strip(), self.path, self.comment_row.get_text().strip(),
                               self.write_row.get_active(), self.guests and self.guest_row.get_active())

    def _go(self):
        new = self._wanted()
        bits = usershare.missing_bits(self.path, new.guest_ok, new.writable) if new else 0
        if not bits:
            self._apply(new, 0)
            return
        name = os.path.basename(self.path)
        body = (tr("Le dossier « {0} » a besoin des permissions supplémentaires suivantes pour que le partage "
                   "fonctionne :").format(name) + "\n"
                + "\n".join("– " + d for d in usershare.describe_bits(bits)) + "\n\n"
                + tr("Voulez-vous qu'Odysseus ajoute automatiquement ces permissions au dossier ?"))
        alert(self.dialog,
              tr("Odysseus doit ajouter des permissions à votre dossier « {0} » afin de pouvoir le partager")
              .format(name), body,
              responses=(("cancel", tr("Annuler")), ("add", tr("Ajouter les permissions automatiquement"))),
              default="add", suggested="add", callback=lambda r: r == "add" and self._apply(new, bits))

    def _apply(self, new, bits: int):
        try:
            if bits:
                usershare.add_bits(self.path, bits)
            usershare.apply(self.old, new, self.guests)
        except usershare.ShareError as e:
            if bits:
                usershare.restore_bits(self.path, usershare.needed_after(self.old))
            self._show(error_text(e), "error")
            return
        usershare.restore_bits(self.path, usershare.needed_after(new))    # écriture retirée, arrêt
        self.dialog.close()
        app = self.window.get_application()
        for w in app.get_windows() if app else []:
            for p in getattr(w, "all_panes", lambda: [])():
                p.refresh_rows()
        toast(self.window, tr("Dossier partagé sur le réseau : « {0} »").format(new.name) if new
              else tr("Le dossier n'est plus partagé"))
