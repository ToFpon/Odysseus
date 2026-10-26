"""Fonctionnalités intégrées, issues des extensions Nautilus de Tof, toutes désactivables.

Remplace le gestionnaire d'extensions : chaque fonctionnalité s'active ou se désactive dans
Préférences → Fonctionnalités, sans redémarrage. Les doublons des extensions d'origine sont
fusionnés (Archive Browser + Extract Here, Hidden Dim icon/all…).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from gi.repository import Gio

from .i18n import tr
from .settings import settings


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str
    icon: str
    category: str
    shortcut: str = ""
    # Programmes nécessaires : chaque groupe est une liste d'alternatives (l'un d'eux suffit).
    tools: tuple = field(default_factory=tuple)
    packages: str = ""          # paquets Debian/Ubuntu à installer si un programme manque
    python: tuple = field(default_factory=tuple)   # modules Python facultatifs
    uri_scheme: str = ""        # schéma GVfs nécessaire (admin…)
    default: bool = True


CATEGORIES = [
    ("view", tr("Affichage")),
    ("files", tr("Fichiers")),
    ("pdf", tr("PDF")),
    ("media", tr("Multimédia")),
    ("system", tr("Système")),
]

FEATURES = [
    Feature("dual_panel", tr("Double panneau"),
            tr("Deux emplacements côte à côte, copie et déplacement d'un panneau à l'autre"),
            "view-dual-symbolic", "view", "F3"),
    Feature("column_view", tr("Vue en colonnes"),
            tr("Colonnes enchaînées à la manière du Finder pour descendre rapidement dans l'arborescence"),
            "odysseus-view-columns-symbolic", "view", "F9 / Ctrl+3"),
    Feature("preview", tr("Panneau d'aperçu"),
            tr("Aperçu du fichier sélectionné : images, vidéos, PDF, textes, informations détaillées"),
            "view-reveal-symbolic", "view", "F4",
            tools=(("ffmpegthumbnailer", "ffmpeg"),), packages="ffmpegthumbnailer"),
    Feature("thumbnails", tr("Miniatures"),
            tr("Miniatures des images, vidéos et PDF, partagées avec Nautilus"),
            "image-x-generic-symbolic", "view"),
    Feature("hidden_dim", tr("Atténuer les fichiers cachés"),
            tr("Les fichiers cachés sont estompés quand ils sont affichés"),
            "view-conceal-symbolic", "view"),
    Feature("cut_dim", tr("Atténuer les éléments coupés"),
            tr("Les éléments coupés (Ctrl+X) sont estompés jusqu'au collage"),
            "edit-cut-symbolic", "view"),
    Feature("copy_mark", tr("Marquer les éléments copiés"),
            tr("Une barre colorée devant les éléments copiés (Ctrl+C), tant qu'ils sont dans le presse-papiers"),
            "edit-copy-symbolic", "view"),
    Feature("folder_colors", tr("Couleurs et emblèmes"),
            tr("Colorer les dossiers et poser des emblèmes, compatible avec Folder Color de Nautilus"),
            "color-select-symbolic", "view"),
    Feature("duration_column", tr("Colonne Durée"),
            tr("Durée des fichiers audio et vidéo (ffprobe), avec cache"),
            "media-playback-start-symbolic", "view",
            tools=(("ffprobe",),), packages="ffmpeg"),
    Feature("archives", tr("Archives"),
            tr("Parcourir les archives comme des dossiers, extraire ici, compresser (moteur de Pressoir)"),
            "package-x-generic-symbolic", "files", "F7",
            tools=(("7zz", "7z", "7za"),), packages="7zip"),
    Feature("content_search", tr("Recherche dans le contenu"),
            tr("Rechercher et remplacer du texte dans les fichiers avec ripgrep ou grep"),
            "edit-find-replace-symbolic", "files", "F8",
            tools=(("rg", "grep"),), packages="ripgrep"),
    Feature("text_editor", tr("Ouvrir avec l'éditeur de texte"),
            tr("Ouvrir directement les fichiers texte sélectionnés dans l'éditeur choisi"),
            "accessories-text-editor-symbolic", "files"),
    Feature("terminal", tr("Terminal ici"),
            tr("Ouvrir un terminal dans le dossier courant"),
            "utilities-terminal-symbolic", "files"),
    Feature("gsconnect", tr("Envoyer vers un téléphone"),
            tr("Envoyer des fichiers vers un appareil connecté avec GSConnect, comme dans Nautilus"),
            "phone-symbolic", "files"),
    Feature("checksums", tr("Sommes de contrôle"),
            tr("Calculer l'empreinte d'un fichier et la comparer à celle publiée, vérifier les "
               "fichiers .sha256 / .md5 / .sfv (menu du clic droit)"),
            "security-high-symbolic", "files"),
    Feature("progress_window", tr("Fenêtre de progression"),
            tr("Une fenêtre suit les copies et déplacements longs ; les opérations courtes restent discrètes"),
            "emblem-synchronizing-symbolic", "files"),
    Feature("pdf_compress", tr("Compresser un PDF"),
            tr("Réduire la taille des PDF avec Ghostscript, comparaison avant / après"),
            "x-office-document-symbolic", "pdf",
            tools=(("gs", "ghostscript"),), packages="ghostscript"),
    Feature("pdf_merge", tr("Fusionner des PDF"),
            tr("Assembler plusieurs PDF dans l'ordre voulu, avec choix des pages"),
            "list-add-symbolic", "pdf",
            tools=(("qpdf", "gs", "ghostscript"),), packages="qpdf"),
    Feature("pdf_watermark", tr("Filigrane PDF"),
            tr("Texte ou image en filigrane, position, pages, aplatissement de sécurité"),
            "security-high-symbolic", "pdf",
            tools=(("qpdf",),), packages="qpdf python3-cairo", python=("cairo",)),
    Feature("pdf_pages", tr("Pages PDF"),
            tr("Extraire, supprimer, faire pivoter ou éclater les pages d'un PDF"),
            "view-paged-symbolic", "pdf",
            tools=(("qpdf",),), packages="qpdf"),
    Feature("video_to_audio", tr("Vidéo vers audio"),
            tr("Extraire la piste audio des vidéos : MP3, M4A, OGG, OPUS, FLAC, WAV"),
            "audio-x-generic-symbolic", "media",
            tools=(("ffmpeg",),), packages="ffmpeg"),
    Feature("image_tools", tr("Outils image"),
            tr("Redimensionner, pivoter et convertir des images (JPEG, PNG, WebP, HEIC…), plusieurs à la fois"),
            "image-x-generic-symbolic", "media",
            tools=(("magick", "convert"),), packages="imagemagick"),
    Feature("annotate", tr("Annoter une image"),
            tr("Flèches, cadres, texte, surligneur et dessin à main levée sur une image"),
            "applications-graphics-symbolic", "media", python=("cairo",)),
    Feature("admin", tr("Ouvrir en tant qu'administrateur"),
            tr("Ouvrir un dossier dans une fenêtre administrateur, ou un fichier avec les droits root "
               "(emplacement admin:// de GVfs, mot de passe demandé par polkit)"),
            "dialog-password-symbolic", "system", packages="gvfs-backends", uri_scheme="admin"),
    Feature("deb_installer", tr("Installateur de paquets .deb"),
            tr("Installer un paquet .deb avec contrôle des dépendances et suivi en direct"),
            "system-software-install-symbolic", "system",
            tools=(("dpkg-deb",), ("pkexec",)), packages="policykit-1"),
]

BY_ID = {f.id: f for f in FEATURES}


def enabled(fid: str) -> bool:
    f = BY_ID.get(fid)
    if f is None:
        return False
    return bool(settings["features"].get(fid, f.default))


def set_enabled(fid: str, value: bool):
    feats = dict(settings["features"])
    feats[fid] = bool(value)
    settings["features"] = feats


# Programme ou module Python → paquet Ubuntu 24.04 qui le fournit
PACKAGE_OF = {"7zz": "7zip", "7z": "7zip", "7za": "7zip", "gs": "ghostscript", "ghostscript": "ghostscript",
              "qpdf": "qpdf", "ffmpeg": "ffmpeg", "magick": "imagemagick", "convert": "imagemagick",
              "lib:heif-libde265": "libheif-plugin-libde265", "ffprobe": "ffmpeg", "ffmpegthumbnailer": "ffmpegthumbnailer",
              "rg": "ripgrep", "grep": "grep", "pdftoppm": "poppler-utils", "zstd": "zstd", "brotli": "brotli",
              "lz4": "lz4", "dpkg-deb": "dpkg", "pkexec": "pkexec", "py:pypdf": "python3-pypdf",
              "py:cairo": "python3-cairo python3-gi-cairo", "gir:Poppler": "gir1.2-poppler-0.18"}


# greffons de bibliothèques (pas de programme à chercher dans le PATH) : fichier installé par le paquet
LIBS = {"lib:heif-libde265": "/usr/lib/*/libheif/plugins/libheif-libde265.so"}


def _lib_path(name: str) -> str:
    import glob
    found = glob.glob(LIBS.get(name, ""))
    return found[0] if found else ""


def present(name: str) -> bool:
    """Programme dans le PATH, module Python (« py:cairo ») ou bibliothèque GObject (« gir:Poppler »)."""
    if name.startswith("py:"):
        import importlib.util
        return importlib.util.find_spec(name[3:]) is not None
    if name.startswith("lib:"):
        return bool(_lib_path(name))
    if name.startswith("gir:"):
        import gi
        try:
            gi.require_version(name[4:], {"Poppler": "0.18"}.get(name[4:], "1.0"))
            return True
        except ValueError:
            return False
    return shutil.which(name) is not None


def _missing(fid: str) -> list[tuple[str, str]]:
    """(élément manquant, paquet qui l'apporte), un par groupe d'alternatives non satisfait."""
    f = BY_ID[fid]
    out = []
    for group in f.tools:
        if not any(shutil.which(t) for t in group):
            out.append((" / ".join(group), PACKAGE_OF.get(group[0], f.packages)))
    if f.uri_scheme and f.uri_scheme not in Gio.Vfs.get_default().get_supported_uri_schemes():
        out.append(("gvfs-backends", "gvfs-backends"))
    for mod in f.python:
        if not present("py:" + mod):
            out.append(("python3-" + mod, PACKAGE_OF.get("py:" + mod, "python3-" + mod)))
    return out


def missing_tools(fid: str) -> list[str]:
    """Programmes manquants (un par groupe d'alternatives non satisfait)."""
    return [name for name, _pkg in _missing(fid)]


def install_command(fid: str) -> str:
    """« sudo apt install … » pour ce qui manque à cette fonctionnalité (vide si rien ne manque)."""
    return apt_command(pkg for _name, pkg in _missing(fid))


def apt_command(packages) -> str:
    pkgs = []
    for p in packages:
        for one in p.split():
            if one not in pkgs:
                pkgs.append(one)
    return "sudo apt install " + " ".join(pkgs) if pkgs else ""


# Onglet « Composants » des préférences : programmes externes, groupés par usage.
# (nom affiché, alternatives — l'une suffit —, à quoi il sert)
COMPONENTS = [
    (tr("Archives"), [
        ("7-Zip", ("7zz", "7z", "7za"), tr("Compresser, extraire et parcourir les archives (indispensable)")),
        ("Zstandard", ("zstd",), tr("Archives .tar.zst")),
        ("Brotli", ("brotli",), tr("Archives .tar.br")),
        ("LZ4", ("lz4",), tr("Archives .tar.lz4")),
    ]),
    (tr("PDF"), [
        ("Ghostscript", ("gs",), tr("Compresser un PDF ; fusion en secours")),
        ("qpdf", ("qpdf",), tr("Fusionner, filigrane, pages d'un PDF")),
        ("Poppler", ("gir:Poppler", "pdftoppm"), tr("Aperçu et miniatures des PDF")),
    ]),
    (tr("Multimédia"), [
        ("FFmpeg", ("ffmpeg",), tr("Vidéo vers audio, miniatures des vidéos")),
        ("ffprobe", ("ffprobe",), tr("Colonne Durée")),
        ("ffmpegthumbnailer", ("ffmpegthumbnailer",), tr("Miniatures des vidéos, plus rapides qu'avec FFmpeg seul")),
        ("ImageMagick", ("magick", "convert"), tr("Redimensionner, pivoter et convertir des images")),
        ("libde265 (HEIC)", ("lib:heif-libde265",), tr("Lire les photos HEIC des iPhone (outils image, miniatures)")),
        ("Cairo (Python)", ("py:cairo",), tr("Annoter une image, filigrane PDF")),
    ]),
    (tr("Recherche"), [
        ("ripgrep", ("rg",), tr("Recherche rapide dans le contenu des fichiers (sinon grep, plus lent)")),
    ]),
]


def component_path(alternatives) -> str:
    """Chemin (ou nom) de la première alternative présente, vide si aucune."""
    for a in alternatives:
        if present(a):
            if a.startswith("py:"):
                return "Python : " + a[3:]
            if a.startswith("gir:"):
                return a[4:] + " (GObject Introspection)"
            if a.startswith("lib:"):
                return _lib_path(a)
            return shutil.which(a)
    return ""


def available(fid: str) -> bool:
    """Activée et utilisable (programmes indispensables présents)."""
    if not enabled(fid):
        return False
    if fid in ("preview", "pdf_merge"):      # outils facultatifs : dégradation gracieuse
        return True
    return not missing_tools(fid)
