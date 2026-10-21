"""Gestion des tâches de fond et fenêtre « Tâches »."""

from __future__ import annotations

import time
from typing import Callable, Optional

from gi.repository import Adw, GLib, GObject, Gtk

from ..backend.engine import Job
from ..i18n import tr
from .common import DialogWindow, copy_to_clipboard, open_folder, toast


class JobManager(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "job-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "job-finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []

    @property
    def running(self) -> list[Job]:
        return [j for j in self.jobs if j.state in ("pending", "running")]

    def overall_fraction(self) -> float:
        r = self.running
        return sum(j.fraction for j in r) / len(r) if r else 1.0

    def submit(self, title: str, func: Callable[[Job], object],
               on_done: Optional[Callable[[Job], None]] = None) -> Job:
        job = Job(title, func)
        pending = {"t": False}

        def notify(j: Job):
            # appelé depuis le fil de la tâche : on regroupe les rafraîchissements
            if j.state in ("done", "failed", "cancelled"):
                GLib.idle_add(finished, j)
            elif not pending["t"]:
                pending["t"] = True
                GLib.timeout_add(80, flush)

        def flush():
            pending["t"] = False
            self.emit("changed")
            return False

        def finished(j: Job):
            self.emit("changed")
            self.emit("job-finished", j)
            if on_done:
                on_done(j)
            return False

        job.subscribe(notify)
        self.jobs.insert(0, job)
        self.emit("job-added", job)
        self.emit("changed")
        job.start()
        return job

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.state in ("pending", "running")]
        self.emit("changed")


manager = JobManager()

STATE_ICONS = {
    "pending": "content-loading-symbolic",
    "running": "media-playback-start-symbolic",
    "done": "emblem-ok-symbolic",
    "failed": "dialog-error-symbolic",
    "cancelled": "process-stop-symbolic",
}


class JobRow(Gtk.ListBoxRow):
    def __init__(self, job: Job):
        super().__init__(activatable=False, css_classes=["job-row"])
        self.job = job
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12, margin_start=14, margin_end=14)
        top = Gtk.Box(spacing=10)
        self.icon = Gtk.Image(pixel_size=20)
        top.append(self.icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(label=job.title, xalign=0, css_classes=["heading"],
                               ellipsize=3)
        self.status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=2)
        texts.append(self.title)
        texts.append(self.status)
        top.append(texts)
        self.log_btn = Gtk.Button(icon_name="utilities-terminal-symbolic", tooltip_text=tr("Journal et commandes"),
                                  css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.log_btn.connect("clicked", self._show_log)
        top.append(self.log_btn)
        self.cancel_btn = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text=tr("Annuler"),
                                     css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.cancel_btn.connect("clicked", lambda *_: job.cancel())
        top.append(self.cancel_btn)
        box.append(top)
        self.bar = Gtk.ProgressBar(css_classes=["job-progress"])
        box.append(self.bar)
        self.set_child(box)
        self.update()

    def update(self):
        j = self.job
        self.icon.set_from_icon_name(STATE_ICONS.get(j.state, "emblem-default-symbolic"))
        status = j.status
        if j.state == "running" and j.fraction > 0.01 and j.started:
            elapsed = time.time() - j.started
            remaining = elapsed / j.fraction - elapsed
            if remaining > 2:
                m, s = divmod(int(remaining), 60)
                status += tr('  ·  ≈ {m} min {s:02d} s restantes').format(m=m, s=s) if m else tr('  ·  ≈ {s} s restantes').format(s=s)
        elif j.state in ("done", "failed", "cancelled") and j.finished and j.started:
            status += tr('  ·  {0:.1f} s').format(j.finished - j.started)
        self.status.set_text(status)
        self.bar.set_fraction(j.fraction)
        self.bar.set_visible(j.state in ("pending", "running"))
        self.cancel_btn.set_visible(j.state in ("pending", "running"))
        for c in ("error", "success"):
            self.remove_css_class(c)
        if j.state == "failed":
            self.add_css_class("error")
        elif j.state == "done":
            self.add_css_class("success")

    def _show_log(self, *_):
        j = self.job
        text = tr("Commandes :\n") + ("\n".join("$ " + c for c in j.commands) or "(aucune)")
        if j.error:
            text += tr('\n\nErreur : {0}').format(j.error)
        if j.log:
            text += tr("\n\nSortie :\n") + "\n".join(j.log)[-20000:]
        dlg = DialogWindow(title=tr("Journal — ") + j.title, content_width=640, content_height=420)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=tr("Copier"))
        copy.connect("clicked", lambda b: (copy_to_clipboard(b, text), toast(self, tr("Journal copié"))))
        hb.pack_start(copy)
        tv.add_top_bar(hb)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        view.get_buffer().set_text(text)
        sw = Gtk.ScrolledWindow(child=view, vexpand=True)
        tv.set_content(sw)
        dlg.set_child(tv)
        dlg.present(self)


