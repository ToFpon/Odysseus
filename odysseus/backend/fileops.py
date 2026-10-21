"""Opérations sur les fichiers : copier, déplacer, corbeille, supprimer, créer, renommer.

Tout s'exécute dans une tâche de fond (engine.Job) avec progression, vitesse et annulation.
La copie passe par os.copy_file_range (copie dans le noyau, très rapide) par blocs de 8 Mo pour
suivre l'avancement ; un déplacement sur le même disque n'est qu'un renommage instantané.
Les conflits de noms sont posés à l'utilisateur par la fonction `ask`, appelée depuis le fil de
la tâche (elle attend la réponse de l'interface).
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import shutil
import stat as st
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from gi.repository import Gio, GLib

from ..i18n import ntr, tr
from . import fsinfo
from .engine import Cancelled, Job, ToolError, human_size

CHUNK = 8 * 1024 * 1024


class _Taken(Exception):
    """Le nom visé a été pris entre la vérification du conflit et la création (autre copie lancée
    en même temps, depuis une autre fenêtre…) : le conflit est alors posé, rien n'est écrasé."""


@dataclass
class Conflict:
    src: str
    dst: str
    src_is_dir: bool
    dst_is_dir: bool
    move: bool = False


# Réponses possibles : "replace", "merge" (dossiers), "rename", "skip", "cancel".
Asker = Callable[[Conflict], tuple]


@dataclass
class Result:
    """Ce qui a été fait (pour annuler, sélectionner les nouveaux éléments, signaler les erreurs)."""
    kind: str
    created: list = field(default_factory=list)      # chemins créés au premier niveau
    moved: list = field(default_factory=list)        # (ancien, nouveau)
    trashed: list = field(default_factory=list)      # chemins d'origine
    errors: list = field(default_factory=list)       # (chemin, message)
    # Pour annuler : seulement ce que l'opération a CRÉÉ. Dans un dossier fusionné, ce sont les
    # éléments ajoutés dedans, pas le dossier (qui existait) : sinon l'annulation emportait aussi
    # ce qu'il contenait déjà.
    new_items: list = field(default_factory=list)    # chemins créés (racines des créations)
    new_moves: list = field(default_factory=list)    # (ancien, nouveau) des éléments déplacés
    no_trash: list = field(default_factory=list)     # corbeille impossible sur ce support


# ---------------------------------------------------------------------------
# Noms
# ---------------------------------------------------------------------------

def split_ext(name: str) -> tuple[str, str]:
    """« photo.tar.gz » → (« photo », « .tar.gz ») ; les dossiers et fichiers cachés gardent leur nom."""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    low = name.lower()
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz4", ".tar.br", ".tar.lzma"):
        if low.endswith(double) and len(name) > len(double):
            return name[:-len(double)], name[-len(double):]
    base, ext = os.path.splitext(name)
    return (base, ext) if base else (name, "")


def unique_name(folder: str, name: str, is_dir: bool = False, copy_word: bool = False) -> str:
    """Nom libre : « nom (copie).ext », « nom (copie 2).ext »… ou « nom (2).ext »."""
    if not os.path.lexists(os.path.join(folder, name)):
        return name
    base, ext = (name, "") if is_dir else split_ext(name)
    n = 1
    while True:
        if copy_word:
            suffix = tr(" (copie)") if n == 1 else tr(" (copie {0})").format(n)
        else:
            suffix = f" ({n + 1})"
        cand = f"{base}{suffix}{ext}"
        if not os.path.lexists(os.path.join(folder, cand)):
            return cand
        n += 1


def valid_name(name: str) -> str:
    """Message d'erreur si le nom n'est pas utilisable, sinon chaîne vide."""
    if not name or not name.strip():
        return tr("Le nom ne peut pas être vide")
    if "/" in name:
        return tr("Le nom ne peut pas contenir « / »")
    if name in (".", ".."):
        return tr("« {0} » n'est pas un nom valide").format(name)
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        return tr("Le nom est trop long")
    return ""


# ---------------------------------------------------------------------------
# Mesure
# ---------------------------------------------------------------------------

