"""« Métadonnées des photos… » (fonctionnalité `photo_metadata`, moteur backend/photometa.py).

Ce que révèlent les photos choisies (position, lieu, appareil, numéro de série, date, auteur), puis :
retirer la position (coordonnées GPS et lieu écrit en clair), retirer toutes les métadonnées (orientation
gardée), modifier auteur / copyright / description (« valeurs différentes » laissées telles quelles, comme
les étiquettes audio), décaler l'heure de prise de vue (horloge de l'appareil, fuseau horaire). Remplacer
les originaux (Ctrl+Z les remet) ou créer des copies. L'image elle-même n'est jamais retouchée.
"""

from __future__ import annotations

import os
import threading

from gi.repository import Adw, GLib, Gtk

from ..backend import photometa
from ..i18n import ntr, tr
from .common import combo_row, dialog_shell, shown

_last = None          # pour les tests


class PhotoMetaDialog:
    def __init__(self, window, paths: list[str]):
        global _last
        _last = self
        self.window = window
        self.paths = sorted(paths)
        self.infos: list = []
        self.initial: dict = {}
        self.entries: dict = {}
        n = len(self.paths)
        self.dialog, page, self.action = dialog_shell(tr("Métadonnées des photos"), tr("Appliquer"), 560, 720)
        self.action.set_sensitive(False)
        self.action.connect("clicked", lambda *_: self._apply())

        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Image(icon_name="mark-location-symbolic", pixel_size=48, css_classes=["dim-label"]))
        title = os.path.basename(self.paths[0]) if n == 1 else ntr("{0} photo", "{0} photos", n).format(n)
        box.append(Gtk.Label(label=shown(title), css_classes=["title-3"], wrap=True,
                             justify=Gtk.Justification.CENTER, max_width_chars=40))
        self.reveal = Gtk.Label(label=tr("Lecture…"), wrap=True, justify=Gtk.Justification.CENTER,
                                css_classes=["dim-label"], max_width_chars=50)
        box.append(self.reveal)
        head.add(box)
        page.add(head)

        privacy = Adw.PreferencesGroup(title=tr("Confidentialité"))
        self.strip_gps = Adw.SwitchRow(title=tr("Retirer la position"),
                                       subtitle=tr("Coordonnées GPS et lieu écrit en clair (ville, pays…)"))
        self.strip_all = Adw.SwitchRow(title=tr("Retirer toutes les métadonnées"),
                                       subtitle=tr("Appareil, numéro de série, logiciel, dates, auteur… "
                                                   "L'orientation de la photo est gardée."))
        for row in (self.strip_gps, self.strip_all):
            row.connect("notify::active", lambda *_: self._sync())
            privacy.add(row)
        page.add(privacy)

        info = Adw.PreferencesGroup(title=tr("Informations"))
        for key, label in (("author", tr("Auteur")), ("copyright", tr("Copyright")),
                           ("description", tr("Description"))):
            row = Adw.EntryRow(title=label)
            row._label = label
            row.connect("changed", lambda *_: self._sync())
            self.entries[key] = row
            info.add(row)
        page.add(info)

        self.time_group = Adw.PreferencesGroup(
            title=tr("Heure de prise de vue"),
            description=tr("Pour corriger l'horloge de l'appareil, ou l'heure d'un voyage dans un autre fuseau"))
        self.hours = Adw.SpinRow.new_with_range(-48, 48, 1)
        self.hours.set_title(tr("Décaler de (heures)"))
        self.minutes = Adw.SpinRow.new_with_range(-59, 59, 1)
        self.minutes.set_title(tr("Décaler de (minutes)"))
        for row in (self.hours, self.minutes):
            row.set_value(0)
            row.connect("notify::value", lambda *_: self._sync())
            self.time_group.add(row)
        page.add(self.time_group)

        out = Adw.PreferencesGroup(title=tr("Enregistrement"))
        self.mode = combo_row(tr("Photos modifiées"), [tr("Remplacer les originaux (Ctrl+Z pour revenir)"),
                                                     tr("Créer des copies à côté")], 0)
        out.add(self.mode)
        page.add(out)
        for w in (self.strip_gps, self.strip_all, *self.entries.values(), self.hours, self.minutes):
            w.set_sensitive(False)
        threading.Thread(target=self._load, daemon=True).start()

    def present(self, parent):
        self.dialog.present(parent)

    # ------------------------------------------------------------------
    def _load(self):
        infos, errors = [], []
        for p in self.paths:
            try:
                infos.append(photometa.summary(p))
            except Exception as e:  # noqa: BLE001 — photo abîmée : signalée, écartée
                errors.append((p, str(e)))
        GLib.idle_add(self._loaded, infos, errors)

    def _loaded(self, infos, errors):
        self.infos = infos
        bad = {p for p, _e in errors}
        self.paths = [p for p in self.paths if p not in bad]
        n = len(infos)
        with_gps = sum(1 for i in infos if i.get("gps") or i.get("place"))
        if n == 1:
            found = photometa.describe(infos[0])
            text = (tr("Cette photo révèle : {0}.").format(", ".join(found)) if found
                    else tr("Cette photo ne contient presque aucune métadonnée."))
        elif n:
            text = ntr("{0} photo sur {1} contient une position.", "{0} photos sur {1} contiennent une position.",
                       with_gps).format(with_gps, n) if with_gps else tr("Aucune de ces photos ne contient de position.")
        else:
            text = tr("Aucune photo lisible.")
        if errors:
            text += "\n" + tr("Illisible : {0}").format(", ".join(shown(os.path.basename(p)) for p, _e in errors[:3]))
        self.reveal.set_label(text)
        self.strip_gps.set_active(with_gps > 0)
        for key, row in self.entries.items():
            vals = {i.get(key, "") for i in infos}
            common = vals.pop() if len(vals) == 1 else None
            self.initial[key] = common or ""
            row.set_text(common or "")
            row.set_title(row._label if common is not None else tr("{0} (valeurs différentes)").format(row._label))
        if n:
            for w in (self.strip_gps, self.strip_all, *self.entries.values(), self.hours, self.minutes):
                w.set_sensitive(True)
        self._sync()
        return False

    def fields(self) -> dict:
        return {k: e.get_text().strip() for k, e in self.entries.items()
                if e.get_text().strip() != self.initial.get(k, "")}

    def shift(self) -> int:
        return int(self.hours.get_value()) * 3600 + int(self.minutes.get_value()) * 60

    def _sync(self):
        everything = self.strip_all.get_active()
        # tout retirer emporte aussi la position et les dates : ces réglages n'ont plus d'objet
        self.strip_gps.set_sensitive(bool(self.infos) and not everything)
        self.time_group.set_sensitive(bool(self.infos) and not everything)
        dirty = (self.strip_gps.get_active() or everything or bool(self.fields())
                 or (self.shift() != 0 and not everything))
        self.action.set_sensitive(dirty and bool(self.paths))

    def options(self) -> dict:
        everything = self.strip_all.get_active()
        return {"strip_gps": self.strip_gps.get_active() and not everything, "strip_all": everything,
                "fields": self.fields(), "shift": 0 if everything else self.shift()}

    def _apply(self):
        opts = self.options()
        copies = self.mode.get_selected() == 1
        suffix = (tr("sans métadonnées") if opts["strip_all"] else
                  tr("sans position") if opts["strip_gps"] else tr("modifiée"))
        paths = list(self.paths)
        self.dialog.close()
        self.window.write_photo_meta(paths, opts, suffix if copies else None)