class JobsDialog(DialogWindow):
    def __init__(self):
        super().__init__(title=tr("Tâches"), content_width=520, content_height=520)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        clear = Gtk.Button(label=tr("Effacer"), tooltip_text=tr("Retirer les tâches terminées"))
        clear.connect("clicked", lambda *_: manager.clear_finished())
        hb.pack_start(clear)
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack()
        empty = Adw.StatusPage(icon_name="emblem-ok-symbolic", title=tr("Aucune tâche"),
                               description=tr("Les copies, déplacements, extractions et conversions apparaîtront ici."))
        self.stack.add_named(empty, "empty")
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                                valign=Gtk.Align.START)
        self.stack.add_named(Gtk.ScrolledWindow(child=self.list, vexpand=True), "list")
        tv.set_content(self.stack)
        self.set_child(tv)
        self.rows: dict[int, JobRow] = {}
        self._h = manager.connect("changed", lambda *_: self.refresh())
        self.connect("closed", lambda *_: manager.disconnect(self._h))
        self.refresh()

    def refresh(self):
        ids = {id(j) for j in manager.jobs}
        for jid in list(self.rows):
            if jid not in ids:
                self.list.remove(self.rows.pop(jid))
        for i, j in enumerate(manager.jobs):
            row = self.rows.get(id(j))
            if row is None:
                row = JobRow(j)
                self.rows[id(j)] = row
                self.list.insert(row, i)
            row.update()
        self.stack.set_visible_child_name("list" if manager.jobs else "empty")


def finished_toast(widget: Gtk.Widget, job: Job, success: str, folder: Optional[str] = None,
                   on_error: Optional[Callable[[Job], None]] = None):
    if job.state == "done":
        if folder:
            toast(widget, success, tr("Ouvrir"), lambda: open_folder(widget, folder), timeout=6)
        else:
            toast(widget, success)
    elif job.state == "cancelled":
        toast(widget, tr('{0} : annulé').format(job.title))
    else:
        if on_error:
            on_error(job)
        else:
            from .common import alert
            alert(widget, job.title + tr(" a échoué"), job.error,
                  responses=(("ok", tr("Fermer")),))


