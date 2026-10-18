"""Dialogues d'archives repris de Pressoir : créer, extraire, propriétés de l'archive."""

from __future__ import annotations

import os
from typing import Callable, Optional

from gi.repository import Adw, Gtk

from ..backend import archive, formats, tools
from ..backend.engine import human_size, sfx_module
from ..settings import settings
from ..i18n import decimal, ntr, tr
from .common import (DialogWindow, choose_folder, choose_save, combo_row, command_group, describe_paths,
                     dialog_shell, path_row)

LEVEL_LABELS = [tr("Stockage (aucune)"), tr("La plus rapide"), tr("Rapide"), tr("Normale"), tr("Maximale"), tr("Ultra")]
LEVEL_7Z = [0, 1, 3, 5, 7, 9]
LEVEL_FRAC = [0.0, 0.0, 0.25, 0.5, 0.75, 1.0]
THREAD_LABEL = tr("Automatique")
DICT_LABELS = [tr("Automatique"), tr("1 Mo"), tr("4 Mo"), tr("16 Mo"), tr("64 Mo"), tr("256 Mo"), tr("1 Go")]
DICT_VALUES = ["", "1m", "4m", "16m", "64m", "256m", "1024m"]
UPDATE_LABELS = [tr("Créer une nouvelle archive"), tr("Ajouter et remplacer"), tr("Mettre à jour (plus récents)"),
                 tr("Synchroniser"), tr("Rafraîchir l'existant")]
UPDATE_VALUES = ["new", "add", "update", "sync", "fresh"]


def level_for(fmt: formats.CreateFormat, idx: int) -> int:
    if fmt.engine == "7z":
        return LEVEL_7Z[idx]
    lo, hi = fmt.levels
    return round(lo + (hi - lo) * LEVEL_FRAC[idx])


