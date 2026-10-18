"""Outils issus des extensions : recherche de contenu, PDF, audio, annotation, paquets .deb."""

from __future__ import annotations

import os

from .. import features
from ..backend import media
from ..i18n import tr
from .common import alert


class ToolActions:
    def setup_tool_actions(self):
        acts = {
            "content-search": self.content_search, "content-search-selected": self.content_search_selected,
            "pdf-compress": lambda: self._pdf("compress"), "pdf-merge": lambda: self._pdf("merge"),
            "pdf-watermark": lambda: self._pdf("watermark"), "pdf-pages": lambda: self._pdf("pages"),
            "video-to-audio": self.video_to_audio, "annotate": self.annotate, "deb-install": self.deb_install,
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
                  tr("« {0} » a besoin de : {1}.\nInstallez-le avec :\nsudo apt install {2}").format(
                      f.title, ", ".join(missing), f.packages))
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

    def annotate(self):
        if not self._need("annotate"):
            return
        sel = [r for r in self.pane.selected_recs() if r.path and r.content_type.startswith("image/")]
        if sel:
            from .annotate import AnnotateWindow
            AnnotateWindow(self, sel[0].path).present()

    def deb_install(self):
        if not self._need("deb_installer"):
            return
        sel = [r for r in self.pane.selected_recs() if r.path and r.name.lower().endswith(".deb")]
        if sel:
            from .deb_dialog import DebInstallerDialog
            DebInstallerDialog(self, sel[0].path).present(self)
