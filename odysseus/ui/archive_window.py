"""Fenêtre d'archive à part, comme l'extension Archive Browser (et File Roller avant qu'il ne sache plus
le faire avec Nautilus) : un double-clic sur une archive l'ouvre dans sa propre fenêtre, la fenêtre
d'Odysseus garde son dossier, et n'importe quel élément de l'archive se glisse vers n'importe quel
dossier (Odysseus, Nautilus, le bureau…).

C'est une fenêtre d'Odysseus allégée (sans barre latérale) : navigation dans l'archive, extraction,
ajout, renommage, mot de passe, glisser (Pane en mode archive) sont ceux de la fenêtre principale.
Quitter l'archive ferme la fenêtre ; un autre emplacement choisi (barre de chemin) s'ouvre dans la fenêtre
principale."""

from __future__ import annotations

import os

from gi.repository import GLib

from .window import MainWindow


class ArchiveWindow(MainWindow):
    __gtype_name__ = "OdysseusArchiveWindow"
    saves_state = False          # ses réglages de fenêtre ne remplacent pas ceux de la fenêtre principale
    is_archive_window = True

    def __init__(self, app, path: str):
        self.archive_file = os.path.abspath(path)
        super().__init__(app, self.archive_file, dual=False)
        self.set_default_size(920, 620)
        self.sidebar_btn.set_active(False)
        self.add_css_class("archive-window")
        self.archive_pane = self.pane
        self.archive_pane.connect("location-changed", self._on_archive_location)

    def _on_archive_location(self, pane):
        """Archive quittée (barre de chemin, dossier parent, « Fermer l'archive ») : l'emplacement
        choisi s'ouvre dans la fenêtre principale, et cette fenêtre se ferme."""
        if pane.mode == "archive" or getattr(pane, "_retired", False):
            return
        loc = pane.location
        GLib.idle_add(self._hand_over, loc)

    def _hand_over(self, loc):
        main = [w for w in self.get_application().get_windows()
                if isinstance(w, MainWindow) and not w.is_archive_window]
        if loc and loc != os.path.dirname(self.archive_file):
            if main:
                main[0].open_location(loc)
                main[0].present()
            else:
                MainWindow(self.get_application(), loc).present()
        elif not main:
            MainWindow(self.get_application(), loc).present()
        self.close()
        return False


def open_archive_window(app, path: str):
    """Une fenêtre par archive : si elle est déjà ouverte, elle revient devant."""
    path = os.path.abspath(path)
    for w in app.get_windows():
        if isinstance(w, ArchiveWindow) and w.archive_file == path and w.archive_pane.mode == "archive":
            w.present()
            return w
    w = ArchiveWindow(app, path)
    w.present()
    return w
