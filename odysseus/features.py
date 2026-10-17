"""Fonctionnalités intégrées, issues des extensions Nautilus de Tof, toutes désactivables.

Remplace le gestionnaire d'extensions : chaque fonctionnalité s'active ou se désactive dans
Préférences → Fonctionnalités, sans redémarrage. Les doublons des extensions d'origine sont
fusionnés (Archive Browser + Extract Here, Hidden Dim icon/all…).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

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
            tools=(("qpdf", "gs", "ghostscript"),), packages="qpdf", python=("pypdf",)),
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
    Feature("annotate", tr("Annoter une image"),
            tr("Flèches, cadres, texte, surligneur et dessin à main levée sur une image"),
            "applications-graphics-symbolic", "media", python=("cairo",)),
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


def missing_tools(fid: str) -> list[str]:
    """Programmes manquants (un par groupe d'alternatives non satisfait)."""
    f = BY_ID[fid]
    out = []
    for group in f.tools:
        if not any(shutil.which(t) for t in group):
            out.append(" / ".join(group))
    for mod in f.python:
        try:
            __import__(mod)
        except ImportError:
            out.append("python3-" + mod)
    return out


def available(fid: str) -> bool:
    """Activée et utilisable (programmes indispensables présents)."""
    if not enabled(fid):
        return False
    if fid in ("preview", "pdf_merge"):      # outils facultatifs : dégradation gracieuse
        return True
    return not missing_tools(fid)
