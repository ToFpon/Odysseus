"""Briques d'interface partagées."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from ..i18n import ntr, tr  # noqa: E402


def shown(text) -> str:
    """Texte affichable par GTK : un nom de fichier non UTF-8 arrive de Python avec des caractères
    de substitution (surrogateescape) que Gtk refuse (UnicodeEncodeError : ligne vide, titre figé).
    Les octets invalides deviennent « � », comme dans Nautilus ; le vrai chemin reste intact ailleurs."""
    if not isinstance(text, str):
        return "" if text is None else str(text)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def toast(widget: Gtk.Widget, text: str, button: str = "", callback: Optional[Callable] = None,
          timeout: int = 4):
    win = widget.get_root() if widget else None
    overlay = getattr(win, "toast_overlay", None)
    if overlay is None:
        return
    t = Adw.Toast.new(text)
    t.set_timeout(timeout)
    if button and callback:
        t.set_button_label(button)
        t.connect("button-clicked", lambda *_: callback())
    overlay.add_toast(t)


# ---------------------------------------------------------------------------
# Dialogues : de vraies fenêtres, comme Nautilus 46 (GNOME 46 / libadwaita 1.5). Les feuilles
# Adw.Dialog / Adw.AlertDialog sont dessinées DANS la fenêtre principale : le thème GTK du système
# (fond, transparence, bordure de fenêtre) ne s'y applique pas, et la fenêtre de progression, elle
# une vraie fenêtre, passait devant la question « Remplacer ? ».
# ---------------------------------------------------------------------------

def dialog_parent(widget) -> Optional[Gtk.Window]:
    if widget is None:
        return None
    root = widget.get_root() if isinstance(widget, Gtk.Widget) else None
    return root if isinstance(root, Gtk.Window) else None


class DialogWindow(Adw.Window):
    """Remplace Adw.Dialog (même usage : title, content_width/height, set_child, present(parent),
    close, signal « closed », Échap pour fermer), en fenêtre modale."""

    __gsignals__ = {"closed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, title: str = "", content_width: int = -1, content_height: int = -1, **kw):
        super().__init__(title=title, modal=True, **kw)
        self.set_default_size(content_width, content_height)
        self.connect("close-request", self._on_close_request)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(keys)

    def _on_close_request(self, *_):
        self.emit("closed")
        return False

    def set_child(self, child):
        self.set_content(child)

    def get_child(self):
        return self.get_content()

    def set_content_width(self, width: int):
        self.set_default_size(width, self.get_default_size()[1])

    def set_content_height(self, height: int):
        self.set_default_size(self.get_default_size()[0], height)

    def force_close(self):
        self.close()

    def present(self, parent=None):
        win = dialog_parent(parent)
        if win is not None and win is not self:
            self.set_transient_for(win)
        super().present()


class AlertDialog(Adw.MessageDialog):
    """Remplace Adw.AlertDialog : Adw.MessageDialog, la boîte de Nautilus 46, large comme elle."""

    def __init__(self, heading: str = "", body: Optional[str] = None, **kw):
        super().__init__(heading=heading, body=body or "", modal=True, **kw)
        if len(heading) + len(body or "") > 50:
            self.set_default_size(600, -1)       # texte sur peu de lignes, comme chez Nautilus

    @classmethod
    def new(cls, heading: str, body: Optional[str] = None) -> "AlertDialog":
        return cls(heading, body)

    def present(self, parent=None):
        win = dialog_parent(parent)
        if win is not None and win is not self:
            self.set_transient_for(win)
        super().present()


def alert(parent: Gtk.Widget, heading: str, body: str = "", responses=(("ok", "OK"),),
          default: str = "", destructive: str = "", suggested: str = "",
          callback: Optional[Callable[[str], None]] = None, extra: Optional[Gtk.Widget] = None):
    d = AlertDialog.new(heading, body or None)
    for rid, label in responses:
        d.add_response(rid, label)
    if destructive:
        d.set_response_appearance(destructive, Adw.ResponseAppearance.DESTRUCTIVE)
    if suggested:
        d.set_response_appearance(suggested, Adw.ResponseAppearance.SUGGESTED)
    if default:
        d.set_default_response(default)
        d.set_close_response(responses[0][0])
    if extra is not None:
        d.set_extra_child(extra)
    if callback:
        d.connect("response", lambda _d, r: callback(r))
    d.present(parent)
    return d


def error(parent: Gtk.Widget, heading: str, body: str = ""):
    alert(parent, heading, body)


def ask_password(parent: Gtk.Widget, archive: str, callback: Callable[[Optional[str]], None],
                 wrong: bool = False):
    row = Adw.PasswordEntryRow(title=tr("Mot de passe"))
    box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
    box.append(row)
    body = os.path.basename(archive)
    if wrong:
        body = tr("Le mot de passe est incorrect. Réessayez.\n") + body
    d = AlertDialog.new(tr("Archive protégée"), body)
    d.add_response("cancel", tr("Annuler"))
    d.add_response("ok", tr("Déverrouiller"))
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    d.set_extra_child(box)
    row.connect("entry-activated", lambda *_: (d.close(), callback(row.get_text())))
    d.connect("response", lambda _d, r: callback(row.get_text() if r == "ok" else None))
    d.present(parent)
    GLib.idle_add(row.grab_focus)


def ask_text(parent: Gtk.Widget, heading: str, label: str, initial: str,
             callback: Callable[[str], None], action: str = tr("Valider")):
    row = Adw.EntryRow(title=label, text=initial)
    box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
    box.append(row)
    d = AlertDialog.new(heading, None)
    d.add_response("cancel", tr("Annuler"))
    d.add_response("ok", action)
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    d.set_extra_child(box)
    done = {"v": False}

    def finish(ok):
        if done["v"]:
            return
        done["v"] = True
        if ok and row.get_text().strip():
            callback(row.get_text().strip())
    row.connect("entry-activated", lambda *_: (finish(True), d.close()))
    d.connect("response", lambda _d, r: finish(r == "ok"))
    d.present(parent)
    GLib.idle_add(row.grab_focus)


def choose_folder(parent: Gtk.Widget, title: str, initial: str, callback: Callable[[str], None]):
    dlg = Gtk.FileDialog(title=title, modal=True)
    if initial and os.path.isdir(initial):
        dlg.set_initial_folder(Gio.File.new_for_path(initial))

    def done(d, res):
        try:
            f = d.select_folder_finish(res)
        except GLib.Error:
            return
        if f and f.get_path():
            callback(f.get_path())
    dlg.select_folder(parent.get_root(), None, done)


def choose_files(parent: Gtk.Widget, title: str, initial: str, callback: Callable[[list], None],
                 multiple: bool = True, filters: Optional[list] = None):
    dlg = Gtk.FileDialog(title=title, modal=True)
    if initial and os.path.isdir(initial):
        dlg.set_initial_folder(Gio.File.new_for_path(initial))
    if filters:
        store = Gio.ListStore.new(Gtk.FileFilter)
        for name, patterns in filters:
            ff = Gtk.FileFilter(name=name)
            for p in patterns:
                ff.add_pattern(p)
            store.append(ff)
        dlg.set_filters(store)

    def done(d, res):
        try:
            if multiple:
                model = d.open_multiple_finish(res)
                paths = [model.get_item(i).get_path() for i in range(model.get_n_items())]
            else:
                f = d.open_finish(res)
                paths = [f.get_path()] if f else []
        except GLib.Error:
            return
        paths = [p for p in paths if p]
        if paths:
            callback(paths)
    if multiple:
        dlg.open_multiple(parent.get_root(), None, done)
    else:
        dlg.open(parent.get_root(), None, done)


def choose_save(parent: Gtk.Widget, title: str, initial_path: str, callback: Callable[[str], None]):
    dlg = Gtk.FileDialog(title=title, modal=True)
    if initial_path:
        folder = os.path.dirname(initial_path)
        if os.path.isdir(folder):
            dlg.set_initial_folder(Gio.File.new_for_path(folder))
        dlg.set_initial_name(os.path.basename(initial_path))

    def done(d, res):
        try:
            f = d.save_finish(res)
        except GLib.Error:
            return
        if f and f.get_path():
            callback(f.get_path())
    dlg.save(parent.get_root(), None, done)


def launch_default(parent: Optional[Gtk.Widget], f: Gio.File) -> bool:
    """Ouvre f avec l'application par défaut de son type, directement, comme Nautilus.
    (Gtk.FileLauncher passe par le portail, qui affiche « Ouvrir avec… » sous GNOME même quand une
    application par défaut existe.) False s'il n'y a pas d'application par défaut."""
    try:
        info = f.query_info("standard::content-type,standard::fast-content-type",
                            Gio.FileQueryInfoFlags.NONE, None)
        ctype = info.get_content_type() or info.get_attribute_string("standard::fast-content-type")
        app = Gio.AppInfo.get_default_for_type(ctype, not f.is_native()) if ctype else None
    except GLib.Error:
        app = None
    if app is None:
        return False
    win = parent.get_root() if parent else None
    ctx = win.get_display().get_app_launch_context() if win is not None else None
    try:
        return bool(app.launch([f], ctx))
    except GLib.Error:
        return False