def measure(job: Optional[Job], paths: list[str]) -> tuple[int, int]:
    """(octets, nombre de fichiers) d'une sélection, liens non suivis."""
    total = files = 0
    stack = list(paths)
    last = time.monotonic()
    while stack:
        p = stack.pop()
        try:
            s = os.lstat(p)
        except OSError:
            continue
        if st.S_ISDIR(s.st_mode):
            try:
                with os.scandir(p) as it:
                    stack.extend(e.path for e in it)
            except OSError:
                pass
        else:
            total += s.st_size
            files += 1
        if job is not None:
            job.check_cancel()
            now = time.monotonic()
            if now - last > 0.2:
                last = now
                job.progress(None, tr("Préparation… {0} fichiers, {1}").format(files, human_size(total)))
    return total, files


# ---------------------------------------------------------------------------
# Copie / déplacement
# ---------------------------------------------------------------------------

class _Copier:
    def __init__(self, job: Job, ask: Asker, total: int, files: int, move: bool):
        self.job = job
        self.ask = ask
        self.total = max(total, 1)
        self.files = files
        self.done = 0
        self.done_files = 0
        self.move = move
        self.sticky: dict = {}       # décision « pour tous » par type de conflit
        self.errors: list = []
        self.started = time.monotonic()
        self._last = 0.0
        self.current = ""
        self.copied: set = set()     # sources (fichiers, liens) copiées avec succès
        self.created_here: set = set()   # destinations créées par cette opération (casefold)
        self.windows = False         # destination FAT / exFAT / NTFS : noms et casse « à la Windows »
        self.fat = False             # FAT32 : 4 Go maximum par fichier
        self.sync = False            # fsync de chaque fichier (remplacement : rien de « vide » après)
        self._names: dict = {}       # FAT… : dossier -> {nom.casefold(): nom réel}, voir _existing()
        self.made: set = set()       # destinations créées (chemins exacts)
        self.roots: list = []        # (source, destination) des créations dont le parent existait

    def tick(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last < 0.1:
            return
        self._last = now
        elapsed = max(now - self.started, 0.001)
        speed = self.done / elapsed
        verb = tr("Déplacement") if self.move else tr("Copie")
        status = tr("{verb} de « {name} » — {done} sur {total}").format(
            verb=verb, name=self.current, done=human_size(self.done), total=human_size(self.total))
        if self.files > 1:
            status += tr(" · fichier {0} sur {1}").format(min(self.done_files + 1, self.files), self.files)
        if speed > 0 and elapsed > 1:
            status += f" · {human_size(speed)}/s"
        self.job.progress(self.done / self.total, status)

    def decide(self, c: Conflict) -> str:
        kind = "dir" if (c.src_is_dir and c.dst_is_dir) else "file"
        if kind in self.sticky:
            return self.sticky[kind]
        choice, for_all = self.ask(c)
        if choice == "cancel":
            raise Cancelled()
        if for_all:
            self.sticky[kind] = choice
        return choice

    def _existing(self, dst: str) -> Optional[str]:
        """Nom réel de l'élément qui occupe déjà dst sur une destination FAT / exFAT / NTFS, sans tenir
        compte de la casse. On lit le dossier au lieu de faire confiance à lexists() : le vfat du
        noyau monté en utf8 (clés USB sous Ubuntu) cherche les noms EN tenant compte de la casse, mais
        refuse d'en créer un qui ne diffère que par la casse (« Le fichier existe »)."""
        folder, name = os.path.split(dst)
        names = self._names.get(folder)
        if names is None:
            try:
                names = {n.casefold(): n for n in os.listdir(folder)}
            except OSError:
                names = {}
            self._names[folder] = names
        return names.get(name.casefold())

    def _made(self, src: str, dst: str):
        if os.path.dirname(dst) not in self.made:
            self.roots.append((src, dst))
        self.made.add(dst)

    def _created(self, dst: str):
        self.created_here.add(dst.casefold())
        names = self._names.get(os.path.dirname(dst))
        if names is not None:
            names[os.path.basename(dst).casefold()] = os.path.basename(dst)

    def _free_name(self, folder: str, name: str, is_dir: bool) -> str:
        """« nom (2).ext »… libre, casse comprise sur FAT."""
        if not self.windows:
            return unique_name(folder, name, is_dir)
        base, ext = (name, "") if is_dir else split_ext(name)
        n = 2
        while True:
            cand = f"{base} ({n}){ext}"
            if self._existing(os.path.join(folder, cand)) is None and \
                    not os.path.lexists(os.path.join(folder, cand)):
                return cand
            n += 1

    def copy_file(self, src: str, dst: str, s: os.stat_result):
        self.current = os.path.basename(src)
        if self.fat and s.st_size > fsinfo.FAT_MAX_FILE:
            # refusé d'avance plutôt qu'après avoir écrit 4 Go
            raise OSError(errno.EFBIG, tr("plus de 4 Go : trop gros pour une clé FAT32 (exFAT ou NTFS le permettent)"))
        # Écrit sous un nom caché puis renommé d'un coup une fois complet : Odysseus tué en pleine
        # copie (SIGKILL, plantage, fin de session) ne laisse jamais un fichier tronqué sous le vrai
        # nom, qu'on croirait copié. Le renommage final n'écrase pas (_rename_new) : un nom pris
        # entre-temps par une autre opération repose le conflit au lieu de mélanger deux fichiers.
        if os.path.lexists(dst):
            raise _Taken(dst)
        tmp = _tmp_name(os.path.dirname(dst) or ".", "copie")
        tmp_created = False
        try:
            with open(src, "rb") as fin:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, st.S_IMODE(s.st_mode) | 0o200)
                tmp_created = True
                try:
                    use_cfr = hasattr(os, "copy_file_range")
                    while True:
                        self.job.check_cancel()
                        n = 0
                        if use_cfr:
                            try:
                                n = os.copy_file_range(fin.fileno(), fd, CHUNK)
                            except OSError:
                                use_cfr = False
                                continue
                        else:
                            data = fin.read(CHUNK)
                            if data:
                                os.write(fd, data)
                                n = len(data)
                        if not n:
                            break
                        self.done += n
                        self.tick()
                    if self.sync:
                        os.fsync(fd)       # contenu réellement écrit avant l'échange
                finally:
                    os.close(fd)
            try:
                shutil.copystat(src, tmp)
            except OSError:
                pass
            try:
                _rename_new(tmp, dst)
            except FileExistsError:
                raise _Taken(dst) from None
            tmp_created = False
        except BaseException:
            # annulation, disque plein, clé retirée, nom pris… : pas de fichier à moitié copié
            if tmp_created:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        self.done_files += 1
        self.copied.add(src)
        self._created(dst)
        self._made(src, dst)

    def copy_tree(self, src: str, dst: str):
        """Copie récursive ; dst n'existe pas, ou est un dossier à fusionner."""
        self.job.check_cancel()
        try:
            s = os.lstat(src)
        except OSError as e:
            self.errors.append((src, e.strerror or str(e)))
            return
        try:
            if st.S_ISLNK(s.st_mode):
                try:
                    os.symlink(os.readlink(src), dst)
                except FileExistsError:
                    raise _Taken(dst) from None
                except OSError as e:
                    if self.windows:
                        raise OSError(e.errno, tr("les liens symboliques ne peuvent pas être copiés sur ce support"))
                    raise
                self.done_files += 1
                self.copied.add(src)
                self._created(dst)
                self._made(src, dst)
            elif st.S_ISDIR(s.st_mode):
                existed = os.path.isdir(dst) and not os.path.islink(dst)
                os.makedirs(dst, exist_ok=True)
                self.copied.add(src)
                self._created(dst)
                if not existed:
                    self._made(src, dst)
                with os.scandir(src) as it:
                    children = [e.name for e in it]
                for name in children:
                    child_dst = os.path.join(dst, fsinfo.safe_name(name) if self.windows else name)
                    self.place(os.path.join(src, name), child_dst, nested=True)
                try:
                    shutil.copystat(src, dst)
                except OSError:
                    pass
            elif st.S_ISREG(s.st_mode):
                self.copy_file(src, dst, s)
            else:
                self.errors.append((src, tr("Fichier spécial ignoré")))
        except Cancelled:
            raise
        except OSError as e:
            self.errors.append((src, e.strerror or str(e)))

    def place(self, src: str, dst: str, nested: bool = False) -> Optional[str]:
        """Copie src vers dst en réglant un éventuel conflit ; renvoie le chemin final. Si le nom
        est pris pendant la copie (création exclusive refusée), le conflit est posé à ce moment-là."""
        for _attempt in range(4):
            try:
                return self._place(src, dst, nested)
            except _Taken:
                continue
        # toujours refusé alors que le nom paraît libre (clé FAT : même nom, autre casse) : signalé,
        # rien n'est écrasé ; pour un déplacement, la source reste
        self.errors.append((src, os.strerror(errno.EEXIST)))
        return None

    def _place(self, src: str, dst: str, nested: bool) -> Optional[str]:
        src_is_dir = os.path.isdir(src) and not os.path.islink(src)
        exists = os.path.lexists(dst)
        if self.windows:
            folder = os.path.dirname(dst)
            actual = self._existing(dst)
            if actual is not None and dst.casefold() in self.created_here:
                # « Photo.jpg » et « photo.jpg » (ou « a:b » et « a_b ») dans la même copie vers une
                # clé FAT : c'est le même nom là-bas ; le second est renommé au lieu d'écraser le premier
                dst = os.path.join(folder, self._free_name(folder, os.path.basename(dst), src_is_dir))
                exists = False
            elif actual is not None:
                dst = os.path.join(folder, actual)      # conflit avec l'élément tel qu'il est nommé
                exists = True
        if exists and _real_location(src) == _real_location(dst):
            return None          # c'est le même élément (vu à travers un lien) : rien à faire
        if exists:
            dst_is_dir = os.path.isdir(dst) and not os.path.islink(dst)
            choice = self.decide(Conflict(src, dst, src_is_dir, dst_is_dir, self.move))
            if choice == "skip":
                if not nested:
                    return None
                _, files = measure(None, [src]) if src_is_dir else (0, 1)
                self.done_files += files
                return None
            if choice == "rename":
                dst = os.path.join(os.path.dirname(dst),
                                   self._free_name(os.path.dirname(dst), os.path.basename(dst), src_is_dir))
            elif choice == "merge" and src_is_dir and dst_is_dir:
                pass
            else:   # replace
                return self._replace(src, dst)
        self.copy_tree(src, dst)
        return dst

    def _replace(self, src: str, dst: str) -> Optional[str]:
        """Remplacer sans risque : copie complète sous un nom temporaire, puis échange. Si la copie
        échoue ou est annulée, l'élément existant reste intact."""
        tmp = _tmp_name(os.path.dirname(dst), "copie")
        before = len(self.errors)
        roots_before = len(self.roots)
        self.sync = True
        try:
            self.copy_tree(src, tmp)
        except BaseException:
            if os.path.lexists(tmp):
                _remove(tmp)
            raise
        finally:
            self.sync = False
        if len(self.errors) != before or not os.path.lexists(tmp):
            if os.path.lexists(tmp):
                _remove(tmp)
            self._forget(src, roots_before)
            return None
        try:
            _swap_into(tmp, dst)
            _fsync_dir(os.path.dirname(dst))
            del self.roots[roots_before:]          # ce qui était sous le nom temporaire
            self._made(src, dst)
        except OSError as e:
            self.errors.append((dst, e.strerror or str(e)))
            if os.path.lexists(tmp):
                _remove(tmp)
            self._forget(src, roots_before)
            return None
        return dst

    def _forget(self, src: str, roots_before: int):
        """Remplacement abandonné : la copie temporaire est jetée, donc rien de src n'a été copié.
        Sans ça, un déplacement supprimerait ensuite de la source des fichiers qui n'existent plus
        nulle part (_prune_moved ne retire que ce qui est dans « copied »)."""
        prefix = src.rstrip("/") + "/"
        self.copied = {p for p in self.copied if p != src and not p.startswith(prefix)}
        del self.roots[roots_before:]


def _tmp_name(folder: str, tag: str) -> str:
    """Nom temporaire court et unique dans folder. Il reprenait le nom du fichier : avec un nom
    de 250 octets, il dépassait la limite de 255 (ENAMETOOLONG)."""
    while True:
        p = os.path.join(folder, f".odysseus-{tag}-{os.getpid()}-{secrets.token_hex(4)}")
        if not os.path.lexists(p):
            return p


def _fsync_dir(folder: str):
    """L'échange (renommage) lui-même écrit sur le disque."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_renameat2 = None


def _exchange(a: str, b: str) -> bool:
    """Échange atomique de deux chemins (renameat2 RENAME_EXCHANGE, dossiers compris). False si le
    système de fichiers ne sait pas faire (FAT, certains FUSE, NFS…) : rien n'a alors bougé."""
    return _rename2(a, b, _RENAME_EXCHANGE)


def _rename_new(src: str, dst: str):
    """os.rename qui n'écrase jamais : FileExistsError si dst existe, même apparu à l'instant
    (renameat2 RENAME_NOREPLACE ; là où ce n'est pas géré, vérification juste avant)."""
    if _rename2(src, dst, _RENAME_NOREPLACE):
        return
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst)


_syncfs = None


def flush_to_disk(path: str):
    """Force sur le disque tout ce qui a été écrit sur le système de fichiers de path (syncfs), à
    appeler AVANT de supprimer un original (déplacement vers un autre disque, « supprimer après »).
    Sans ça, une coupure de courant pouvait laisser la suppression écrite et la copie encore dans
    le cache du noyau : ni l'original ni la copie. Un seul appel par élément, pas par fichier."""
    global _syncfs
    if _syncfs is None:
        try:
            _syncfs = ctypes.CDLL(None, use_errno=True).syncfs
            _syncfs.argtypes = [ctypes.c_int]
        except (OSError, AttributeError):
            _syncfs = False
    folder = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        os.sync()
        return
    try:
        if not _syncfs or _syncfs(fd) != 0:
            os.sync()
    finally:
        os.close(fd)


def place_new(tmp: str, dst: str) -> str:
    """Pose tmp (fichier ou dossier terminé) sous le nom dst sans jamais rien écraser : si le nom
    a été pris entre-temps (autre opération lancée en même temps), « nom (2) ». Renvoie le chemin
    final. Pour les sorties censées être nouvelles (archives, PDF, extraction…)."""
    folder = os.path.dirname(dst) or "."
    for _attempt in range(20):
        try:
            _rename_new(tmp, dst)
            return dst
        except FileExistsError:
            dst = os.path.join(folder, unique_name(folder, os.path.basename(dst), os.path.isdir(tmp)))
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)


def move_new(src: str, dst: str):
    """Déplace src vers dst (autre disque compris) sans jamais écraser : FileExistsError sinon."""
    try:
        _rename_new(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst) from None
        shutil.move(src, dst)


def _rename2(a: str, b: str, flags: int) -> bool:
    global _renameat2
    if _renameat2 is None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            _renameat2 = libc.renameat2
            _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        except (OSError, AttributeError):
            _renameat2 = False
    if not _renameat2:
        return False
    at_fdcwd = -100
    if _renameat2(at_fdcwd, os.fsencode(a), at_fdcwd, os.fsencode(b), flags) == 0:
        return True
    err = ctypes.get_errno()
    if err in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
        return False
    raise OSError(err, os.strerror(err), b)


def _swap_into(new: str, dst: str):
    """Met new à la place de dst. Fichier sur fichier : os.replace, atomique. Dossier : échange
    atomique si possible (le nom visé existe à tout instant), puis l'ancien, désormais sous le nom
    temporaire, est supprimé. Sinon l'ancien est d'abord mis de côté puis supprimé seulement une
    fois le nouveau en place (remis s'il y a échec)."""
    old_is_dir = os.path.isdir(dst) and not os.path.islink(dst)
    new_is_dir = os.path.isdir(new) and not os.path.islink(new)
    if not old_is_dir and not new_is_dir:
        os.replace(new, dst)
        return
    if _exchange(new, dst):
        _remove(new)
        return
    aside = _tmp_name(os.path.dirname(dst), "ancien")
    os.rename(dst, aside)
    try:
        os.rename(new, dst)
    except OSError:
        os.rename(aside, dst)
        raise
    _remove(aside)


def _prune_moved(src: str, copied: set):
    """Après un déplacement par copie : retire de la source ce qui a été copié, et seulement ça.
    Les éléments ignorés (conflit « ignorer ») ou en erreur restent à la source avec leurs dossiers."""
    if os.path.islink(src) or not os.path.isdir(src):
        if src in copied:
            os.remove(src)
        return
    for root, dirs, files in os.walk(src, topdown=False):
        for name in files:
            p = os.path.join(root, name)
            if p in copied:
                os.remove(p)
        for name in dirs:
            p = os.path.join(root, name)
            if os.path.islink(p):
                if p in copied:
                    os.remove(p)
            elif p in copied:
                try:
                    os.rmdir(p)          # seulement s'il est vide : rien d'ignoré dedans
                except OSError:
                    pass
    if src in copied:
        try:
            os.rmdir(src)
        except OSError:
            pass


def _remove(path: str):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _real_location(path: str) -> str:
    """Chemin réel du dossier parent + nom (le dernier élément n'est pas suivi : un lien reste un
    lien). Deux chemins égaux ici désignent le même élément, même vu à travers un lien symbolique."""
    path = os.path.abspath(path).rstrip("/") or "/"
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _is_inside(child: str, parent: str) -> bool:
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _move_same_disk(src: str, dst: str, dest_dir: str, name: str, copier: "_Copier", res: Result,
                    direct: list, to_copy: list):
    """Déplacement par simple renommage. Le renommage n'écrase jamais (_rename_new) : si le nom est
    pris entre la vérification et le renommage (autre déplacement lancé en même temps), le conflit
    est posé à nouveau au lieu de remplacer en silence l'élément arrivé entre-temps."""
    for _attempt in range(4):
        if os.path.lexists(dst) and _real_location(dst) == _real_location(src):
            return
        if os.path.lexists(dst):
            src_is_dir = os.path.isdir(src) and not os.path.islink(src)
            dst_is_dir = os.path.isdir(dst) and not os.path.islink(dst)
            choice = copier.decide(Conflict(src, dst, src_is_dir, dst_is_dir, True))
            if choice == "skip":
                return
            if choice == "rename":
                dst = os.path.join(dest_dir, unique_name(dest_dir, name, src_is_dir))
            elif choice == "merge" and src_is_dir and dst_is_dir:
                to_copy.append((src, dst, True))
                return
            else:
                try:
                    _swap_into(src, dst)
                    res.moved.append((src, dst))
                    res.created.append(dst)
                    direct.append((src, dst))
                except OSError as e:
                    res.errors.append((dst, e.strerror or str(e)))
                return
        try:
            _rename_new(src, dst)
        except FileExistsError:
            continue                          # pris à l'instant : on repose la question
        except OSError as e:
            res.errors.append((src, e.strerror or str(e)))
            return
        res.moved.append((src, dst))
        res.created.append(dst)
        direct.append((src, dst))
        return
    res.errors.append((src, os.strerror(errno.EEXIST)))     # nom refusé sans fin (voir _Copier.place)


def transfer(job: Job, sources: list[str], dest_dir: str, ask: Asker, move: bool = False) -> Result:
    """Copie ou déplace des éléments dans dest_dir."""
    res = Result("move" if move else "copy")
    if not os.path.isdir(dest_dir):
        raise ToolError(tr("Le dossier de destination n'existe pas : {0}").format(dest_dir))
    for s in sources:
        # un lien symbolique est copié / déplacé comme lien : le poser dans le dossier qu'il vise est permis
        if os.path.isdir(s) and not os.path.islink(s) and _is_inside(dest_dir, s):
            raise ToolError(tr("Impossible de copier « {0} » dans lui-même").format(os.path.basename(s)))
    try:
        dest_dev = os.stat(dest_dir).st_dev
    except OSError as e:
        raise ToolError(e.strerror or str(e))

    # Déplacements sur le même disque : simples renommages, sans mesure préalable.
    to_copy = []
    direct = []                    # déplacements par simple renommage : (ancien, nouveau)
    copier = _Copier(job, ask, 0, 0, move)
    kind = fsinfo.fs_type(os.path.realpath(dest_dir))     # clé atteinte par un lien symbolique
    copier.windows, copier.fat = kind in fsinfo.WINDOWS_LIKE, kind in fsinfo.FAT
    for src in sources:
        job.check_cancel()
        name = os.path.basename(src.rstrip("/"))
        if copier.windows:
            name = fsinfo.safe_name(name)
        dst = os.path.join(dest_dir, name)
        if move:
            if os.path.dirname(_real_location(src)) == os.path.realpath(dest_dir):
                continue                       # déjà là (éventuellement vu à travers un lien)
            try:
                same = os.lstat(src).st_dev == dest_dev
            except OSError as e:
                res.errors.append((src, e.strerror or str(e)))
                continue
            if same:
                _move_same_disk(src, dst, dest_dir, name, copier, res, direct, to_copy)
                continue
        else:
            if _real_location(dst) == _real_location(src):
                # coller au même endroit : « nom (copie) », comme Nautilus
                is_dir = os.path.isdir(src) and not os.path.islink(src)
                dst = os.path.join(dest_dir, unique_name(dest_dir, name, is_dir, copy_word=True))
        to_copy.append((src, dst, False))

    if to_copy:
        total, files = measure(job, [s for s, _d, _m in to_copy])
        copier.total, copier.files = max(total, 1), files
        copier.started = time.monotonic()
        # déplacement : une source n'est retirée qu'une fois sa copie forcée sur le disque
        # (flush_to_disk). Par lots de 64 éléments : un syncfs par élément coûtait cher sur une
        # clé ou un disque lent quand on déplace des centaines de fichiers choisis un à un.
        pending: list = []

        def settle():
            if not pending:
                return
            flush_to_disk(pending[-1][1])
            for s_, final_ in pending:
                try:
                    _prune_moved(s_, copier.copied)
                    if not os.path.lexists(s_):
                        res.moved.append((s_, final_))
                except OSError as e:
                    copier.errors.append((s_, e.strerror or str(e)))
            pending.clear()
        try:
            for src, dst, merge in to_copy:
                job.check_cancel()
                if merge:
                    copier.copy_tree(src, dst)
                    final = dst
                else:
                    final = copier.place(src, dst)
                if final is None:
                    continue
                res.created.append(final)
                if move:
                    pending.append((src, final))
                    if len(pending) >= 64:
                        settle()
        finally:
            settle()          # annulation, erreur : les éléments entièrement copiés sont bien déplacés
        copier.tick(force=True)
    res.new_items = [d for _s, d in direct] + [d for _s, d in copier.roots]
    if move:
        res.new_moves = list(direct) + [(s_, d) for s_, d in copier.roots if not os.path.lexists(s_)]
    res.errors.extend(copier.errors)
    _raise_partial(res)
    return res


