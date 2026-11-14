"""Outils issus des extensions : recherche de contenu, PDF, audio, annotation, paquets .deb."""

from __future__ import annotations

import os

from gi.repository import Gio, GLib, Gtk

from .. import features
from ..backend import fileops, media
from ..backend.engine import ToolError
from ..i18n import ntr, tr
from ..settings import settings
from .common import alert, toast
from .jobs import finished_toast, manager


class ToolActions:
    def setup_tool_actions(self):
        acts = {
            "content-search": self.content_search, "content-search-selected": self.content_search_selected,
            "pdf-compress": lambda: self._pdf("compress"), "pdf-merge": lambda: self._pdf("merge"),
            "pdf-watermark": lambda: self._pdf("watermark"), "pdf-pages": lambda: self._pdf("pages"),
            "video-to-audio": self.video_to_audio, "annotate": self.annotate, "deb-install": self.deb_install,
            "image-resize": lambda: self.image_tools("resize"), "image-rotate": lambda: self.image_tools("rotate"),
            "image-convert": lambda: self.image_tools("convert"),
            "verify-checksums": self.verify_checksums, "checksums": self.checksums,
            "playlist-create": self.playlist_create, "playlist-add": self.playlist_add,
            "playlist-add-other": self.playlist_add_other, "edit-tags": self.edit_tags,
        }
        for name, cb in acts.items():
            self._add(name, cb)

    def tool_accels(self) -> dict:
        return {"win.content-search": ["F8"]}

    def _need(self, fid: str) -> bool:
        if not features.enabled(fid):
            return False
        missing = features.missing_tools(fid)
        if missing and not features.available(fid):
            f = features.BY_ID[fid]
            alert(self, tr("Programme manquant"),
                  tr("« {0} » a besoin de : {1}.\nInstallez-le avec :\n{2}").format(
                      f.title, ", ".join(missing), features.install_command(fid)))
            return False
        return True

    def content_search(self, folder: str = ""):
        if not self._need("content_search"):
            return
        from .search_dialog import ContentSearchWindow
        ContentSearchWindow(self, folder or self.pane.current_dir() or "").present()

    def content_search_selected(self):
        dirs = [r.path for r in self.pane.selected_recs() if r.is_dir and r.path]
        self.content_search(dirs[0] if dirs else "")

    def _pdf(self, tool: str):
        if not self._need("pdf_" + tool):
            return
        paths = [r.path for r in self.pane.selected_recs() if r.path and r.name.lower().endswith(".pdf")]
        if not paths and tool != "merge":
            return
        from . import pdf_dialogs
        from ..backend import pdf as pdfmod
        if tool in ("watermark", "pages"):
            # faux PDF ou PDF abîmé : le dialogue ne s'ouvrait pas et rien n'était dit
            for p in paths:
                try:
                    ok = pdfmod.is_pdf(p) and pdfmod.page_count(p) > 0
                except Exception:  # noqa: BLE001 — pypdf / qpdf lèvent des erreurs variées
                    ok = False
                if not ok:
                    alert(self, tr("PDF illisible"),
                          tr("« {0} » n'est pas un PDF valide ou il est endommagé.").format(os.path.basename(p)))
                    return
        try:
            {"compress": pdf_dialogs.CompressDialog, "merge": pdf_dialogs.MergeDialog,
             "watermark": pdf_dialogs.WatermarkDialog, "pages": pdf_dialogs.PagesDialog}[tool](self, paths).present(self)
        except Exception as e:  # noqa: BLE001
            alert(self, tr("PDF illisible"), str(e))

    def video_to_audio(self):
        if not self._need("video_to_audio"):
            return
        paths = [r.path for r in self.pane.selected_recs() if r.path and media.is_video(r.name)]
        if paths:
            from .audio_dialog import VideoToAudioDialog
            VideoToAudioDialog(self, paths).present(self)

    def image_tools(self, mode: str):
        if not self._need("image_tools"):
            return
        from ..backend import images
        paths = [r.path for r in self.pane.selected_recs() if r.path and not r.is_dir and images.is_image(r.name)]
        if paths:
            from .image_dialog import ImageToolsDialog
            ImageToolsDialog(self, paths, mode).present(self)

    def edit_tags(self):
        if not self._need("tag_editor"):
            return
        from ..backend import tags
        paths = [r.path for r in self.pane.selected_recs() if r.path and not r.is_dir and tags.supported(r.name)]
        if paths:
            from .tag_dialog import TagDialog
            TagDialog(self, paths).present(self)

    def write_tags(self, paths: list, common: dict, cover, numbered: bool):
        """Écrit les étiquettes (fil, progression) ; une entrée d'annulation avec les valeurs d'avant."""
        from ..backend import tags
        n = len(paths)

        def work(job):
            res = fileops.Result("tags")
            res.count = 0
            changed = []
            for i, p in enumerate(paths):
                job.check_cancel()
                job.progress(i / max(n, 1), os.path.basename(p))
                changes = dict(common)
                if numbered:
                    changes["tracknumber"] = [str(i + 1)]
                try:
                    before = tags.snapshot(p)
                    tags.write(p, changes, cover)
                    changed.append((p, before, tags.snapshot(p)))
                    res.count += 1
                except Exception as e:  # noqa: BLE001 — un fichier en échec n'arrête pas les autres
                    res.errors.append((p, str(e)))
            res.tag_changes = changed
            job.progress(1.0, "")
            return res
        self._run(ntr("Étiquettes de {0} fichier", "Étiquettes de {0} fichiers", n).format(n), work,
                  undo=lambda r: ("tags", r.tag_changes) if getattr(r, "tag_changes", None) else None)

    def annotate(self):
        if not self._need("annotate"):
            return
        sel = [r for r in self.pane.selected_recs() if r.path and r.content_type.startswith("image/")]
        if sel:
            from .annotate import AnnotateWindow
            AnnotateWindow(self, sel[0].path).present()

    def checksums(self):
        """Empreintes du fichier sélectionné et comparaison avec celle publiée, dans une fenêtre
        à part (non modale) : on peut continuer à naviguer pendant le calcul."""
        if not features.enabled("checksums"):
            return
        sel = [r for r in self.pane.selected_recs() if r.path and not r.is_dir]
        if len(sel) == 1 and os.path.isfile(sel[0].path):
            from .checksum_window import ChecksumWindow
            ChecksumWindow(self, sel[0].path).present()

    def verify_checksums(self):
        """Fichier .sha256 / .md5 / .sfv / SHA256SUMS… : vérifie chaque fichier qu'il liste, en
        arrière-plan (progression dans les opérations), puis montre le résultat ligne par ligne."""
        if not features.enabled("checksums"):
            return
        from ..backend import tools
        sel = [r for r in self.pane.selected_recs() if r.path and tools.is_checksum_file(r.name)]
        if not sel:
            return
        path = sel[0].path
        from .jobs import finished_toast, manager

        def done(job):
            if job.state != "done":
                finished_toast(self, job, "")
                return
            from .checksum_dialog import ChecksumResults
            ChecksumResults(path, job.result).present(self)
        job = manager.submit(tr("Vérification de {0}").format(os.path.basename(path)),
                             lambda j: tools.verify_hash_file(j, path), done)
        self._watch_progress(job)

    def deb_install(self):
        if not self._need("deb_installer"):
            return
        sel = [r for r in self.pane.selected_recs() if r.path and r.name.lower().endswith(".deb")]
        if sel:
            from .deb_dialog import DebInstallerDialog
            DebInstallerDialog(self, sel[0].path).present(self)

    # -- listes de lecture (fonctionnalité playlists, backend/playlist.py) ----------------------------
    def _playlist_sources(self) -> list[str]:
        return [r.path for r in self.pane.selected_recs() if r.path]

    def playlist_create(self):
        """Nouvelle liste .m3u dans le dossier affiché (dossier de départ d'une recherche), avec les
        morceaux sélectionnés ou ceux des dossiers sélectionnés."""
        if not features.enabled("playlists"):
            return
        from ..backend import fsinfo, playlist
        from .common import ask_text
        sources = self._playlist_sources()
        if not sources:
            return
        dest = self.pane.current_dir() or os.path.dirname(sources[0])
        dirs = [p for p in sources if os.path.isdir(p)]
        initial = os.path.basename(dirs[0].rstrip("/")) if len(dirs) == 1 and len(sources) == 1 else \
            (os.path.basename(dest.rstrip("/")) or tr("Liste de lecture"))

        def go(name: str):
            name = name.replace("/", "_").strip()
            if not playlist.is_playlist(name):
                name += ".m3u"
            if fsinfo.is_windows_like(dest):
                name = fsinfo.safe_name(name)
            target = os.path.join(dest, name)

            def work(job):
                tracks = playlist.collect(sources, job)
                return playlist.create(job, tracks, target)

            def done(job):
                if job.state == "done":
                    settings["last_playlist"] = job.result
                    toast(self, tr("Liste de lecture créée : {0}").format(os.path.basename(job.result)))
                else:
                    finished_toast(self, job, "")
            manager.submit(tr("Liste de lecture {0}").format(name), work, done)
        ask_text(self, tr("Créer une liste de lecture"), tr("Nom de la liste"), initial, go, tr("Créer"))

    def playlist_add(self, target: str = ""):
        """Ajoute la sélection à la fin d'une liste existante (par défaut la dernière utilisée)."""
        if not features.enabled("playlists"):
            return
        from ..backend import playlist
        sources = self._playlist_sources()
        target = target or settings["last_playlist"]
        if not sources or not target:
            return

        def work(job):
            if not os.path.isfile(target):
                raise ToolError(tr("La liste de lecture n'existe plus : {0}").format(target))
            tracks = playlist.collect(sources, job)
            if not tracks:
                raise ToolError(tr("Aucun morceau de musique dans la sélection"))
            return playlist.append(job, target, tracks)

        def done(job):
            if job.state == "done":
                settings["last_playlist"] = target
                added, already = job.result
                msg = ntr("{0} morceau ajouté à {1}", "{0} morceaux ajoutés à {1}", added).format(
                    added, os.path.basename(target))
                if already:
                    msg += " — " + ntr("{0} déjà dans la liste", "{0} déjà dans la liste", already).format(already)
                toast(self, msg)
            else:
                finished_toast(self, job, "")
        manager.submit(tr("Liste de lecture {0}").format(os.path.basename(target)), work, done)

    def playlist_add_other(self):
        """Choisir la liste (.m3u / .m3u8) à compléter."""
        if not features.enabled("playlists") or not self._playlist_sources():
            return
        dlg = Gtk.FileDialog(title=tr("Ajouter à une liste de lecture"), modal=True)
        flt = Gtk.FileFilter(name=tr("Listes de lecture"))
        for pat in ("*.m3u", "*.m3u8", "*.M3U", "*.M3U8"):
            flt.add_pattern(pat)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dlg.set_filters(filters)
        last = settings["last_playlist"]
        start = os.path.dirname(last) if last else (self.pane.current_dir() or "")
        if start and os.path.isdir(start):
            dlg.set_initial_folder(Gio.File.new_for_path(start))

        def chosen(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self.playlist_add(f.get_path())
        dlg.open(self, None, chosen)