def open_with_default(parent: Gtk.Widget, path: str, folder: bool = False):
    f = Gio.File.new_for_path(path)
    if not folder and launch_default(parent, f):
        return
    launcher = Gtk.FileLauncher.new(f)      # pas d'application par défaut : le choix du système
    win = parent.get_root() if parent else None

    def done(l, res):
        try:
            l.launch_finish(res) if not folder else l.open_containing_folder_finish(res)
        except GLib.Error:
            try:
                subprocess.Popen(["xdg-open", path if not folder else os.path.dirname(path)])
            except OSError:
                pass
    if folder:
        launcher.open_containing_folder(win, None, done)
    else:
        launcher.launch(win, None, done)


def open_folder(parent: Gtk.Widget, folder: str):
    f = Gio.File.new_for_path(folder)
    if launch_default(parent, f):
        return
    launcher = Gtk.FileLauncher.new(f)

    def done(l, res):
        try:
            l.launch_finish(res)
        except GLib.Error:
            try:
                subprocess.Popen(["xdg-open", folder])
            except OSError:
                pass
    launcher.launch(parent.get_root() if parent else None, None, done)


def copy_to_clipboard(widget: Gtk.Widget, text: str):
    widget.get_clipboard().set(shown(text))


def combo_row(title: str, labels: list[str], selected: int = 0, subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_selected(max(0, min(selected, len(labels) - 1)))
    return row


def path_row(title: str, path: str, on_browse: Callable, icon: str = "folder-open-symbolic") -> Adw.EntryRow:
    row = Adw.EntryRow(title=title, text=path)
    btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tr("Parcourir…"),
                     css_classes=["flat"])
    btn.connect("clicked", lambda *_: on_browse(row))
    row.add_suffix(btn)
    return row