def _raise_partial(res: Result):
    if res.errors and not res.created and not res.trashed:
        path, msg = res.errors[0]
        raise ToolError(f"{os.path.basename(path)} : {msg}",
                        "\n".join(f"{p} : {m}" for p, m in res.errors))


# ---------------------------------------------------------------------------
# Corbeille / suppression
# ---------------------------------------------------------------------------

def trash(job: Job, paths: list[str]) -> Result:
    res = Result("trash")
    for i, p in enumerate(paths):
        job.check_cancel()
        job.progress(i / max(len(paths), 1), tr("Mise à la corbeille de « {0} »").format(os.path.basename(p)))
        try:
            Gio.File.new_for_path(p).trash(None)
            res.trashed.append(p)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                res.no_trash.append(p)      # tmpfs, montage système… : l'interface propose de supprimer
            else:
                res.errors.append((p, e.message))
    if not res.no_trash:
        _raise_partial(res)
    return res


def delete(job: Job, paths: list[str]) -> Result:
    res = Result("delete")
    total, files = measure(job, paths)
    done = 0
    for p in paths:
        job.check_cancel()
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                for root, dirs, fnames in os.walk(p, topdown=False):
                    for f in fnames:
                        job.check_cancel()
                        os.remove(os.path.join(root, f))
                        done += 1
                        if done % 200 == 0:
                            job.progress(done / max(files, 1), tr("Suppression… {0} sur {1}").format(done, files))
                    for d in dirs:
                        full = os.path.join(root, d)
                        if os.path.islink(full):
                            os.remove(full)
                        else:
                            os.rmdir(full)
                os.rmdir(p)
            else:
                os.remove(p)
                done += 1
            res.created.append(p)
        except OSError as e:
            res.errors.append((p, e.strerror or str(e)))
    if res.errors and not res.created:
        path, msg = res.errors[0]
        raise ToolError(f"{os.path.basename(path)} : {msg}")
    return res


def trash_items(job: Job) -> list[Gio.File]:
    out = []
    try:
        en = Gio.File.new_for_uri("trash:///").enumerate_children(
            "standard::name,trash::orig-path,trash::deletion-date", Gio.FileQueryInfoFlags.NONE, None)
        root = Gio.File.new_for_uri("trash:///")
        while True:
            info = en.next_file(None)
            if info is None:
                break
            out.append((info, root.get_child(info.get_name())))
    except GLib.Error:
        pass
    return out