class CreateDialog:
    """Création d'une archive (ou conversion si `convert_from` est fourni)."""

    def __init__(self, parent: Gtk.Widget, sources: list[str],
                 on_accept: Callable[[list, str, archive.CreateOptions, bool], None],
                 convert_from: str = "", out_dir: str = ""):
        self.sources = sources
        self.convert_from = convert_from
        self.on_accept = on_accept
        title = tr("Convertir l'archive") if convert_from else tr("Nouvelle archive")
        self.dialog, page, self.action = dialog_shell(title, tr("Convertir") if convert_from else tr("Créer"),
                                                      width=600, height=720)
        self.formats = [f for f in formats.CREATE_FORMATS if f.available()]
        self._building = True

        # --- résumé -----------------------------------------------------
        summary = Adw.PreferencesGroup()
        if convert_from:
            src_desc = os.path.basename(convert_from)
            size = os.path.getsize(convert_from) if os.path.exists(convert_from) else 0
        else:
            src_desc = describe_paths(sources)
            size = archive.total_size(sources)
        hero = Gtk.Box(spacing=14, css_classes=["dialog-hero"])
        hero.append(Gtk.Image(icon_name="package-x-generic-symbolic", pixel_size=36,
                              css_classes=["hero-icon"]))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        col.append(Gtk.Label(label=src_desc, xalign=0, css_classes=["title-4"], ellipsize=3))
        col.append(Gtk.Label(label=f"{human_size(size)}" + ("" if convert_from else
                             ntr(" · {0} élément", " · {0} éléments", len(sources)).format(len(sources))),
                             xalign=0, css_classes=["dim-label"]))
        hero.append(col)
        summary.add(hero)
        page.add(summary)

        # --- archive ----------------------------------------------------
        g = Adw.PreferencesGroup(title=tr("Archive"))
        keys = [f.key for f in self.formats]
        default_fmt = settings["create_format"] if settings["create_format"] in keys else keys[0]
        if not convert_from and len(sources) == 1 and os.path.isfile(sources[0]) is False and \
                not formats.FORMATS_BY_KEY[default_fmt].multi:
            default_fmt = "7z"
        self.fmt_row = combo_row(tr("Format"), [f.label for f in self.formats], keys.index(default_fmt))
        self.fmt_row.connect("notify::selected", lambda *_: self._on_format())
        g.add(self.fmt_row)
        self.fmt_note = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], wrap=True)

        base_sources = [convert_from] if convert_from else sources
        out = archive.default_output(base_sources, default_fmt)
        if convert_from:
            base, _ = formats.split_archive_name(os.path.basename(convert_from))
            out = os.path.join(os.path.dirname(convert_from), base + "." + formats.FORMATS_BY_KEY[default_fmt].ext)
        if out_dir:
            out = os.path.join(out_dir, os.path.basename(out))
        self.out_row = path_row(tr("Enregistrer sous"), out, self._browse_out, icon="document-save-symbolic")
        self.out_row.connect("changed", lambda *_: self._refresh())
        g.add(self.out_row)

        self.level_row = combo_row(tr("Niveau de compression"), LEVEL_LABELS, settings["create_level"])
        self.level_row.connect("notify::selected", lambda *_: self._refresh())
        g.add(self.level_row)
        self.method_row = combo_row(tr("Méthode"), ["—"])
        self.method_row.connect("notify::selected", lambda *_: self._refresh())
        g.add(self.method_row)
        self.each_row = Adw.SwitchRow(title=tr("Une archive par élément"),
                                      subtitle=tr("Compresse chaque fichier ou dossier séparément"))
        self.each_row.set_visible(len(sources) > 1 and not convert_from)
        self.each_row.connect("notify::active", lambda *_: self._refresh())
        g.add(self.each_row)
        self.update_row = combo_row(tr("Si l'archive existe"), UPDATE_LABELS, 0)
        self.update_row.connect("notify::selected", lambda *_: self._refresh())
        g.add(self.update_row)
        page.add(g)

        # --- sécurité ---------------------------------------------------
        self.sec = Adw.PreferencesGroup(title=tr("Chiffrement"),
                                        description=tr("AES-256 — conservez votre mot de passe en lieu sûr"))
        self.pw_row = Adw.PasswordEntryRow(title=tr("Mot de passe"))
        self.pw2_row = Adw.PasswordEntryRow(title=tr("Confirmer le mot de passe"))
        gen = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", tooltip_text=tr("Générer un mot de passe fort"),
                         valign=Gtk.Align.CENTER, css_classes=["flat"])
        gen.connect("clicked", self._generate)
        self.pw_row.add_suffix(gen)
        self.strength = Gtk.LevelBar(min_value=0, max_value=128, valign=Gtk.Align.CENTER,
                                     width_request=60, css_classes=["strength"])
        self.pw_row.add_suffix(self.strength)
        for r in (self.pw_row, self.pw2_row):
            r.connect("changed", lambda *_: self._refresh())
        self.names_row = Adw.SwitchRow(title=tr("Chiffrer aussi les noms de fichiers"),
                                       active=settings["create_encrypt_names"])
        self.zipenc_row = combo_row(tr("Algorithme ZIP"), [tr("AES-256 (recommandé)"), tr("ZipCrypto (compatibilité)")])
        for r in (self.pw_row, self.pw2_row, self.names_row, self.zipenc_row):
            self.sec.add(r)
        self.names_row.connect("notify::active", lambda *_: self._refresh())
        self.zipenc_row.connect("notify::selected", lambda *_: self._refresh())
        page.add(self.sec)

        # --- avancé -----------------------------------------------------
        adv = Adw.PreferencesGroup(title=tr("Options avancées"))
        self.solid_row = Adw.SwitchRow(title=tr("Archive solide"), active=settings["create_solid"],
                                       subtitle=tr("Meilleur taux, extraction partielle plus lente"))
        self.threads_row = Adw.SpinRow.new_with_range(0, max(1, os.cpu_count() or 1) * 2, 1)
        self.threads_row.set_title(tr("Fils d'exécution"))
        self.threads_row.set_subtitle(tr("0 = automatique"))
        self.dict_row = combo_row(tr("Taille du dictionnaire"), DICT_LABELS)
        self.vol_row = combo_row(tr("Découper en volumes"), [tr("Ne pas découper")] +
                                 [l for l, _ in tools.PRESET_VOLUMES[1:]] + [tr("Taille personnalisée…")])
        self.vol_custom = Adw.EntryRow(title=tr("Taille d'un volume (ex. 250m, 1,5g)"), visible=False)
        self.sfx_row = Adw.SwitchRow(title=tr("Auto-extractible"),
                                     subtitle=tr("Exécutable Linux qui s'extrait seul"))
        self.sfx_row.set_sensitive(bool(sfx_module()))
        self.del_row = Adw.SwitchRow(title=tr("Supprimer les originaux après compression"))
        self.excl_row = Adw.EntryRow(title=tr("Exclure (motifs séparés par ;  ex. *.tmp;.git)"))
        self.extra_row = Adw.EntryRow(title=tr("Options 7-Zip supplémentaires"))
        for r in (self.solid_row, self.threads_row, self.dict_row, self.vol_row, self.vol_custom,
                  self.sfx_row, self.del_row, self.excl_row, self.extra_row):
            adv.add(r)
            if isinstance(r, Adw.SwitchRow):
                r.connect("notify::active", lambda *_: self._refresh())
            elif isinstance(r, Adw.ComboRow):
                r.connect("notify::selected", lambda *_: self._refresh())
            elif isinstance(r, Adw.SpinRow):
                r.connect("notify::value", lambda *_: self._refresh())
            else:
                r.connect("changed", lambda *_: self._refresh())
        self.del_row.set_visible(not convert_from)
        page.add(adv)

        cmd_group, self.cmd_label = command_group()
        page.add(cmd_group)

        self.action.connect("clicked", self._accept)
        self._building = False
        self._on_format()

    # ------------------------------------------------------------------
    @property
    def fmt(self) -> formats.CreateFormat:
        return self.formats[self.fmt_row.get_selected()]

    def _browse_out(self, row):
        choose_save(self.dialog, tr("Enregistrer l'archive"), row.get_text(), row.set_text)

    def _generate(self, *_):
        pw = tools.generate_password(24)
        self.pw_row.set_text(pw)
        self.pw2_row.set_text(pw)
        self.dialog.get_clipboard().set(pw)
        from .common import toast  # noqa: F811
        toast(self.dialog, tr("Mot de passe généré et copié dans le presse-papiers"))

    def _on_format(self):
        if self._building:
            return
        f = self.fmt
        # adapter l'extension du fichier de sortie
        out = self.out_row.get_text()
        if out:
            folder, name = os.path.split(out)
            base, _ = formats.split_archive_name(name)
            if base.endswith(".run"):
                base = base[:-4]
            # un seul fichier : « notes.txt » → « notes.txt.gz » en GZ / XZ… (un seul fichier, l'extension
            # doit survivre à la décompression), « notes.zip » en ZIP / 7Z… (si la base n'a pas été
            # changée à la main)
            srcs = [self.convert_from] if self.convert_from else self.sources
            if len(srcs) == 1 and os.path.isfile(srcs[0]) and not self.convert_from:
                full = os.path.basename(srcs[0])
                stem = os.path.splitext(full)[0] or full
                if base in (full, stem):
                    base = stem if f.multi else full
            new = os.path.join(folder, f"{base}.{f.ext}")
            if new != out:
                self.out_row.set_text(new)
        methods = list(f.methods) or ["—"]
        self.method_row.set_model(Gtk.StringList.new(methods))
        self.method_row.set_selected(0)
        self.method_row.set_sensitive(bool(f.methods))
        self.level_row.set_sensitive(f.levels != (0, 0))
        self.sec.set_visible(f.encrypt)
        self.names_row.set_visible(f.encrypt_names)
        self.zipenc_row.set_visible(f.key == "zip")
        self.solid_row.set_visible(f.solid)
        self.dict_row.set_visible(f.key == "7z")
        self.threads_row.set_visible(f.engine == "7z" and f.key in ("7z", "zip"))
        self.sfx_row.set_visible(f.sfx)
        self.extra_row.set_visible(f.engine == "7z")
        self.update_row.set_visible(f.engine == "7z")
        self.fmt_row.set_subtitle(f.notes)
        self._refresh()

    def _volume(self) -> str:
        idx = self.vol_row.get_selected()
        presets = tools.PRESET_VOLUMES[1:]
        if idx == 0:
            return ""
        if idx <= len(presets):
            return presets[idx - 1][1]
        return self.vol_custom.get_text().strip().lower().replace(",", ".").replace("o", "")

    def options(self) -> archive.CreateOptions:
        f = self.fmt
        method = f.methods[self.method_row.get_selected()] if f.methods else ""
        level = level_for(f, self.level_row.get_selected())
        if method == "Copy":
            level = 0
        return archive.CreateOptions(
            fmt=f.key, level=level, method=method if method != "LZMA2" else "",
            solid=self.solid_row.get_active(), threads=int(self.threads_row.get_value()),
            password=self.pw_row.get_text() if f.encrypt else "",
            encrypt_names=self.names_row.get_active(),
            zip_aes=self.zipenc_row.get_selected() == 0,
            volume_size=self._volume(), sfx=self.sfx_row.get_active() and f.sfx,
            delete_after=self.del_row.get_active() and not self.convert_from,
            excludes=[p for p in self.excl_row.get_text().split(";") if p.strip()],
            update_mode=UPDATE_VALUES[self.update_row.get_selected()],
            dictionary=DICT_VALUES[self.dict_row.get_selected()] if f.key == "7z" else "",
            extra_args=self.extra_row.get_text() if f.engine == "7z" else "",
        )

    def _validate(self) -> str:
        f = self.fmt
        if not self.out_row.get_text().strip():
            return tr("Indiquez un fichier de destination")
        if not f.multi and not self.each_row.get_active():
            srcs = [self.convert_from] if self.convert_from else self.sources
            if self.convert_from or len(srcs) != 1 or not os.path.isfile(srcs[0]):
                return tr("{0} ne contient qu'un seul fichier : choisissez TAR.* pour un dossier").format(f.label)
        if f.encrypt and self.pw_row.get_text() != self.pw2_row.get_text():
            return tr("Les mots de passe ne correspondent pas")
        if self.vol_row.get_selected() > len(tools.PRESET_VOLUMES) - 1:
            try:
                tools.parse_size(self.vol_custom.get_text())
            except Exception:
                return tr("Taille de volume invalide")
        return ""

    def _refresh(self):
        if self._building:
            return
        self.vol_custom.set_visible(self.vol_row.get_selected() > len(tools.PRESET_VOLUMES) - 1)
        pw = self.pw_row.get_text()
        bits, label = tools.password_strength(pw)
        self.strength.set_value(min(bits, 128))
        self.strength.set_tooltip_text(tr('{label} — {bits:.0f} bits').format(label=label, bits=bits))
        self.strength.set_visible(bool(pw))
        if pw and self.pw2_row.get_text() and pw != self.pw2_row.get_text():
            self.pw2_row.add_css_class("error")
        else:
            self.pw2_row.remove_css_class("error")
        out = self.out_row.get_text().strip()
        self.update_row.set_sensitive(os.path.exists(out))
        problem = self._validate()
        self.action.set_sensitive(not problem)
        self.action.set_tooltip_text(problem or None)
        if problem:
            self.cmd_label.set_text("⚠ " + problem)
            return
        srcs = [self.convert_from] if self.convert_from else self.sources
        o = self.options()
        if self.convert_from:
            self.cmd_label.set_text("1. " + archive.describe_extract(self.convert_from, archive.ExtractOptions(dest="<temp>"))
                                    + "\n2. " + archive.describe_create(["<temp>/*"], out, o))
        else:
            self.cmd_label.set_text(archive.describe_create(srcs if not self.each_row.get_active() else srcs[:1],
                                                            out, o))

    def _accept(self, *_):
        if self._validate():
            return
        o = self.options()
        settings.data["create_format"] = o.fmt
        settings.data["create_level"] = self.level_row.get_selected()
        settings.data["create_solid"] = self.solid_row.get_active()
        settings.data["create_encrypt_names"] = self.names_row.get_active()
        settings.save()
        out = self.out_row.get_text().strip()
        if o.sfx and not out.endswith(".run"):
            base, _ = formats.split_archive_name(out)
            out = base + ".run"
        self.dialog.close()
        self.on_accept(self.sources, out, o, self.each_row.get_active())

    def present(self, parent):
        self.dialog.present(parent)