def command_group(title: str = tr("Commande équivalente")) -> tuple[Adw.PreferencesGroup, Gtk.Label]:
    """Affiche la ligne de commande générée, comme la console de PeaZip."""
    group = Adw.PreferencesGroup(title=title)
    exp = Adw.ExpanderRow(title=tr("Afficher la commande"), subtitle=tr("Utile pour vos scripts"))
    exp.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
    label = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["monospace", "command-label"],
                      margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
    label.set_wrap_mode(2)  # PANGO_WRAP_CHAR
    copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                      tooltip_text=tr("Copier"), css_classes=["flat"])
    copy.connect("clicked", lambda b: (copy_to_clipboard(b, label.get_text()), toast(b, tr("Commande copiée"))))
    exp.add_suffix(copy)
    exp.add_row(label)
    group.add(exp)
    return group, label


def dialog_shell(title: str, action_label: str, width: int = 560, height: int = 680,
                 destructive: bool = False):
    """Fenêtre de dialogue avec barre d'en-tête Annuler / Action et page de préférences."""
    dialog = DialogWindow(title=title, content_width=width, content_height=height)
    tv = Adw.ToolbarView()
    header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
    cancel = Gtk.Button(label=tr("Annuler"))
    cancel.connect("clicked", lambda *_: dialog.close())
    header.pack_start(cancel)
    action = Gtk.Button(label=action_label,
                        css_classes=["destructive-action" if destructive else "suggested-action"])
    header.pack_end(action)
    tv.add_top_bar(header)
    page = Adw.PreferencesPage()
    tv.set_content(page)
    dialog.set_child(tv)
    dialog.set_default_widget(action)
    return dialog, page, action


def describe_paths(paths: list[str]) -> str:
    if not paths:
        return tr("Aucun élément")
    if len(paths) == 1:
        return os.path.basename(paths[0].rstrip("/")) or paths[0]
    n = len(paths) - 1
    return ntr("{name} et {n} autre", "{name} et {n} autres", n).format(
        name=os.path.basename(paths[0].rstrip("/")), n=n)


# Mois abrégés comme dans Nautilus (indépendants de la locale C du processus)
MONTHS = [tr("janv."), tr("févr."), tr("mars"), tr("avril"), tr("mai"), tr("juin"), tr("juil."),
          tr("août"), tr("sept."), tr("oct."), tr("nov."), tr("déc.")]


def date_label(mtime: float) -> str:
    """Date à la manière de Nautilus : « Aujourd'hui à 23:25 », « Hier à 23:35 », « 4 sept. 2026 »."""
    if not mtime:
        return ""
    dt = datetime.fromtimestamp(mtime)
    now = datetime.now()
    if dt.date() == now.date():
        return tr("Aujourd'hui à {0}").format(dt.strftime("%H:%M"))
    if (now.date() - dt.date()).days == 1:
        return tr("Hier à {0}").format(dt.strftime("%H:%M"))
    return tr("{day} {month} {year}").format(day=dt.day, month=MONTHS[dt.month - 1], year=dt.year)


def full_date(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M:%S") if mtime else ""


def perms_label(mode: int) -> str:
    import stat as st
    if not mode:
        return ""
    kind = "d" if st.S_ISDIR(mode) else ("l" if st.S_ISLNK(mode) else "-")
    out = kind
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 7
        out += ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
    return out


def clear_box(box: Gtk.Widget):
    while (c := box.get_first_child()) is not None:
        box.remove(c)


def popup_menu(parent: Gtk.Widget, model, x: float, y: float) -> Gtk.PopoverMenu:
    pop = Gtk.PopoverMenu.new_from_model(model)
    pop.set_parent(parent)
    pop.set_has_arrow(False)
    pop.set_halign(Gtk.Align.START)
    r = Gdk.Rectangle()
    r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
    pop.set_pointing_to(r)
    pop.connect("closed", lambda p_: GLib.idle_add(p_.unparent))
    pop.popup()
    return pop


def bidir():
    return GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE
