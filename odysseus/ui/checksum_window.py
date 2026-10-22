"""Fenêtre « Empreintes » d'un fichier : ses sommes de contrôle, et la comparaison avec l'empreinte
publiée par le site de téléchargement. Fenêtre à part (non modale), comme la fenêtre de
progression : les Propriétés gardent leur seul rôle, et on peut lire le fichier pendant le calcul.

Pas de bouton « Comparer » : la comparaison part dès que le texte du champ contient une empreinte.
Le verdict se lit sur la pastille ronde (grise pendant le calcul, verte, rouge, orange)."""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ..backend import tools
from ..backend.engine import Job, human_size
from ..i18n import tr
from .common import copy_to_clipboard, shown, toast

# calculés à l'ouverture, en une seule lecture du fichier
ALGOS = ("MD5", "SHA-1", "SHA-256", "SHA-512")

VERDICTS = {"busy": "content-loading-symbolic", True: "object-select-symbolic",
            False: "window-close-symbolic", "unknown": "dialog-question-symbolic"}


class ChecksumWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, path: str):
        super().__init__(title=tr("Empreintes"), default_width=560, default_height=660,
                         transient_for=parent)
        self.path = path
        self.digests: dict[str, str] = {}       # algorithme → empreinte, une fois calculées
        self.verdict = None                      # None, "busy", True, False, "unknown"
        self._job = None                         # calcul des empreintes de ALGOS
        self._extra = None                       # calcul d'autres algorithmes (SHA3, BLAKE2…)
        self._compared = None                    # texte déjà comparé (un « changed » sans changement ne relance rien)
        self._timer = 0

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.toast_overlay = Adw.ToastOverlay()
        page = Adw.PreferencesPage()

        head = Adw.PreferencesGroup()
        try:
            size = human_size(os.path.getsize(path))
        except OSError:
            size = ""
        info = Adw.ActionRow(title=shown(os.path.basename(path)), subtitle=size)
        info.set_title_lines(2)
        ctype = Gio.content_type_guess(path, None)[0]
        info.add_prefix(Gtk.Image(gicon=Gio.content_type_get_icon(ctype), pixel_size=32))
        head.add(info)
        page.add(head)

        # textes d'aide : à l'ouverture, un novice ne sait pas ce que la fenêtre calcule déjà
        cmp = Adw.PreferencesGroup(
            title=tr("Comparer avec l'empreinte publiée"),
            description=tr("Copiez l'empreinte indiquée sur le site où vous avez téléchargé ce fichier, "
                           "puis collez-la ci-dessous : le verdict s'affiche tout seul."))
        self.entry = Adw.EntryRow(title=tr("Collez ici l'empreinte du site de téléchargement"))
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("entry-activated", lambda *_: self._compare())
        cmp.add(self.entry)
        self.result = Adw.ActionRow(visible=False, subtitle_selectable=True)
        self.result.set_subtitle_lines(3)
        self.badge = Gtk.Image(icon_name=VERDICTS["busy"], valign=Gtk.Align.CENTER,
                               css_classes=["verdict-badge"])
        self.result.add_prefix(self.badge)
        cmp.add(self.result)
        page.add(cmp)

        sums = Adw.PreferencesGroup(title=tr("Empreintes du fichier"),
                                    description=tr("Lecture du fichier pour calculer ses empreintes…"))
        self.sums = sums
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
        sums.set_header_suffix(self.bar)
        self.rows: dict[str, Adw.ActionRow] = {}
        for algo in ALGOS:
            row = Adw.ActionRow(title=algo, subtitle=tr("Calcul…"), subtitle_selectable=True)
            row.add_css_class("property")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, sensitive=False,
                              css_classes=["flat"], tooltip_text=tr("Copier"))
            copy.connect("clicked", self._copy, algo)
            row.add_suffix(copy)
            row._copy = copy
            self.rows[algo] = row
            sums.add(row)
        page.add(sums)

        self.toast_overlay.set_child(page)
        tv.set_content(self.toast_overlay)
        self.set_content(tv)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(keys)
        self.connect("close-request", self._on_close)
        self._start()

    # ------------------------------------------------------------------
    def _run(self, job: Job, done):
        """Lance le calcul dans un fil ; la barre suit la progression ; done(job) dans la boucle GTK."""
        pending = []

        def moved(j):
            if not pending:                     # une mise à jour à la fois, pas une par Mio lu
                pending.append(1)

                def show():
                    pending.clear()
                    self.bar.set_fraction(j.fraction)
                    return False
                GLib.idle_add(show)
        job.subscribe(moved)
        self.bar.set_visible(True)
        self.bar.set_fraction(0)

        def work():
            job.run_sync()
            GLib.idle_add(lambda: (done(job), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        self._job = Job("hash", lambda j: tools.hash_files(j, [self.path], list(ALGOS))[0])
        self._run(self._job, self._hashed)

    def _hashed(self, job):
        if job is not self._job:
            return
        self._job = None
        self.bar.set_visible(False)
        self.sums.set_description(tr("Calculées à partir du fichier sur ce disque. Celle qui correspond "
                                     "à l'empreinte collée passe en vert.") if job.state == "done" else "")
        if job.state != "done":
            for row in self.rows.values():
                row.set_subtitle(job.error or tr("Annulé"))
            if self.verdict == "busy":
                self._show(False, tr("Calcul impossible"), job.error or "")
            return
        for algo in ALGOS:
            self.digests[algo] = job.result[algo]
            self.rows[algo].set_subtitle(job.result[algo])
            self.rows[algo]._copy.set_sensitive(True)
        self._compared = None                   # le verdict attendait ces empreintes
        self._compare()

    def _copy(self, _button, algo):
        copy_to_clipboard(self, self.digests[algo])
        toast(self, tr("{0} copié").format(algo))

    # ------------------------------------------------------------------
    def _on_changed(self, *_):
        # petite attente : un collage ou une frappe rapide ne lance qu'une comparaison
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(250, self._timer_done)

    def _timer_done(self):
        self._timer = 0
        self._compare()
        return False

    def _show(self, verdict, title: str, subtitle: str = ""):
        self.verdict = verdict
        self.result.set_visible(verdict is not None)
        self.result.set_title(title)
        self.result.set_subtitle(subtitle)
        for c in ("busy", "ok", "ko", "unknown"):
            self.badge.remove_css_class(c)
        if verdict is not None:
            self.badge.add_css_class({"busy": "busy", True: "ok", False: "ko", "unknown": "unknown"}[verdict])
            self.badge.set_from_icon_name(VERDICTS[verdict])

    def _mark(self, algo=None):
        for name, row in self.rows.items():
            (row.add_css_class if name == algo else row.remove_css_class)("success")

    def _compare(self):
        text = self.entry.get_text()
        if text == self._compared:
            return
        self._compared = text
        if self._extra is not None:
            self._extra.cancel()
            self._extra = None
        self._mark(None)
        if not text.strip():
            self._show(None, "")
            return
        expected = tools.expected_digest(text)
        if expected is None:
            self._show("unknown", tr("Empreinte non reconnue"),
                       tr("Collez l'empreinte publiée (MD5, SHA-1, SHA-256, SHA-512…), seule ou avec le nom du fichier"))
            return
        candidates = tools._DIGEST_CANDIDATES[len(expected)]
        match = next((a for a in candidates if self.digests.get(a) == expected), None)
        if match:
            self._mark(match)
            self._show(True, tr("Correspond ({0})").format(match), tr("Le fichier est intact"))
            return
        if self._job is not None:               # empreintes pas encore calculées : _hashed relancera
            self._show("busy", tr("Calcul…"))
            return
        missing = [a for a in candidates if a not in self.digests]
        if not missing:
            self._mismatch(candidates)
            return
        # longueur partagée avec un algorithme rare (SHA3, BLAKE2) : on le calcule aussi
        self._show("busy", tr("Calcul…"))
        job = Job("hash", lambda j: tools.hash_files(j, [self.path], missing)[0])
        self._extra = job

        def done(job):
            if job is not self._extra:
                return
            self._extra = None
            self.bar.set_visible(False)
            if job.state != "done":
                self._show(False, tr("Calcul impossible"), job.error or "")
                return
            for a in missing:
                self.digests[a] = job.result[a]
            self._compared = None
            self._compare()
        self._run(job, done)

    def _mismatch(self, candidates):
        algo = candidates[0]                    # le plus courant pour cette longueur
        self._show(False, tr("Ne correspond pas : le fichier est différent ou abîmé"),
                   tr("{0} calculé : {1}").format(algo, self.digests.get(algo, "")))

    def _on_close(self, *_):
        for job in (self._job, self._extra):
            if job is not None:
                job.cancel()
        self._job = self._extra = None
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        return False