class OperationsButton(Gtk.MenuButton):
    """Indicateur des opérations en cours dans la barre d'en-tête, comme celui de Nautilus."""

    def __init__(self):
        super().__init__(tooltip_text=tr("Opérations sur les fichiers"), visible=False,
                         css_classes=["flat", "operations-button"])
        self.pie = Gtk.ProgressBar(css_classes=["operations-pie"], valign=Gtk.Align.CENTER, width_request=22)
        box = Gtk.Box(spacing=6)
        self.spinner = Gtk.Spinner()
        box.append(self.spinner)
        self.label = Gtk.Label(css_classes=["caption", "numeric"])
        box.append(self.label)
        self.set_child(box)
        pop = Gtk.Popover()
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                width_request=380)
        sw = Gtk.ScrolledWindow(child=self.list, propagate_natural_height=True, max_content_height=460,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        col.append(sw)
        clear = Gtk.Button(label=tr("Effacer les opérations terminées"), css_classes=["flat"])
        clear.connect("clicked", lambda *_: manager.clear_finished())
        col.append(clear)
        pop.set_child(col)
        self.set_popover(pop)
        self.rows: dict[int, JobRow] = {}
        self._hide_id = 0
        manager.connect("changed", lambda *_: self.refresh())

    def refresh(self):
        ids = {id(j) for j in manager.jobs}
        for jid in list(self.rows):
            if jid not in ids:
                self.list.remove(self.rows.pop(jid))
        for i, j in enumerate(manager.jobs[:30]):
            row = self.rows.get(id(j))
            if row is None:
                row = JobRow(j)
                self.rows[id(j)] = row
                self.list.insert(row, i)
            row.update()
        running = manager.running
        if running:
            if self._hide_id:
                GLib.source_remove(self._hide_id)
                self._hide_id = 0
            self.set_visible(True)
            self.spinner.start()
            self.label.set_text(f"{int(manager.overall_fraction() * 100)} %")
            self.label.set_visible(True)
        else:
            self.spinner.stop()
            self.label.set_visible(False)
            if manager.jobs and not self._hide_id and self.get_visible():
                self._hide_id = GLib.timeout_add_seconds(20, self._autohide)
            elif not manager.jobs:
                self.set_visible(False)

    def _autohide(self):
        self._hide_id = 0
        if not manager.running and not self.get_active():
            self.set_visible(False)
        return False


class ProgressWindow(Adw.Window):
    """Fenêtre de progression des opérations longues (fonctionnalité « Progress Mirror »).

    N'apparaît que si une opération dure plus que le délai réglé : les copies rapides restent
    silencieuses. Se ferme seule quand tout est terminé.
    """

    def __init__(self, parent: Gtk.Window):
        super().__init__(title=tr("Opérations en cours"), default_width=460, transient_for=parent,
                         hide_on_close=True, resizable=True)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                                valign=Gtk.Align.START)
        tv.set_content(Gtk.ScrolledWindow(child=self.list, propagate_natural_height=True,
                                          max_content_height=520, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_content(tv)
        self.rows: dict[int, JobRow] = {}
        self._questions = 0          # questions (conflit…) ouvertes : la fenêtre s'efface devant elles
        manager.connect("changed", lambda *_: self.refresh())

    def question_opened(self):
        """Une question « Remplacer ? » s'ouvre : sinon cette fenêtre passerait devant elle."""
        self._questions += 1
        self.set_visible(False)

    def question_closed(self):
        self._questions = max(self._questions - 1, 0)
        if not self._questions and any(getattr(j, "file_operation", False) and
                                       getattr(j, "progress_shown", False) for j in manager.running):
            self.refresh()
            self.present()

    def _fit(self):
        """Hauteur ajustée au nombre d'opérations (jusqu'à 520 px, puis défilement). GTK garde la
        taille d'une fenêtre masquée puis rouverte : sans ça, elle restait haute pour une seule."""
        self.set_default_size(self.get_width() or 460, -1)

    def refresh(self):
        running = [j for j in manager.running if getattr(j, "file_operation", False)]
        ids = {id(j) for j in running}
        count = len(self.rows)
        for jid in list(self.rows):
            if jid not in ids:
                self.list.remove(self.rows.pop(jid))
        for j in running:
            row = self.rows.get(id(j))
            if row is None:
                row = JobRow(j)
                self.rows[id(j)] = row
                self.list.append(row)
            row.update()
        if not running and self.get_visible():
            self.set_visible(False)
        elif len(self.rows) != count or not self.get_visible():
            self._fit()

    def watch(self, job: Job, delay: float):
        def show():
            if job.state in ("pending", "running"):
                job.progress_shown = True
                if not self._questions:
                    self.refresh()
                    self.present()
            return False
        GLib.timeout_add(int(max(delay, 0) * 1000), show)
