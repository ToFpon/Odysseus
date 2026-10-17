"""Vidéo vers audio (reprise de Video to Audio) : format, qualité, dossier, état de chaque fichier."""

from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from ..backend import media
from ..i18n import ntr, tr
from ..settings import settings
from .common import choose_folder, combo_row, dialog_shell
from .jobs import finished_toast, manager

QUALITY_LABELS = [tr("Haute (320 kb/s)"), tr("Moyenne (192 kb/s)"), tr("Basse (128 kb/s)"),
                  tr("Copie sans réencodage")]


class VideoToAudioDialog:
    def __init__(self, window, videos: list[str]):
        self.window = window
        self.videos = videos
        self.dialog, page, self.action = dialog_shell(tr("Extraire l'audio"), tr("Convertir"), 560, 620)
        g = Adw.PreferencesGroup(title=tr("Réglages"))
        fmts = [f for f, _c in media.AUDIO_FORMATS]
        cur = settings["audio_format"]
        self.fmt = combo_row(tr("Format"), [f.upper() for f in fmts], fmts.index(cur) if cur in fmts else 0)
        g.add(self.fmt)
        q = settings["audio_quality"]
        self.quality = combo_row(tr("Qualité"), QUALITY_LABELS,
                                 media.QUALITIES.index(q) if q in media.QUALITIES else 0)
        g.add(self.quality)
        self.dest = ""
        self.dest_row = Adw.ActionRow(title=tr("Dossier de destination"), subtitle=tr("À côté de chaque vidéo"))
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b.connect("clicked", lambda *_: choose_folder(self.dialog, tr("Dossier de destination"),
                                                      os.path.dirname(videos[0]), self._set_dest))
        self.dest_row.add_suffix(b)
        g.add(self.dest_row)
        self.fmt.connect("notify::selected", lambda *_: self._sync())
        page.add(g)
        self.files = Adw.PreferencesGroup(title=ntr("{0} fichier", "{0} fichiers", len(videos)).format(len(videos)))
        self.status = []
        for v in videos:
            r = Adw.ActionRow(title=os.path.basename(v))
            r.set_title_lines(1)
            r.add_prefix(Gtk.Image(icon_name="video-x-generic-symbolic"))
            st = Gtk.Image(icon_name="content-loading-symbolic", css_classes=["dim-label"])
            r.add_suffix(st)
            self.status.append(st)
            self.files.add(r)
        page.add(self.files)
        self.action.connect("clicked", self._go)
        self._sync()

    def _set_dest(self, path):
        self.dest = path
        self.dest_row.set_subtitle(path)

    def _sync(self):
        lossless = media.AUDIO_FORMATS[self.fmt.get_selected()][0] in media.LOSSLESS
        self.quality.set_sensitive(not lossless)

    def _go(self, *_):
        fmt = media.AUDIO_FORMATS[self.fmt.get_selected()][0]
        quality = media.QUALITIES[self.quality.get_selected()]
        settings["audio_format"] = fmt
        settings["audio_quality"] = quality
        self.action.set_sensitive(False)
        status = self.status

        def mark(i, ok):
            def do():
                status[i].set_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
                status[i].remove_css_class("dim-label")
                status[i].add_css_class("success" if ok else "error")
                return False
            GLib.idle_add(do)
        videos, dest = list(self.videos), self.dest

        def done(job):
            n = sum(1 for _o, ok in (job.result or []) if ok)
            finished_toast(self.window, job, tr("{0} sur {1} fichiers convertis").format(n, len(videos)),
                           dest or os.path.dirname(videos[0]))
            self.dialog.close()
        job = manager.submit(ntr("Extraction audio de {0} vidéo", "Extraction audio de {0} vidéos",
                                 len(videos)).format(len(videos)),
                             lambda j: media.extract_audio(j, videos, fmt, quality, dest, on_file=mark), done)
        self.window._watch_progress(job)

    def present(self, parent):
        self.dialog.present(parent)
