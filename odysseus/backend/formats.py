"""Formats d'archives connus et détection par extension."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from ..i18n import tr

# Compresseurs de flux : extension -> (programme, args compression, args décompression)
STREAM_TOOLS = {
    "gz": ("gzip", ["-c"], ["-dc"]),
    "bz2": ("bzip2", ["-c"], ["-dc"]),
    "xz": ("xz", ["-c", "-T0"], ["-dc"]),
    "zst": ("zstd", ["-c", "-q", "-T0"], ["-dc", "-q"]),
    "br": ("brotli", ["-c"], ["-dc"]),
    "lz4": ("lz4", ["-c", "-q"], ["-dc", "-q"]),
    "lzma": ("xz", ["--format=lzma", "-c"], ["--format=lzma", "-dc"]),
    "z": ("gzip", ["-c"], ["-dc"]),
}

# Alias d'extensions tar compressées.
TAR_ALIASES = {
    "tgz": "gz", "tpz": "gz", "taz": "z",
    "tbz": "bz2", "tbz2": "bz2", "tb2": "bz2",
    "txz": "xz", "tlz": "lzma",
    "tzst": "zst", "tzs": "zst",
    "tlz4": "lz4", "tbr": "br",
}

# Niveaux : (min, max) acceptés par chaque compresseur de flux.
STREAM_LEVELS = {
    "gz": (1, 9), "bz2": (1, 9), "xz": (0, 9), "lzma": (0, 9),
    "zst": (1, 19), "br": (0, 11), "lz4": (1, 12), "z": (1, 9),
}

# Extensions que 7-Zip sait lire (liste non exhaustive, utilisée pour reconnaître
# une archive dans le gestionnaire de fichiers).
READABLE_EXTS = {
    "7z", "zip", "zipx", "rar", "r00", "tar", "gz", "tgz", "bz2", "tbz", "tbz2",
    "xz", "txz", "lzma", "tlz", "zst", "tzst", "br", "lz4", "z", "taz", "wim",
    "swm", "esd", "cab", "arj", "lzh", "lha", "iso", "img", "udf", "dmg", "hfs",
    "vhd", "vhdx", "vmdk", "vdi", "qcow2", "squashfs", "cramfs", "cpio", "rpm",
    "deb", "udeb", "ar", "a", "xar", "pkg", "chm", "msi", "nsis", "apk", "jar",
    "war", "ear", "xpi", "epub", "odt", "ods", "odp", "docx", "xlsx", "pptx",
    "ipa", "appx", "crx", "001", "apfs", "ext4", "fat", "ntfs", "pmd", "zipx",
}


@dataclass(frozen=True)
class CreateFormat:
    key: str            # identifiant interne
    label: str          # nom affiché
    ext: str            # extension produite
    engine: str         # "7z", "tar", "stream"
    sevenz_type: str = ""
    methods: tuple = ()
    encrypt: bool = False
    encrypt_names: bool = False
    solid: bool = False
    volumes: bool = False
    multi: bool = True       # accepte plusieurs fichiers/dossiers
    sfx: bool = False
    tool: str = ""           # programme externe requis
    levels: tuple = (0, 9)
    notes: str = ""

    def available(self) -> bool:
        from .engine import sevenzip_path
        if self.engine == "7z" and not sevenzip_path():
            return False
        if self.tool and not shutil.which(self.tool):
            return False
        if self.engine == "tar" and not shutil.which("tar"):
            return False
        return True


CREATE_FORMATS: list[CreateFormat] = [
    CreateFormat("7z", "7Z", "7z", "7z", "7z",
                 methods=("LZMA2", "LZMA", "PPMd", "BZip2", "Deflate", "Copy"),
                 encrypt=True, encrypt_names=True, solid=True, volumes=True, sfx=True,
                 notes=tr("Meilleur taux de compression, chiffrement AES-256")),
    CreateFormat("zip", "ZIP", "zip", "7z", "zip",
                 methods=("Deflate", "Deflate64", "BZip2", "LZMA", "PPMd", "Copy"),
                 encrypt=True, volumes=True,
                 notes=tr("Compatible partout")),
    CreateFormat("tar", "TAR", "tar", "7z", "tar", levels=(0, 0), volumes=True,
                 notes=tr("Regroupement sans compression, conserve les droits Unix")),
    CreateFormat("tar.gz", "TAR.GZ", "tar.gz", "tar", tool="gzip", levels=(1, 9)),
    CreateFormat("tar.bz2", "TAR.BZ2", "tar.bz2", "tar", tool="bzip2", levels=(1, 9)),
    CreateFormat("tar.xz", "TAR.XZ", "tar.xz", "tar", tool="xz", levels=(0, 9)),
    CreateFormat("tar.zst", "TAR.ZST", "tar.zst", "tar", tool="zstd", levels=(1, 19),
                 notes=tr("Très rapide, excellent compromis")),
    CreateFormat("tar.br", "TAR.BR", "tar.br", "tar", tool="brotli", levels=(0, 11)),
    CreateFormat("tar.lz4", "TAR.LZ4", "tar.lz4", "tar", tool="lz4", levels=(1, 12),
                 notes=tr("Le plus rapide")),
    CreateFormat("wim", "WIM", "wim", "7z", "wim", levels=(0, 0),
                 notes=tr("Image Windows, déduplication")),
    CreateFormat("gz", tr("GZ (un fichier)"), "gz", "stream", tool="gzip", multi=False, levels=(1, 9)),
    CreateFormat("bz2", tr("BZ2 (un fichier)"), "bz2", "stream", tool="bzip2", multi=False, levels=(1, 9)),
    CreateFormat("xz", tr("XZ (un fichier)"), "xz", "stream", tool="xz", multi=False, levels=(0, 9)),
    CreateFormat("zst", tr("ZST (un fichier)"), "zst", "stream", tool="zstd", multi=False, levels=(1, 19)),
    CreateFormat("br", tr("BR (un fichier)"), "br", "stream", tool="brotli", multi=False, levels=(0, 11)),
    CreateFormat("lz4", tr("LZ4 (un fichier)"), "lz4", "stream", tool="lz4", multi=False, levels=(1, 12)),
]

FORMATS_BY_KEY = {f.key: f for f in CREATE_FORMATS}


@dataclass
class Detected:
    """Résultat de la détection d'un fichier archive."""
    kind: str                 # "7z" (lu par 7-Zip), "tar-stream", "stream", "none"
    stream: str = ""          # extension de flux (gz, zst...) pour tar-stream/stream
    inner_name: str = ""      # nom du fichier décompressé pour un flux simple
    base_name: str = ""       # nom sans extension(s) d'archive
    extras: dict = field(default_factory=dict)