SUBFOLDER_LABELS = [tr("Intelligent (si plusieurs éléments)"), tr("Toujours"), tr("Jamais")]
SUBFOLDER_VALUES = ["smart", "always", "never"]
OVERWRITE_LABELS = [tr("Renommer les nouveaux fichiers"), tr("Remplacer"), tr("Ignorer"), tr("Renommer les fichiers existants")]
OVERWRITE_VALUES = ["rename", "overwrite", "skip", "rename_existing"]


class ExtractDialog:
    def __init__(self, parent: Gtk.Widget, archives: list[str],
                 on_accept: Callable[[list, archive.ExtractOptions], None],
                 files: Optional[list] = None, password: str = "", dest: str = ""):
        self.archives = archives
        self.files = files or []
        self.on_accept = on_accept
        title = tr("Extraire la sélection") if self.files else tr("Extraire")
        self.dialog, page, self.action = dialog_shell(title, tr("Extraire"), width=560, height=600)

        summary = Adw.PreferencesGroup()
        hero = Gtk.Box(spacing=14, css_classes=["dialog-hero"])
        hero.append(Gtk.Image(icon_name="folder-download-symbolic", pixel_size=36, css_classes=["hero-icon"]))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        col.append(Gtk.Label(label=describe_paths(archives), xalign=0, css_classes=["title-4"], ellipsize=3))
        sub = tr('{0} élément(s) sélectionné(s)').format(len(self.files)) if self.files else \
            human_size(sum(os.path.getsize(a) for a in archives if os.path.exists(a)))
        col.append(Gtk.Label(label=sub, xalign=0, css_classes=["dim-label"]))
        hero.append(col)
        summary.add(hero)
        page.add(summary)

        g = Adw.PreferencesGroup(title=tr("Destination"))
        self.dest_row = path_row(tr("Dossier"), dest or os.path.dirname(os.path.abspath(archives[0])),
                                 lambda r: choose_folder(self.dialog, tr("Dossier de destination"), r.get_text(), r.set_text))
        g.add(self.dest_row)
        self.sub_row = combo_row(tr("Créer un sous-dossier"), SUBFOLDER_LABELS,
                                 SUBFOLDER_VALUES.index(settings["extract_subfolder"])
                                 if settings["extract_subfolder"] in SUBFOLDER_VALUES else 0)
        self.sub_row.set_visible(not self.files)
        g.add(self.sub_row)
        self.over_row = combo_row(tr("Fichiers déjà présents"), OVERWRITE_LABELS,
                                  OVERWRITE_VALUES.index(settings["extract_overwrite"])
                                  if settings["extract_overwrite"] in OVERWRITE_VALUES else 0)
        g.add(self.over_row)
        page.add(g)

        g2 = Adw.PreferencesGroup(title=tr("Options"))
        self.pw_row = Adw.PasswordEntryRow(title=tr("Mot de passe (si l'archive est protégée)"), text=password)
        g2.add(self.pw_row)
        self.paths_row = Adw.SwitchRow(title=tr("Conserver l'arborescence"), active=True)
        g2.add(self.paths_row)
        self.open_row = Adw.SwitchRow(title=tr("Ouvrir le dossier une fois terminé"),
                                      active=settings["open_after_extract"])
        g2.add(self.open_row)
        self.del_row = Adw.SwitchRow(title=tr("Supprimer l'archive après extraction"))
        self.del_row.set_visible(not self.files)
        g2.add(self.del_row)
        page.add(g2)

        cmd_group, self.cmd_label = command_group()
        page.add(cmd_group)
        for r in (self.dest_row, self.pw_row):
            r.connect("changed", lambda *_: self._refresh())
        for r in (self.sub_row, self.over_row):
            r.connect("notify::selected", lambda *_: self._refresh())
        for r in (self.paths_row, self.del_row):
            r.connect("notify::active", lambda *_: self._refresh())
        self.action.connect("clicked", self._accept)
        self._refresh()

    def options(self) -> archive.ExtractOptions:
        return archive.ExtractOptions(
            dest=self.dest_row.get_text().strip(), password=self.pw_row.get_text(),
            overwrite=OVERWRITE_VALUES[self.over_row.get_selected()],
            subfolder="never" if self.files else SUBFOLDER_VALUES[self.sub_row.get_selected()],
            keep_paths=self.paths_row.get_active(), files=list(self.files),
            open_after=self.open_row.get_active(), delete_after=self.del_row.get_active())

    def _refresh(self):
        o = self.options()
        ok = bool(o.dest)
        self.action.set_sensitive(ok)
        lines = []
        for a in self.archives[:3]:
            lines.append(archive.describe_extract(a, o))
        if len(self.archives) > 3:
            lines.append(tr('… et {0} autre(s)').format(len(self.archives) - 3))
        self.cmd_label.set_text("\n".join(lines))

    def _accept(self, *_):
        o = self.options()
        settings.data["extract_overwrite"] = o.overwrite
        if not self.files:
            settings.data["extract_subfolder"] = o.subfolder
        settings.data["open_after_extract"] = o.open_after
        settings.save()
        self.dialog.close()
        self.on_accept(self.archives, o)

    def present(self, parent):
        self.dialog.present(parent)


