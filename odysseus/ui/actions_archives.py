"""Archives : parcourir, extraire, compresser, modifier (moteur et dialogues de Pressoir).

Remplace Archive Browser et Extract Here : les deux extensions faisaient en partie la même chose,
elles sont fusionnées ici.
"""

from __future__ import annotations

import os
from typing import Optional

from .. import features
from ..backend import archive, formats
from ..backend.engine import human_size
from ..i18n import ntr, tr
from ..settings import session_passwords, settings
from .common import alert, ask_password, choose_files, describe_paths, open_with_default, toast
from .jobs import finished_toast, manager

PREVIEW_LIMIT = 512 * 1024 * 1024


class ArchiveActions:
    def setup_archive_actions(self):
        acts = {
            "compress": self.compress, "extract-here": lambda: self.extract_quick("smart"),
            "extract-folder": lambda: self.extract_quick("always"), "extract": self.extract_dialog,
            "extract-all": self.extract_dialog, "extract-selected": self.extract_selected,
            "archive-info": self.archive_info, "archive-test": self.archive_test,
            "archive-add": lambda: self.add_to_open_archive(self.pane), "close-archive": self.close_archive,
            "open-archive": self.open_archive_key,
        }
        for name, cb in acts.items():
            self._add(name, cb)

    def archive_accels(self) -> dict:
        return {"win.extract-here": ["<Ctrl><Shift>e"], "win.extract": ["<Ctrl>e"], "win.open-archive": ["F7"],
                "win.archive-info": ["<Ctrl><Alt>Return"]}

    # ------------------------------------------------------------------
    def _selected_archives(self) -> list[str]:
        p = self.pane
        if p.mode == "archive":
            return [p.archive_path]
        return [r.path for r in p.selected_recs() if r.path and not r.is_dir and formats.readable(r.name)]

    def open_archive_key(self):
        """F7 : parcourir l'archive sélectionnée (raccourci d'Archive Browser)."""
        if not features.available("archives"):
            return
        arcs = self._selected_archives()
        if arcs and self.pane.mode != "archive":
            self.pane.open_archive(arcs[0])

    def compress(self, sources: Optional[list] = None):
        if not features.available("archives"):
            return
        p = self.pane
        if sources is None:
            sources = p.selected_paths() if p.mode in ("fs", "search") else []
        if not sources:
            choose_files(self, tr("Choisir les fichiers à compresser"), p.current_dir(),
                         lambda paths: self.compress(paths))
            return
        from .archive_dialogs import CreateDialog
        CreateDialog(self, sources, self._run_create).present(self)

    def _run_create(self, sources, out, o: archive.CreateOptions, each: bool):
        name = os.path.basename(out)
        if each:
            func = lambda j: archive.create_each(j, sources, os.path.dirname(out), o)  # noqa: E731
            title = tr("Compression de {0} archives").format(len(sources))
        else:
            func = lambda j: archive.create(j, sources, out, o)  # noqa: E731
            title = tr("Compression de {name}").format(name=name)

        def done(job):
            folder = os.path.dirname(out)
            if job.state == "done":
                # nom réellement créé : « projet (2).tar.gz » si l'archive existait déjà
                made = job.result if (not each and isinstance(job.result, str)) else out
                size = f" ({human_size(os.path.getsize(made))})" if not each and os.path.exists(made) else ""
                finished_toast(self, job, tr("{name} créée{size}").format(name=os.path.basename(made), size=size)
                               if not each else tr("Archives créées"), folder)
                if o.password and settings["remember_passwords"]:
                    session_passwords[os.path.abspath(made)] = o.password
                skipped = getattr(job, "skipped_files", [])
                if skipped:
                    text = "\n".join(f"{path} : {why}" for path, why in skipped[:15])
                    if len(skipped) > 15:
                        text += "\n" + tr("… et {0} autre(s)").format(len(skipped) - 15)
                    alert(self, ntr("{0} fichier n'a pas pu être lu", "{0} fichiers n'ont pas pu être lus",
                                    len(skipped)).format(len(skipped)),
                          tr("L'archive a été créée sans eux.") + "\n\n" + text)
            else:
                finished_toast(self, job, "")
        self._watch_progress(manager.submit(title, func, done))

    # -- extraction ------------------------------------------------------
    def extract_dialog(self):
        arcs = self._selected_archives()
        if not arcs:
            return
        p = self.pane
        pw = p.password if p.mode == "archive" else session_passwords.get(arcs[0], "")
        from .archive_dialogs import ExtractDialog
        ExtractDialog(self, arcs, self.run_extract, password=pw).present(self)

    def extract_selected(self):
        p = self.pane
        if p.mode != "archive":
            return self.extract_dialog()
        files = [r.get("inner", r.name) for r in p.selected_recs()]
        if not files:
            return self.extract_dialog()
        from .archive_dialogs import ExtractDialog
        ExtractDialog(self, [p.archive_path], self.run_extract, files=files, password=p.password).present(self)

    def extract_quick(self, subfolder: str):
        """« Extraire ici » : dossier intelligent à côté de l'archive (comme Extract Here)."""
        p = self.pane
        arcs = self._selected_archives()
        files = [r.get("inner", r.name) for r in p.selected_recs()] if p.mode == "archive" else []
        for a in arcs:
            o = archive.ExtractOptions(dest=os.path.dirname(a), subfolder="never" if files else subfolder,
                                       overwrite=settings["extract_overwrite"], files=files,
                                       password=(p.password if p.mode == "archive" else "")
                                       or session_passwords.get(a, ""),
                                       open_after=settings["open_after_extract"])
            self.run_extract([a], o)

    def run_extract(self, archives: list[str], o: archive.ExtractOptions):
        for a in archives:
            self._extract_one(a, o)

    def _extract_one(self, arch: str, o: archive.ExtractOptions):
        o = archive.ExtractOptions(**{**o.__dict__})
        if not o.password:
            o.password = session_passwords.get(arch, "")

        def done(job):
            if job.state == "done":
                dest = job.result
                if o.password and settings["remember_passwords"]:
                    session_passwords[arch] = o.password
                finished_toast(self, job, tr("{0} extraite").format(os.path.basename(arch)), dest)
                links = getattr(job, "skipped_links", [])
                if links:
                    # 7-Zip a refusé des liens qui pointaient hors du dossier d'extraction
                    text = "\n".join(f"{name} → {target}" for name, target in links[:15])
                    if len(links) > 15:
                        text += "\n" + tr("… et {0} autre(s)").format(len(links) - 15)
                    alert(self, ntr("{0} lien ignoré", "{0} liens ignorés", len(links)).format(len(links)),
                          tr("Ces liens de l'archive pointaient hors du dossier d'extraction ; tout le reste "
                             "a été extrait.") + "\n\n" + text)
                if o.open_after and dest:
                    self.open_location(dest, "tab")
            else:
                def retry(pw):
                    o.password = pw
                    self._extract_one(arch, o)
                self._handle_job_error(job, arch, retry)
        self._watch_progress(manager.submit(tr("Extraction de {0}").format(os.path.basename(arch)),
                                            lambda j: archive.extract(j, arch, o), done))

    def _handle_job_error(self, job, arch: str, retry):
        if job.state == "cancelled":
            toast(self, tr("{0} : annulé").format(job.title))
            return
        if job.needs_password:
            had = bool(session_passwords.get(arch)) or bool(self.pane and self.pane.password)

            def got(pw):
                if pw:
                    retry(pw)
            ask_password(self, arch, got, wrong=had)
            return
        finished_toast(self, job, "")

    def archive_test(self):
        p = self.pane
        for a in self._selected_archives():
            self._test_one(a, (p.password if p.mode == "archive" else "") or session_passwords.get(a, ""))

    def _test_one(self, arch: str, pw: str):
        def done(job):
            if job.state == "done":
                out = job.result or ""
                summary = "\n".join(l for l in out.splitlines()
                                    if l.split(":")[0].strip() in ("Files", "Folders", "Size", "Compressed"))
                summary = (summary.replace("Files", tr("Fichiers")).replace("Folders", tr("Dossiers"))
                           .replace("Compressed", tr("Compressé")).replace("Size", tr("Taille")))
                alert(self, tr("Archive intègre ✔"),
                      tr("{0}\nAucune erreur détectée.\n\n{summary}").format(os.path.basename(arch), summary=summary))
            else:
                self._handle_job_error(job, arch, lambda p2: self._test_one(arch, p2))
        manager.submit(tr("Test de {0}").format(os.path.basename(arch)), lambda j: archive.test(j, arch, pw), done)

    def archive_info(self):
        from .archive_dialogs import show_archive_info
        p = self.pane
        if p.mode == "archive" and p.listing:
            show_archive_info(self, p.listing)
            return
        arcs = self._selected_archives()
        if not arcs:
            return
        arch = arcs[0]

        def done(job):
            if job.state == "done":
                show_archive_info(self, job.result)
            else:
                self._handle_job_error(job, arch, lambda pw: (session_passwords.__setitem__(arch, pw),
                                                              self.archive_info()))
        manager.submit(tr("Lecture de {0}").format(os.path.basename(arch)),
                       lambda j: archive.list_archive(arch, session_passwords.get(arch, ""), j), done)

    def close_archive(self):
        p = self.pane
        if p.mode == "archive":
            name = os.path.basename(p.archive_path)
            p.open(os.path.dirname(p.archive_path), select=[name])

    def preview_archive_item(self, pane, rec, external: bool = False):
        """Extrait un élément dans un dossier temporaire puis l'ouvre (archive imbriquée incluse)."""
        if rec.size and rec.size > PREVIEW_LIMIT:
            toast(self, tr("Fichier volumineux : utilisez « Extraire »"))
            return
        arch, pw = pane.archive_path, pane.password
        inner = rec.get("inner", rec.name)

        def done(job):
            if job.state != "done":
                def retry(pw2):
                    pane.password = pw2
                    session_passwords[arch] = pw2
                    self.preview_archive_item(pane, rec, external)
                self._handle_job_error(job, arch, retry)
                return
            target = os.path.join(job.result, inner)
            if not external and formats.looks_like_archive(rec.name):
                pane.open_archive(target)
            else:
                open_with_default(self, target)
        manager.submit(tr("Ouverture de {0}").format(rec.name),
                       lambda j: archive.extract_to_temp(j, arch, [inner], pw), done)

    # -- modification de l'archive ouverte ---------------------------------
    def add_to_open_archive(self, pane, paths: Optional[list] = None):
        if pane.mode != "archive":
            return
        if not formats.is_modifiable(pane.archive_path):
            toast(self, tr("Ce format d'archive ne peut pas être modifié"))
            return
        if paths is None:
            choose_files(self, tr("Ajouter à l'archive"), os.path.dirname(pane.archive_path),
                         lambda ps: self.add_to_open_archive(pane, ps))
            return
        arch, inner, pw = pane.archive_path, pane.inner, pane.password

        def done(job):
            finished_toast(self, job, tr("{0} élément(s) ajouté(s)").format(len(paths)))
            if job.state == "done":
                pane.refresh()

        def go(r):
            if r == "add":
                manager.submit(tr("Ajout dans {0}").format(os.path.basename(arch)),
                               lambda j: archive.add_to_archive(j, arch, paths, inner, pw), done)
        alert(self, tr("Ajouter à l'archive ?"),
              f"{describe_paths(paths)} → {os.path.basename(arch)}" + (f"/{inner}" if inner else ""),
              responses=(("cancel", tr("Annuler")), ("add", tr("Ajouter"))), suggested="add", default="add",
              callback=go)

    def rename_in_archive(self, pane, rec):
        if not formats.is_modifiable(pane.archive_path):
            return
        old = rec.get("inner", rec.name)
        parent = old.rsplit("/", 1)[0] + "/" if "/" in old else ""
        arch, pw = pane.archive_path, pane.password

        def apply(new):
            def done(job):
                finished_toast(self, job, tr("Élément renommé"))
                if job.state == "done":
                    pane.refresh()
            manager.submit(tr("Renommage dans l'archive"),
                           lambda j: archive.rename_in_archive(j, arch, old, parent + new, pw), done)
        self.ask_name(tr("Renommer"), rec.name, tr("Renommer"), apply, current=rec.name)

    def delete_in_archive(self, pane):
        if not formats.is_modifiable(pane.archive_path):
            return
        paths = [r.get("inner", r.name) for r in pane.selected_recs()]
        if not paths:
            return
        arch, pw = pane.archive_path, pane.password

        def go(r):
            if r != "delete":
                return

            def done(job):
                finished_toast(self, job, tr("{0} élément(s) supprimé(s) de l'archive").format(len(paths)))
                if job.state == "done":
                    pane.refresh()
            manager.submit(tr("Suppression dans {0}").format(os.path.basename(arch)),
                           lambda j: archive.delete_from_archive(j, arch, paths, pw), done)
        alert(self, tr("Supprimer de l'archive ?"),
              tr("{0} sera retiré de {1}. Cette action est définitive.").format(describe_paths(paths),
                                                                                 os.path.basename(arch)),
              responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), destructive="delete",
              default="cancel", callback=go)

    def new_folder_in_archive(self, pane):
        if not formats.is_modifiable(pane.archive_path):
            return
        arch, pw = pane.archive_path, pane.password

        def make(name):
            inner = (pane.inner + "/" if pane.inner else "") + name
            manager.submit(tr("Nouveau dossier dans l'archive"),
                           lambda j: archive.new_folder_in_archive(j, arch, inner, pw),
                           lambda j: (finished_toast(self, j, tr("Dossier créé")), j.state == "done" and pane.refresh()))
        self.ask_name(tr("Nouveau dossier"), tr("Nouveau dossier"), tr("Créer"), make, select_stem=False)