def empty_trash(job: Job) -> int:
    n = 0

    def rm(f: Gio.File):
        try:
            info = f.query_info("standard::type", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
            if info.get_file_type() == Gio.FileType.DIRECTORY:
                en = f.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
                for ci in en:
                    rm(f.get_child(ci.get_name()))
            f.delete(None)
        except GLib.Error:
            pass
    items = trash_items(job)
    for i, (_info, f) in enumerate(items):
        job.check_cancel()
        job.progress(i / max(len(items), 1), tr("Vidage de la corbeille…"))
        rm(f)
        n += 1
    return n


def restore_from_trash(job: Job, uris: list[str]) -> Result:
    """Remet des éléments de la corbeille à leur emplacement d'origine."""
    res = Result("restore")
    for uri in uris:
        job.check_cancel()
        f = Gio.File.new_for_uri(uri)
        try:
            info = f.query_info("trash::orig-path", Gio.FileQueryInfoFlags.NONE, None)
            orig = info.get_attribute_byte_string("trash::orig-path")
            if not orig:
                raise GLib.Error(tr("Emplacement d'origine inconnu"))
            target = orig
            if os.path.lexists(target):
                target = os.path.join(os.path.dirname(orig),
                                      unique_name(os.path.dirname(orig), os.path.basename(orig)))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            f.move(Gio.File.new_for_path(target), Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
            res.created.append(target)
        except (GLib.Error, OSError) as e:
            res.errors.append((uri, getattr(e, "message", None) or str(e)))
    _raise_partial(res)
    return res


def restore_trashed_paths(job: Job, originals: list[str]) -> Result:
    """Annulation d'une mise à la corbeille : retrouve les éléments par leur chemin d'origine."""
    wanted = set(originals)
    best: dict = {}
    for info, f in trash_items(job):
        orig = info.get_attribute_byte_string("trash::orig-path")
        if orig in wanted:
            date = info.get_attribute_string("trash::deletion-date") or ""
            if orig not in best or date > best[orig][0]:
                best[orig] = (date, f.get_uri())
    return restore_from_trash(job, [u for _d, u in best.values()])


# ---------------------------------------------------------------------------
# Création / renommage
# ---------------------------------------------------------------------------

def make_folder(folder: str, name: str) -> str:
    err = valid_name(name)
    if err:
        raise ToolError(err)
    path = os.path.join(folder, name)
    if os.path.lexists(path):
        raise ToolError(tr("« {0} » existe déjà").format(name))
    os.mkdir(path)
    return path


def make_file(folder: str, name: str, template: str = "") -> str:
    err = valid_name(name)
    if err:
        raise ToolError(err)
    path = os.path.join(folder, name)
    if os.path.lexists(path):
        raise ToolError(tr("« {0} » existe déjà").format(name))
    if template:
        shutil.copyfile(template, path)
    else:
        with open(path, "x"):
            pass
    return path


def rename(path: str, new_name: str) -> str:
    err = valid_name(new_name)
    if err:
        raise ToolError(err)
    folder, old = os.path.split(path)
    target = os.path.join(folder, new_name)
    if new_name == old:
        return target
    # on lit le dossier plutôt que de se fier à lexists() : selon le système de fichiers, la recherche
    # d'un nom tient compte de la casse ou pas (vfat utf8 : oui, mais création refusée quand même)
    names = os.listdir(folder or ".")
    folded = [n for n in names if n.casefold() == new_name.casefold() and n != old]
    if new_name in names or (folded and (fsinfo.is_windows_like(os.path.realpath(folder or ".")) or os.path.lexists(target))):
        raise ToolError(tr("« {0} » existe déjà").format(new_name))
    if new_name.casefold() == old.casefold():
        # seule la casse change : en deux temps (sur FAT, NTFS… c'est le même nom pour le système)
        tmp = _tmp_name(folder, "casse")
        os.rename(path, tmp)
        os.rename(tmp, target)
        return target
    if os.path.lexists(target) and os.path.realpath(target) != os.path.realpath(path):
        raise ToolError(tr("« {0} » existe déjà").format(new_name))
    os.rename(path, target)
    return target


def make_link(job: Job, sources: list[str], dest_dir: str) -> Result:
    res = Result("link")
    for s in sources:
        name = tr("Lien vers {0}").format(os.path.basename(s))
        dst = os.path.join(dest_dir, unique_name(dest_dir, name))
        try:
            os.symlink(s, dst)
            res.created.append(dst)
        except OSError as e:
            res.errors.append((s, e.strerror or str(e)))
    _raise_partial(res)
    return res


def describe_result(res: Result) -> str:
    n = len(res.created) or len(res.trashed)
    if res.kind == "trash":
        return ntr("{0} élément mis à la corbeille", "{0} éléments mis à la corbeille", n).format(n)
    if res.kind == "delete":
        return ntr("{0} élément supprimé", "{0} éléments supprimés", n).format(n)
    if res.kind == "move":
        return ntr("{0} élément déplacé", "{0} éléments déplacés", n).format(n)
    if res.kind == "restore":
        return ntr("{0} élément restauré", "{0} éléments restaurés", n).format(n)
    if res.kind == "link":
        return ntr("{0} lien créé", "{0} liens créés", n).format(n)
    return ntr("{0} élément copié", "{0} éléments copiés", n).format(n)