INFO_KEYS = [
    ("Type", tr("Format")), ("Method", tr("Méthode")), ("Solid", tr("Solide")), ("Blocks", tr("Blocs")),
    ("Headers Size", tr("Taille des en-têtes")), ("Physical Size", tr("Taille sur disque")),
    ("Volumes", tr("Volumes")), ("Multivolume", tr("Multivolume")), ("Comment", tr("Commentaire")),
    ("Code Page", tr("Page de code")), ("Characteristics", tr("Caractéristiques")),
]


def show_archive_info(parent: Gtk.Widget, listing: archive.ArchiveListing):
    dlg = DialogWindow(title=tr("Propriétés de l'archive"), content_width=520, content_height=620)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()
    phys = os.path.getsize(listing.path) if os.path.exists(listing.path) else 0
    total = listing.total_size

    hero_g = Adw.PreferencesGroup()
    hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["info-hero"])
    hero.append(Gtk.Image(icon_name="package-x-generic", pixel_size=64))
    hero.append(Gtk.Label(label=os.path.basename(listing.path), css_classes=["title-2"], wrap=True,
                          justify=Gtk.Justification.CENTER))
    if total and phys:
        ratio = phys / total
        bar = Gtk.LevelBar(min_value=0, max_value=1, value=min(ratio, 1), css_classes=["ratio-bar"],
                           margin_start=40, margin_end=40, margin_top=6)
        hero.append(bar)
        hero.append(Gtk.Label(label=tr("Compressée à {0} % · gain de {1}").format(decimal(ratio * 100), human_size(max(0, total - phys))),
                              css_classes=["dim-label"]))
    hero_g.add(hero)
    page.add(hero_g)

    def add(group, title, value):
        r = Adw.ActionRow(title=title, subtitle=str(value), subtitle_selectable=True)
        r.add_css_class("property")
        group.add(r)

    g = Adw.PreferencesGroup(title=tr("Contenu"))
    add(g, tr("Emplacement"), listing.path)
    add(g, tr("Fichiers"), listing.file_count)
    add(g, tr("Dossiers"), listing.dir_count)
    add(g, tr("Taille décompressée"), tr('{0} ({total:,} octets)').format(human_size(total), total=total).replace(",", " "))
    add(g, tr("Taille compressée"), tr('{0} ({phys:,} octets)').format(human_size(phys), phys=phys).replace(",", " "))
    add(g, tr("Chiffrée"), tr("Oui 🔒") if listing.encrypted else tr("Non"))
    page.add(g)

    g2 = Adw.PreferencesGroup(title=tr("Format"))
    for key, label in INFO_KEYS:
        v = listing.info.get(key)
        if v not in (None, ""):
            if key in ("Physical Size", "Headers Size"):
                try:
                    v = human_size(int(v))
                except ValueError:
                    pass
            if v in ("+", "-"):
                v = tr("Oui") if v == "+" else tr("Non")
            add(g2, label, v)
    methods = sorted({e.method for e in listing.entries if e.method})
    if methods and "Method" not in listing.info:
        add(g2, tr("Méthodes"), ", ".join(methods[:6]))
    page.add(g2)
    tv.set_content(page)
    dlg.set_child(tv)
    dlg.present(parent)


def apply_theme():
    sm = Adw.StyleManager.get_default()
    sm.set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(settings["theme"], Adw.ColorScheme.DEFAULT))
