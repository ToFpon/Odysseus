"""Résultat de la vérification d'un fichier d'empreintes (.sha256, .md5, .sfv, SHA256SUMS…)."""

from __future__ import annotations

import os

from gi.repository import Adw, Gtk

from ..i18n import ntr, tr
from .common import DialogWindow, shown

STATUS = {
    "ok": ("emblem-ok-symbolic", "success"),
    "mismatch": ("dialog-error-symbolic", "error"),
    "missing": ("dialog-question-symbolic", "warning"),
    "unreadable": ("dialog-warning-symbolic", "warning"),
}


def status_label(status: str) -> str:
    return {"ok": tr("Correct"), "mismatch": tr("Différent : fichier abîmé ou modifié"),
            "missing": tr("Absent"), "unreadable": tr("Illisible")}.get(status, status)


class ChecksumResults(DialogWindow):
    def __init__(self, checksum_file: str, results: list[dict]):
        super().__init__(title=tr("Sommes de contrôle"), content_width=560, content_height=520)
        self.results = results
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        bad = [r for r in results if r["status"] != "ok"]
        if not results:
            title, icon, cls = tr("Aucune empreinte trouvée dans ce fichier"), "dialog-question-symbolic", "warning"
        elif not bad:
            title = ntr("{0} fichier vérifié : intact", "{0} fichiers vérifiés : tous intacts",
                        len(results)).format(len(results))
            icon, cls = "emblem-ok-symbolic", "success"
        else:
            title = ntr("{0} problème sur {1} fichier", "{0} problèmes sur {1} fichiers",
                        len(bad)).format(len(bad), len(results))
            icon, cls = "dialog-error-symbolic", "error"
        self.summary = title
        head = Adw.PreferencesGroup()
        status = Adw.ActionRow(title=title, subtitle=shown(os.path.basename(checksum_file)), css_classes=[cls])
        status.add_prefix(Gtk.Image(icon_name=icon, pixel_size=32))
        head.add(status)
        page.add(head)
        if results:
            base = os.path.dirname(os.path.abspath(checksum_file))
            g = Adw.PreferencesGroup()
            # les problèmes d'abord : c'est ce qu'on cherche dans une longue liste
            for r in sorted(results, key=lambda r: r["status"] == "ok"):
                name = os.path.relpath(r["path"], base) if r["path"].startswith(base) else r["path"]
                row = Adw.ActionRow(title=shown(name), subtitle=f"{r['algo']} · {status_label(r['status'])}")
                row.set_title_lines(1)
                ic, c = STATUS.get(r["status"], STATUS["unreadable"])
                img = Gtk.Image(icon_name=ic)
                img.add_css_class(c)
                row.add_suffix(img)
                if r["status"] == "mismatch":
                    row.set_tooltip_text(tr("Attendu : {0}\nCalculé : {1}").format(r["expected"], r["actual"]))
                g.add(row)
            page.add(g)
        tv.set_content(page)
        self.set_child(tv)