def split_archive_name(name: str) -> tuple[str, str]:
    """Sépare 'photos.tar.gz' en ('photos', 'tar.gz')."""
    lower = name.lower()
    for comp in STREAM_TOOLS:
        suffix = f".tar.{comp}"
        if lower.endswith(suffix):
            return name[: -len(suffix)], suffix[1:]
    # volumes : archive.7z.001, archive.zip.001
    root, ext = os.path.splitext(name)
    if ext.lower() == ".001":
        r2, e2 = os.path.splitext(root)
        if e2:
            return r2, e2[1:].lower() + ".001"
        return root, "001"
    if ext:
        return root, ext[1:].lower()
    return name, ""


def detect(path: str) -> Detected:
    name = os.path.basename(path)
    base, ext = split_archive_name(name)
    lower_ext = ext.lower()
    if lower_ext.startswith("tar.") and lower_ext[4:] in STREAM_TOOLS:
        return Detected("tar-stream", lower_ext[4:], base_name=base)
    if lower_ext in TAR_ALIASES:
        return Detected("tar-stream", TAR_ALIASES[lower_ext], base_name=base)
    if lower_ext in ("zst", "br", "lz4"):
        return Detected("stream", lower_ext, inner_name=base, base_name=base)
    if lower_ext in READABLE_EXTS or lower_ext.endswith(".001"):
        return Detected("7z", base_name=base)
    return Detected("none", base_name=base)


def is_archive(path: str) -> bool:
    return os.path.isfile(path) and detect(path).kind != "none"


def is_modifiable(path: str) -> bool:
    """7-Zip peut ajouter / supprimer / renommer dans ces formats."""
    _, ext = split_archive_name(os.path.basename(path))
    return ext.lower() in ("7z", "zip", "tar", "wim")


# Archives « ordinaires » : ouvertes comme des dossiers au double-clic et proposées à
# l'extraction. Les autres formats lisibles (documents Office, EPUB, paquets, images disque…)
# s'ouvrent avec leur application, mais peuvent être parcourus depuis le menu contextuel.
PLAIN_ARCHIVE_EXTS = {
    "7z", "zip", "zipx", "rar", "r00", "tar", "gz", "tgz", "bz2", "tbz", "tbz2", "xz", "txz",
    "lzma", "tlz", "zst", "tzst", "br", "lz4", "z", "taz", "wim", "cab", "arj", "lzh", "lha", "cpio", "001",
}


def looks_like_archive(name: str) -> bool:
    """D'après le nom seul (sans accès disque) : archive ordinaire ?"""
    _, ext = split_archive_name(name)
    ext = ext.lower()
    return ext in PLAIN_ARCHIVE_EXTS or ext.startswith("tar.") or ext.endswith(".001") or ext in TAR_ALIASES


def readable(name: str) -> bool:
    """D'après le nom seul : 7-Zip peut-il lire ce fichier ?"""
    return detect(name).kind != "none"
