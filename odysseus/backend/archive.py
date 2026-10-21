"""Opérations sur les archives : lister, créer, extraire, tester, modifier, convertir."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import formats
from ..i18n import tr
from .engine import (Job, PasswordRequired, ToolError, raise_for_7z, relay,
                     normal_mode, sevenzip_path, sfx_module, quote_cmd)

SEVENZ_PROGRESS = ["-bsp1", "-bso0", "-bse1", "-y"]


def _7z() -> str:
    exe = sevenzip_path()
    if not exe:
        raise ToolError(tr("7-Zip est introuvable (installez le paquet 7zip ou p7zip-full)"))
    return exe


def _pw_args(password: Optional[str]) -> list[str]:
    # « -p » vide empêche 7-Zip de demander le mot de passe sur l'entrée standard
    # (lecture, test, extraction).
    return ["-p" + (password or "")]


def _pw_update(password: Optional[str]) -> list[str]:
    # En modification, « -p » vide provoquerait une invite : on ne le passe que s'il est connu.
    return ["-p" + password] if password else []


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    path: str
    is_dir: bool = False
    size: Optional[int] = None
    packed: Optional[int] = None
    modified: Optional[datetime] = None
    encrypted: bool = False
    crc: str = ""
    method: str = ""
    attributes: str = ""
    implicit: bool = False     # dossier déduit d'un chemin, absent de l'archive

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]

    @property
    def parent(self) -> str:
        p = self.path.rstrip("/")
        return p.rsplit("/", 1)[0] if "/" in p else ""


@dataclass
class ArchiveListing:
    path: str
    entries: list[Entry]
    info: dict = field(default_factory=dict)
    kind: str = "7z"

    def __post_init__(self):
        self._children: dict[str, list[Entry]] = {}
        self._by_path: dict[str, Entry] = {}
        for e in self.entries:
            e.path = e.path.replace("\\", "/").strip("/")
            self._by_path[e.path] = e
        # dossiers implicites
        for e in list(self.entries):
            parent = e.parent
            while parent and parent not in self._by_path:
                d = Entry(parent, is_dir=True, implicit=True)
                self._by_path[parent] = d
                self.entries.append(d)
                parent = d.parent
        for e in self.entries:
            if e.path:
                self._children.setdefault(e.parent, []).append(e)

    def children(self, folder: str) -> list[Entry]:
        return list(self._children.get(folder.strip("/"), []))

    def get(self, path: str) -> Optional[Entry]:
        return self._by_path.get(path.strip("/"))

    def walk(self, folder: str):
        for e in self.children(folder):
            yield e
            if e.is_dir:
                yield from self.walk(e.path)

    def top_level(self) -> list[Entry]:
        return self.children("")

    @property
    def total_size(self) -> int:
        return sum(e.size or 0 for e in self.entries if not e.is_dir)

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if not e.is_dir)

    @property
    def dir_count(self) -> int:
        return sum(1 for e in self.entries if e.is_dir)

    @property
    def encrypted(self) -> bool:
        return any(e.encrypted for e in self.entries) or \
            self.info.get("Encrypted", "-") == "+"


def _parse_int(v: str) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_date(v: str) -> Optional[datetime]:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v[:26] if "." in v else v, fmt)
        except ValueError:
            continue
    return None


def _parse_block(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        if " = " in line:
            k, v = line.split(" = ", 1)
            d[k.strip()] = v.strip()
        elif line.endswith(" ="):
            d[line[:-2].strip()] = ""
    return d


def parse_slt(text: str, default_name: str = "contenu") -> tuple[dict, list[Entry]]:
    header, sep, body = text.partition("\n----------\n")
    if not sep:
        raise_for_7z(text, 2)
    info = {}
    if "\n--\n" in header:
        info = _parse_block(header.split("\n--\n", 1)[1])
    entries = []
    for block in body.split("\n\n"):
        d = _parse_block(block)
        if "Path" not in d:
            # flux xz/bz2 : 7-Zip ne donne pas de nom à l'unique élément
            if "Size" not in d and "Packed Size" not in d:
                continue
            d["Path"] = default_name
        attrs = d.get("Attributes", "") or d.get("Mode", "")
        is_dir = d.get("Folder") == "+" or attrs.startswith("D") or attrs.startswith("d")
        entries.append(Entry(
            path=d["Path"],
            is_dir=is_dir,
            size=_parse_int(d.get("Size")),
            packed=_parse_int(d.get("Packed Size")),
            modified=_parse_date(d.get("Modified", "")),
            encrypted=d.get("Encrypted") == "+",
            crc=d.get("CRC", ""),
            method=d.get("Method", ""),
            attributes=attrs,
        ))
    return info, entries


# ---------------------------------------------------------------------------
# Pipelines (décompression de flux)
# ---------------------------------------------------------------------------

def _decompress_cmd(stream: str) -> list[str]:
    fast = formats.parallel_tool(stream)
    if fast:
        return [fast[0]] + fast[2]
    if stream == "bz2" and sevenzip_path():
        # sans lbzip2 : le décodeur bzip2 de 7-Zip est multicœur (mesuré 3× plus rapide que bzip2)
        return [sevenzip_path(), "x", "-tbzip2", "-si", "-so", "-bd"]
    prog, _, dargs = formats.STREAM_TOOLS[stream]
    exe = shutil.which(prog)
    if not exe:
        raise ToolError(tr('Le programme « {prog} » est nécessaire pour ce format').format(prog=prog))
    return [exe] + dargs


def _compress_cmd(stream: str, level: Optional[int]) -> list[str]:
    fast = formats.parallel_tool(stream)
    if fast:
        exe, cargs = fast[0], fast[1]
    else:
        prog, cargs, _ = formats.STREAM_TOOLS[stream]
        exe = shutil.which(prog)
        if not exe:
            raise ToolError(tr('Le programme « {prog} » est nécessaire pour ce format').format(prog=prog))
    argv = [exe] + list(cargs)
    if level is not None:
        lo, hi = formats.STREAM_LEVELS.get(stream, (1, 9))
        level = max(lo, min(hi, level))
        if stream == "br":
            argv += ["-q", str(level)]
        elif stream == "zst" and level > 19:
            argv += ["--ultra", f"-{level}"]
        else:
            argv.append(f"-{level}")
    return argv


def _feed(job: Job, path: str, proc_stdin, weight, label, errors: list):
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as src:
            relay(job, src, proc_stdin, total, weight, label)
    except BrokenPipeError:
        pass
    except Exception as e:  # remonté au thread principal
        errors.append(e)
    finally:
        try:
            proc_stdin.close()
        except Exception:
            pass


def _run_decompress_into(job: Job, archive: str, stream: str, consumer_argv: list[str],
                         label: str, capture=True, consumer_stdout=None, weight=(0.0, 1.0)) -> str:
    """fichier -> décompresseur -> consommateur (7z -si ou fichier)."""
    dec = job.popen(_decompress_cmd(stream), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
    errors: list = []
    feeder = threading.Thread(target=_feed, args=(job, archive, dec.stdin, weight, label, errors),
                              daemon=True)
    feeder.start()
    if consumer_argv:
        out = subprocess.PIPE if capture else (consumer_stdout or subprocess.DEVNULL)
        cons = job.popen(consumer_argv, stdin=dec.stdout, stdout=out, stderr=subprocess.STDOUT)
        dec.stdout.close()
        text = cons.communicate()[0] if capture else b""
        code = cons.returncode
    else:
        shutil.copyfileobj(dec.stdout, consumer_stdout, 1 << 20)
        text, code = b"", 0
    dec_err = dec.stderr.read()
    dec.wait()
    feeder.join()
    job.check_cancel()
    if errors:
        raise errors[0]
    if dec.returncode != 0:
        raise ToolError(tr("Décompression impossible : ") + dec_err.decode("utf-8", "replace").strip()[-300:],
                        dec_err.decode("utf-8", "replace"), dec.returncode)
    text = (text or b"").decode("utf-8", "replace")
    if code not in (0, 1):
        raise_for_7z(text, code)
    return text


# ---------------------------------------------------------------------------
# Lister
# ---------------------------------------------------------------------------

def list_archive(path: str, password: Optional[str] = None, job: Optional[Job] = None) -> ArchiveListing:
    job = job or Job(tr("Lecture"), lambda j: None)
    det = formats.detect(path)
    if det.kind == "tar-stream":
        text = _run_decompress_into(job, path, det.stream, [_7z(), "l", "-slt", "-si", "-ttar"],
                                    "Lecture ")
        info, entries = parse_slt(text)
        info["Type"] = f"tar.{det.stream}"
        info["Physical Size"] = str(os.path.getsize(path))
        return ArchiveListing(path, entries, info, "tar-stream")
    if det.kind == "stream":
        size = os.path.getsize(path)
        e = Entry(det.inner_name or "contenu", size=None, packed=size,
                  modified=datetime.fromtimestamp(os.path.getmtime(path)))
        return ArchiveListing(path, [e], {"Type": det.stream, "Physical Size": str(size)}, "stream")
    argv = [_7z(), "l", "-slt", "-sccUTF-8"] + _pw_args(password) + ["--", path]
    job.commands.append(quote_cmd(argv))
    proc = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
    text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    if proc.returncode not in (0, 1):
        raise_for_7z(text, proc.returncode)
    info, entries = parse_slt(text, det.base_name or "contenu")
    if "Errors" in text and "Can not open encrypted" in text:
        raise PasswordRequired(tr("Mot de passe requis"), text)
    return ArchiveListing(path, entries, info, "7z")


def needs_password(path: str) -> bool:
    """Vrai si l'archive (7z/zip…) a des noms chiffrés ou un contenu chiffré."""
    try:
        listing = list_archive(path)
    except PasswordRequired:
        return True
    except ToolError:
        return False
    return listing.encrypted


# ---------------------------------------------------------------------------
# Créer
# ---------------------------------------------------------------------------

@dataclass
class CreateOptions:
    fmt: str = "7z"
    level: int = 5
    method: str = ""
    solid: bool = True
    threads: int = 0                 # 0 = automatique
    password: str = ""
    encrypt_names: bool = False
    zip_aes: bool = True
    volume_size: str = ""            # ex. « 100m », « 4480m »
    sfx: bool = False
    delete_after: bool = False
    excludes: list = field(default_factory=list)
    update_mode: str = "new"         # new (« nom (2) » si l'archive existe), add, update, sync, fresh
    dictionary: str = ""             # ex. « 64m »
    extra_args: str = ""             # options 7-Zip libres (utilisateurs avancés)


def total_size(paths: list[str]) -> int:
    total = 0
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    try:
                        total += os.lstat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
        else:
            try:
                total += os.lstat(p).st_size
            except OSError:
                pass
    return total


def default_output(sources: list[str], fmt_key: str, sfx: bool = False) -> str:
    fmt = formats.FORMATS_BY_KEY[fmt_key]
    first = os.path.abspath(sources[0])
    parent = os.path.dirname(first)
    if len(sources) == 1:
        base = os.path.basename(first.rstrip("/"))
        if os.path.isfile(first) and fmt.multi:
            base = os.path.splitext(base)[0] or base
    else:
        base = os.path.basename(parent) or "archive"
    if sfx:
        return os.path.join(parent, base + ".run")
    return os.path.join(parent, f"{base}.{fmt.ext}")


def build_7z_create(sources: list[str], out: str, o: CreateOptions) -> list[str]:
    fmt = formats.FORMATS_BY_KEY[o.fmt]
    cmd = {"new": "a", "add": "a", "update": "u", "sync": "u", "fresh": "u"}.get(o.update_mode, "a")
    argv = [_7z(), cmd, "-t" + fmt.sevenz_type]
    if o.update_mode == "sync":
        argv.append("-uq0")      # supprime de l'archive ce qui n'existe plus sur disque
    elif o.update_mode == "fresh":
        argv.append("-ur0")      # ne met à jour que les fichiers déjà présents
    if fmt.key in ("7z", "zip"):
        argv.append(f"-mx={o.level}")
    if o.method and fmt.methods:
        if fmt.key == "7z":
            argv.append(f"-m0={o.method}")
        elif fmt.key == "zip":
            argv.append(f"-mm={o.method}")
    if o.dictionary and fmt.key == "7z" and (o.method or "LZMA2").upper().startswith("LZMA"):
        argv.append(f"-md={o.dictionary}")
    if fmt.key == "7z":
        argv.append("-ms=on" if o.solid else "-ms=off")
    if fmt.key in ("7z", "zip"):
        argv.append(f"-mmt={o.threads}" if o.threads else "-mmt=on")
    if o.password and fmt.encrypt:
        argv.append("-p" + o.password)
        if fmt.key == "7z" and o.encrypt_names:
            argv.append("-mhe=on")
        if fmt.key == "zip":
            argv.append("-mem=AES256" if o.zip_aes else "-mem=ZipCrypto")
    if o.volume_size and fmt.volumes:
        argv.append("-v" + o.volume_size)
    if o.sfx and fmt.sfx:
        mod = sfx_module()
        if not mod:
            raise ToolError(tr("Module auto-extractible (7zCon.sfx) introuvable"))
        argv.append("-sfx" + mod)
    # « supprimer après » : jamais -sdel. 7-Zip supprimait les sources dès l'archive écrite, avant
    # qu'elle soit posée sous son vrai nom ; Odysseus tué entre les deux : sources perdues, archive
    # restée sous un nom caché. Les sources sont retirées par create(), une fois l'archive en place.
    for pat in o.excludes:
        pat = pat.strip()
        if pat:
            argv.append("-xr!" + pat)
    if o.extra_args.strip():
        import shlex
        argv += shlex.split(o.extra_args)
    argv += ["-sccUTF-8", "-scsUTF-8"] + SEVENZ_PROGRESS + ["--", out]
    argv += [os.path.abspath(s).rstrip("/") for s in sources]
    return argv


def build_tar_create(sources: list[str], o: CreateOptions) -> tuple[list[str], list[str]]:
    stream = o.fmt.split(".", 1)[1]
    tar = [shutil.which("tar") or "tar", "-cf", "-"]
    for pat in o.excludes:
        if pat.strip():
            tar.append("--exclude=" + pat.strip())
    for s in sources:
        s = os.path.abspath(s).rstrip("/")
        tar += ["-C", os.path.dirname(s) or "/", os.path.basename(s)]
    return tar, _compress_cmd(stream, o.level)


def describe_create(sources: list[str], out: str, o: CreateOptions) -> str:
    """Commande équivalente, affichée dans l'interface (comme PeaZip)."""
    fmt = formats.FORMATS_BY_KEY[o.fmt]
    try:
        if fmt.engine == "7z":
            return quote_cmd(build_7z_create(sources, out, o))
        if fmt.engine == "tar":
            tar, comp = build_tar_create(sources, o)
            return f"{quote_cmd(tar)} | {quote_cmd(comp)} > {quote_cmd([out])}"
        comp = _compress_cmd(fmt.key, o.level)
        return f"{quote_cmd(comp)} < {quote_cmd([sources[0]])} > {quote_cmd([out])}"
    except ToolError as e:
        return str(e)


def _remove_sources(sources: list[str], keep: frozenset = frozenset()):
    """Supprime les sources après compression, sauf keep (fichiers absents de l'archive : illisibles)
    et les dossiers qui les contiennent."""
    for s in sources:
        s = os.path.abspath(s).rstrip("/")
        if os.path.isdir(s) and not os.path.islink(s):
            if not keep:
                shutil.rmtree(s, ignore_errors=True)
                continue
            for root, dirs, files in os.walk(s, topdown=False):
                for n in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                    p = os.path.join(root, n)
                    if p not in keep:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                for n in dirs:
                    try:
                        os.rmdir(os.path.join(root, n))      # vide seulement
                    except OSError:
                        pass
            try:
                os.rmdir(s)
            except OSError:
                pass
        elif os.path.lexists(s) and s not in keep:
            os.remove(s)


def _skipped_paths(sources: list[str], skipped: list) -> Optional[frozenset]:
    """Chemins absolus des fichiers sautés par tar (noms relatifs au parent de chaque source).
    None si l'un d'eux ne se retrouve pas : on ne supprime alors rien."""
    out = set()
    parents = {os.path.dirname(os.path.abspath(s).rstrip("/")) for s in sources}
    for name, _why in skipped:
        cands = [name] if os.path.isabs(name) else [os.path.join(d, name) for d in parents]
        hit = [c for c in cands if os.path.lexists(c)]
        if not hit:
            return None
        out.update(os.path.normpath(c) for c in hit)
    return frozenset(out)


def free_output(out: str, volumes: bool = False) -> str:
    """Nom de sortie libre : « nom (2).ext » si l'archive (ou son premier volume) existe déjà.
    Jamais d'écrasement ni de mise à jour silencieuse d'une archive existante (une sauvegarde…)."""
    from .fileops import split_ext
    folder, name = os.path.dirname(out) or ".", os.path.basename(out)

    def taken(n):
        p = os.path.join(folder, n)
        return os.path.lexists(p) or (volumes and os.path.lexists(p + ".001"))
    if not taken(name):
        return out
    base, ext = split_ext(name)
    n = 2
    while True:
        cand = f"{base} ({n}){ext}"
        if not taken(cand):
            return os.path.join(folder, cand)
        n += 1


_7Z_UNREADABLE = re.compile(r"^(.+?) : errno=\d+ : (.+)$")
_TAR_UNREADABLE = re.compile(r"^(?:\S*/)?tar: (.+?): Cannot (?:open|stat|read|readlink)[^:]*: (.+)$")


def unreadable_from_7z(text: str) -> list:
    """Fichiers sautés par 7-Zip (« WARNINGS for files: … errno=13 … ») : [(chemin, raison)]."""
    out, inside = [], False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("WARNINGS for files"):
            inside = True
            continue
        if inside:
            m = _7Z_UNREADABLE.match(line)
            if m:
                out.append((m.group(1), m.group(2)))
            elif line.startswith("---"):
                inside = False
    return out


def unreadable_from_tar(text: str) -> Optional[list]:
    """tar finit en échec (code 2) quand il n'a pas pu lire un fichier, mais l'archive contient
    tout le reste. [(chemin, raison)] si ce sont les seules erreurs, sinon None."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or re.match(r"^(?:\S*/)?tar: Exiting with failure status", line):
            continue
        m = _TAR_UNREADABLE.match(line)
        if not m:
            return None
        out.append((m.group(1), m.group(2)))
    return out


def create(job: Job, sources: list[str], out: str, o: CreateOptions) -> str:
    fmt = formats.FORMATS_BY_KEY[o.fmt]
    if not sources:
        raise ToolError(tr("Aucun fichier à compresser"))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if fmt.engine != "7z" or o.update_mode == "new":
        # tar.*, gz… ne savent pas mettre à jour ; en 7z / zip, « a » modifiait sans question
        # l'archive existante (une sauvegarde…) : on garde les deux, comme Nautilus
        out = free_output(out, volumes=bool(o.volume_size))
    if fmt.engine == "7z":
        # nouvelle archive : écrite sous un nom temporaire puis posée sans écraser. Écrite sous son
        # nom final, une autre compression lancée en même temps vers ce nom y AJOUTAIT ses fichiers
        # (« a » met à jour une archive existante). Volumes (.001…) : nommés par 7-Zip, à part.
        fresh = o.update_mode == "new" and not o.volume_size
        target = out
        if fresh:
            from .fileops import _tmp_name, split_ext
            target = _tmp_name(os.path.dirname(os.path.abspath(out)), "archive") + split_ext(out)[1]
        try:
            text = job.run_tool(build_7z_create(sources, target, o), label=tr("Compression"))
            if fresh:
                from .fileops import place_new
                out = place_new(target, out)
        finally:
            if fresh and os.path.lexists(target):
                os.remove(target)
        # 7-Zip saute un fichier illisible avec un simple avertissement (code 1) : on le dit
        job.skipped_files = getattr(job, "skipped_files", []) + unreadable_from_7z(text)
        _delete_sources_after(job, sources, o, out)
        return out

    from .fileops import _tmp_name
    tmp_out = _tmp_name(os.path.dirname(os.path.abspath(out)), "archive")   # jamais un « X.part » existant
    total = total_size(sources)
    try:
        if fmt.engine == "tar":
            tar_argv, comp_argv = build_tar_create(sources, o)
            with open(tmp_out, "wb") as fout:
                tar = job.popen(tar_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                comp = job.popen(comp_argv, stdin=subprocess.PIPE, stdout=fout, stderr=subprocess.PIPE)
                try:
                    relay(job, tar.stdout, comp.stdin, int(total * 1.02) + 10240,
                          label=tr("Compression") + " ")
                finally:
                    comp.stdin.close()
                terr = tar.stderr.read().decode("utf-8", "replace")
                tar.wait()
                cerr = comp.stderr.read().decode("utf-8", "replace")
                comp.wait()
            job.check_cancel()
            if tar.returncode not in (0, 1):
                skipped = unreadable_from_tar(terr) if tar.returncode == 2 else None
                if not skipped:
                    raise ToolError("tar : " + terr.strip()[-300:], terr, tar.returncode)
                # comme en 7z / zip : l'archive est gardée, les fichiers illisibles sont signalés
                job.skipped_files = getattr(job, "skipped_files", []) + skipped
            if comp.returncode != 0:
                raise ToolError("compression : " + cerr.strip()[-300:], cerr, comp.returncode)
        else:  # flux d'un seul fichier
            src = sources[0]
            if not os.path.isfile(src) or len(sources) > 1:
                raise ToolError(tr("Le format {0} ne compresse qu'un seul fichier ; utilisez TAR.* pour un dossier").format(fmt.label))
            with open(tmp_out, "wb") as fout, open(src, "rb") as fin:
                comp = job.popen(_compress_cmd(fmt.key, o.level), stdin=subprocess.PIPE,
                                 stdout=fout, stderr=subprocess.PIPE)
                try:
                    relay(job, fin, comp.stdin, total, label=tr("Compression") + " ")
                finally:
                    comp.stdin.close()
                cerr = comp.stderr.read().decode("utf-8", "replace")
                comp.wait()
            job.check_cancel()
            if comp.returncode != 0:
                raise ToolError("compression : " + cerr.strip()[-300:], cerr, comp.returncode)
        from .fileops import place_new
        out = place_new(tmp_out, out)      # nom pris entre-temps : « nom (2) », jamais d'écrasement
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    if o.volume_size:
        from .tools import split_file, parse_size
        split_file(job, out, parse_size(o.volume_size), remove_original=True)
    _delete_sources_after(job, sources, o, out)
    return out


def _delete_sources_after(job: Job, sources: list[str], o: CreateOptions, out: str):
    """« Supprimer après » : seulement une fois l'archive posée sous son vrai nom ET écrite sur le
    disque (coupure de courant), et jamais les fichiers que l'outil n'a pas pu lire."""
    if not o.delete_after:
        return
    from .fileops import flush_to_disk
    flush_to_disk(out)
    skipped = getattr(job, "skipped_files", [])
    keep = _skipped_paths(sources, skipped) if skipped else frozenset()
    if keep is not None:          # un fichier sauté introuvable : on ne supprime rien
        _remove_sources(sources, keep)
    else:
        job.sources_kept = True


def create_each(job: Job, sources: list[str], out_dir: str, o: CreateOptions) -> list[str]:
    """Une archive séparée par élément (option « chaque élément dans sa propre archive »)."""
    results = []
    n = len(sources)
    for i, s in enumerate(sources):
        job.check_cancel()
        out = os.path.join(out_dir or os.path.dirname(os.path.abspath(s)),
                           os.path.basename(default_output([s], o.fmt, o.sfx)))
        sub = Job(job.title, lambda j: None)
        sub._cancel = job._cancel
        sub._procs = job._procs
        sub.subscribe(lambda sj, i=i: job.progress((i + sj.fraction) / n,
                                                   f"[{i + 1}/{n}] {sj.status}"))
        out = create(sub, [s], out, o)
        job.commands += sub.commands
        results.append(out)
    return results


# ---------------------------------------------------------------------------
# Extraire / tester
# ---------------------------------------------------------------------------

@dataclass
class ExtractOptions:
    dest: str = ""
    password: str = ""
    overwrite: str = "rename"        # overwrite, skip, rename, rename_existing
    subfolder: str = "smart"         # never, always, smart
    keep_paths: bool = True
    files: list = field(default_factory=list)   # chemins internes à extraire
    open_after: bool = False
    delete_after: bool = False

OVERWRITE_FLAGS = {"overwrite": "-aoa", "skip": "-aos", "rename": "-aou", "rename_existing": "-aot"}


def resolve_destination(archive: str, o: ExtractOptions, listing: Optional[ArchiveListing] = None) -> str:
    dest = o.dest or os.path.dirname(os.path.abspath(archive))
    base = formats.detect(archive).base_name or "archive"
    if o.subfolder == "always":
        # un dossier NEUF : extraire dans un dossier existant du même nom mélangeait (ou, avec
        # « Remplacer », écrasait) ce qu'il contenait
        return _unique_path(os.path.join(dest, base))
    if o.subfolder == "smart" and not o.files:
        if listing is None:
            try:
                listing = list_archive(archive, o.password)
            except ToolError:
                return os.path.join(dest, base)
        top = listing.top_level()
        if len(top) == 1:      # un seul élément à la racine (dossier ou fichier) : pas de sous-dossier
            return dest
        return _unique_path(os.path.join(dest, base))
    return dest


def build_7z_extract(archive: str, dest: str, o: ExtractOptions, stdin_tar=False) -> list[str]:
    argv = [_7z(), "x" if o.keep_paths else "e"]
    if stdin_tar:
        argv += ["-si", "-ttar"]
    else:
        argv += _pw_args(o.password)
    argv += [OVERWRITE_FLAGS.get(o.overwrite, "-aou"), "-o" + dest, "-sccUTF-8"] + SEVENZ_PROGRESS
    argv += ["--"] + ([] if stdin_tar else [archive]) + list(o.files)
    return argv


def describe_extract(archive: str, o: ExtractOptions) -> str:
    det = formats.detect(archive)
    dest = o.dest or os.path.dirname(os.path.abspath(archive))
    try:
        if det.kind == "tar-stream":
            return f"{quote_cmd(_decompress_cmd(det.stream))} < {quote_cmd([archive])} | " + \
                quote_cmd(build_7z_extract(archive, dest, o, stdin_tar=True))
        if det.kind == "stream":
            return f"{quote_cmd(_decompress_cmd(det.stream))} < {quote_cmd([archive])} > " + \
                quote_cmd([os.path.join(dest, det.inner_name)])
        return quote_cmd(build_7z_extract(archive, dest, o))
    except ToolError as e:
        return str(e)


def _place(src: str, dst: str) -> str:
    from .fileops import place_new
    return place_new(src, dst)


def _rename_new(src: str, dst: str):
    from .fileops import _rename_new as rn
    rn(src, dst)


def _unique_path(path: str) -> str:
    """« projet (2) », « notes (2).txt »… comme partout ailleurs (et comme Nautilus)."""
    if not os.path.lexists(path):
        return path
    from .fileops import unique_name
    folder = os.path.dirname(path) or "."
    return os.path.join(folder, unique_name(folder, os.path.basename(path), os.path.isdir(path)))


_DANGEROUS_LINK = re.compile(r"ERROR:\s*Dangerous link path was ignored : (.+?) : (.*)$")
_SUB_ERRORS = re.compile(r"^Sub items Errors:\s*(\d+)")


def skipped_links(err: ToolError) -> Optional[list]:
    """Si 7-Zip n'a fait QUE refuser des liens dangereux (cible hors du dossier d'extraction), tout
    le reste est extrait : renvoie [(lien, cible)]. Sinon None (vraie erreur)."""
    links, other, counted = [], False, None
    for line in (err.output or "").splitlines():
        line = line.strip()
        m = _DANGEROUS_LINK.search(line)
        if m:
            links.append((m.group(1), m.group(2)))
        elif "ERROR" in line:
            other = True
        n = _SUB_ERRORS.match(line)
        if n:
            counted = int(n.group(1))
    if not links or other or (counted is not None and counted != len(links)):
        return None
    return links


def _restore_absolute_links(out_dir: str, names) -> None:
    """7-Zip réécrit un lien absolu d'une archive tar (« zone -> /usr/share/zoneinfo ») en le faisant
    pointer DANS le dossier d'extraction (dans notre dossier temporaire caché pour l'extraction
    intelligente) : lien cassé au nom étrange. Une fois l'extraction finie — plus rien ne peut être
    écrit à travers —, on lui rend sa cible d'origine, comme tar ou Nautilus (File Roller)."""
    prefix = out_dir.rstrip("/") + "/"
    for name in names:
        top = os.path.join(out_dir, name)
        todo = [top] if os.path.islink(top) else []
        if os.path.isdir(top) and not os.path.islink(top):
            for root, dirs, files in os.walk(top):
                todo += [os.path.join(root, n) for n in dirs + files if os.path.islink(os.path.join(root, n))]
        for link in todo:
            try:
                target = os.readlink(link)
                if target.startswith(prefix):
                    os.remove(link)
                    os.symlink("/" + target[len(prefix):], link)
            except OSError:
                pass


def _extract_into(job: Job, archive: str, det, o: ExtractOptions, dest: str):
    before = set(os.listdir(dest)) if os.path.isdir(dest) else set()
    try:
        _extract_raw(job, archive, det, o, dest)
    except ToolError as e:
        links = skipped_links(e)
        if links is None:
            raise
        # liens refusés par 7-Zip, tout le reste est là : on garde, et on le dit
        job.skipped_links = getattr(job, "skipped_links", []) + links
    if det.kind != "stream":
        _restore_absolute_links(dest, set(os.listdir(dest)) - before)


def _extract_raw(job: Job, archive: str, det, o: ExtractOptions, dest: str):
    if det.kind == "tar-stream":
        _run_decompress_into(job, archive, det.stream, build_7z_extract(archive, dest, o, stdin_tar=True),
                             tr("Extraction") + " ")
    elif det.kind == "stream":
        target = os.path.join(dest, det.inner_name or "contenu")
        if os.path.exists(target):
            if o.overwrite == "skip":
                return
            if o.overwrite == "rename":
                target = _unique_path(target)
            elif o.overwrite == "rename_existing":
                os.replace(target, _unique_path(target))
        from .fileops import _tmp_name
        part = _tmp_name(dest, "extrait")              # jamais un « X.part » existant
        try:
            with open(part, "wb") as fout:
                _run_decompress_into(job, archive, det.stream, [], tr("Extraction") + " ", consumer_stdout=fout)
            if o.overwrite == "overwrite" and os.path.lexists(target):
                os.replace(part, target)                # remplacer : choisi explicitement
            else:
                from .fileops import place_new
                place_new(part, target)
        finally:
            if os.path.exists(part):
                os.remove(part)
    else:
        job.run_tool(build_7z_extract(archive, dest, o), label=tr("Extraction"))


def _remove(path: str):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def _extract_smart(job: Job, archive: str, det, o: ExtractOptions) -> str:
    """« Dossier intelligent » en une seule passe, sans lister d'abord (pour un tar.bz2, lister
    oblige à tout décompresser une première fois : long, sans progression, sans annulation).
    Extraction dans un dossier temporaire caché à côté, puis rangement : un seul élément à la racine
    va à côté de l'archive, plusieurs vont dans un dossier au nom de l'archive. Une annulation ou
    une erreur ne laisse rien derrière elle."""
    parent = o.dest or os.path.dirname(os.path.abspath(archive))
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".odysseus-extract-", dir=parent)
    try:
        _extract_into(job, archive, det, o, tmp)
        job.check_cancel()
        entries = os.listdir(tmp)
        if len(entries) == 1:
            src, final = os.path.join(tmp, entries[0]), os.path.join(parent, entries[0])
            if os.path.lexists(final):
                if o.overwrite == "skip":
                    job.extract_skipped = True
                    return parent
                if o.overwrite == "overwrite" and not os.path.isdir(final):
                    os.remove(final)
                elif o.overwrite == "rename_existing":
                    _place(final, _unique_path(final))
                else:
                    final = _unique_path(final)
            final = _place(src, final)
            return final if os.path.isdir(final) else parent
        final = _unique_path(os.path.join(parent, det.base_name or "archive"))
        normal_mode(tmp, is_dir=True)     # mkdtemp crée en 0700
        final = _place(tmp, final)
        tmp = ""
        return final
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def _extract_selected(job: Job, archive: str, det, o: ExtractOptions) -> str:
    """Éléments choisis dans l'archive : posés à destination SANS leur chemin dans l'archive
    (« projet/doc.txt » → « doc.txt »), comme un glisser hors de l'archive. 7-Zip les recréait
    avec leur chemin, donc dans le dossier source « projet » s'il existait à côté de l'archive."""
    dest = resolve_destination(archive, o)
    os.makedirs(dest, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".odysseus-extract-", dir=dest)
    try:
        _extract_into(job, archive, det, o, tmp)
        job.check_cancel()
        for inner in o.files:
            src = os.path.join(tmp, inner.strip("/"))
            if not os.path.lexists(src):
                continue
            final = os.path.join(dest, os.path.basename(inner.rstrip("/")))
            if os.path.lexists(final):
                if o.overwrite == "skip":
                    job.extract_skipped = True
                    continue
                if o.overwrite == "overwrite" and not os.path.isdir(final):
                    os.remove(final)
                elif o.overwrite == "rename_existing":
                    _place(final, _unique_path(final))
                else:
                    final = _unique_path(final)
            _place(src, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def extract(job: Job, archive: str, o: ExtractOptions) -> str:
    det = formats.detect(archive)
    if o.subfolder == "smart" and not o.files:
        dest = _extract_smart(job, archive, det, o)
    elif o.files and o.keep_paths and det.kind != "stream":
        dest = _extract_selected(job, archive, det, o)
    else:
        # Extraction dans un dossier temporaire caché, puis rangement par nous-mêmes : 7-Zip, lui,
        # écrivait à travers un lien symbolique déjà présent à destination (« x » → dossier
        # extérieur : « x/f.txt » atterrissait dehors). Une annulation ne laisse rien derrière elle.
        dest = resolve_destination(archive, o)
        os.makedirs(dest, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".odysseus-extract-", dir=dest)
        try:
            _extract_into(job, archive, det, o, tmp)
            job.check_cancel()
            if _merge_into(tmp, dest, o.overwrite):
                job.extract_skipped = True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if o.delete_after:
        if getattr(job, "extract_skipped", False):
            job.archive_kept = True     # des éléments ont été ignorés : l'archive en est la seule copie
        else:
            from .fileops import flush_to_disk
            flush_to_disk(dest)          # le contenu extrait sur le disque AVANT de retirer l'archive
            for v in volume_set(archive):
                os.remove(v)
    return dest


def _merge_into(src_dir: str, dest_dir: str, overwrite: str) -> bool:
    """Range le contenu extrait (src_dir) dans dest_dir. Un dossier n'est fusionné que s'il est un
    vrai dossier des deux côtés : un lien symbolique à destination n'est jamais suivi, il est traité
    comme un fichier. True si des éléments ont été ignorés (déjà présents, choix « Ignorer »)."""
    skipped = False
    for name in os.listdir(src_dir):
        src, dst = os.path.join(src_dir, name), os.path.join(dest_dir, name)
        src_dir_real = os.path.isdir(src) and not os.path.islink(src)
        if not os.path.lexists(dst):
            try:
                _rename_new(src, dst)
                continue
            except FileExistsError:
                pass                     # arrivé à l'instant (autre extraction) : traité comme existant
        if src_dir_real and os.path.isdir(dst) and not os.path.islink(dst):
            skipped |= _merge_into(src, dst, overwrite)
            continue
        if overwrite == "skip":
            skipped = True
        elif overwrite == "overwrite" and os.path.isdir(dst) and not os.path.islink(dst):
            _place(src, _unique_path(dst))        # un fichier ne remplace jamais tout un dossier
        elif overwrite == "overwrite":
            from .fileops import _swap_into
            _swap_into(src, dst)
        elif overwrite == "rename_existing":
            _place(dst, _unique_path(dst))
            _place(src, dst)
        else:
            _place(src, _unique_path(dst))
    return skipped


def volume_set(archive: str) -> list[str]:
    """Tous les volumes d'une archive découpée (.001, .002…), ou l'archive seule."""
    if archive.endswith(".001"):
        root = archive[:-4]
        vols, i = [], 1
        while os.path.exists(f"{root}.{i:03d}"):
            vols.append(f"{root}.{i:03d}")
            i += 1
        return vols
    return [archive]


def test(job: Job, archive: str, password: str = "") -> str:
    det = formats.detect(archive)
    if det.kind == "tar-stream":
        return _run_decompress_into(job, archive, det.stream,
                                    [_7z(), "t", "-si", "-ttar", "-bso1", "-bse1"], tr("Test") + " ")
    if det.kind == "stream":
        with open(os.devnull, "wb") as null:
            _run_decompress_into(job, archive, det.stream, [], tr("Test") + " ", consumer_stdout=null)
        return tr("Flux valide")
    argv = [_7z(), "t"] + _pw_args(password) + ["-bsp1", "-bso1", "-bse1", "-y", "--", archive]
    return job.run_tool(argv, label=tr("Test"))


# ---------------------------------------------------------------------------
# Modifier une archive existante
# ---------------------------------------------------------------------------

def add_to_archive(job: Job, archive: str, files: list[str], inner_dir: str = "",
                   password: str = "", encrypt_names: bool = False) -> None:
    if not formats.is_modifiable(archive):
        raise ToolError(tr("Ce format ne peut pas être modifié ; convertissez-le d'abord (7Z, ZIP, TAR, WIM)"))
    extra = []
    if password:
        extra.append("-p" + password)
        if encrypt_names and archive.lower().endswith(".7z"):
            extra.append("-mhe=on")
    inner_dir = inner_dir.strip("/")
    if not inner_dir:
        argv = [_7z(), "a"] + extra + ["-sccUTF-8"] + SEVENZ_PROGRESS + ["--", archive] + \
            [os.path.abspath(f).rstrip("/") for f in files]
        job.run_tool(argv, label=tr("Ajout"))
        return
    # 7-Zip n'a pas d'option « ajouter dans un sous-dossier » : on prépare l'arborescence
    with tempfile.TemporaryDirectory(prefix="pressoir-") as tmp:
        stage = os.path.join(tmp, inner_dir)
        os.makedirs(stage, exist_ok=True)
        for f in files:
            target = os.path.join(stage, os.path.basename(f.rstrip("/")))
            try:
                os.symlink(os.path.abspath(f), target)
            except OSError:
                if os.path.isdir(f):
                    shutil.copytree(f, target)
                else:
                    shutil.copy2(f, target)
        top = inner_dir.split("/", 1)[0]
        # 7-Zip suit les liens symboliques par défaut sous Linux : le contenu réel est stocké.
        argv = [_7z(), "a"] + extra + ["-sccUTF-8"] + SEVENZ_PROGRESS + \
            ["--", os.path.abspath(archive), top]
        job.run_tool(argv, cwd=tmp, label=tr("Ajout"))


def delete_from_archive(job: Job, archive: str, paths: list[str], password: str = "") -> None:
    if not formats.is_modifiable(archive):
        raise ToolError(tr("Ce format ne peut pas être modifié"))
    argv = [_7z(), "d"] + _pw_update(password) + ["-sccUTF-8"] + SEVENZ_PROGRESS + ["--", archive] + list(paths)
    job.run_tool(argv, label=tr("Suppression"))


def rename_in_archive(job: Job, archive: str, old: str, new: str, password: str = "") -> None:
    if not formats.is_modifiable(archive):
        raise ToolError(tr("Ce format ne peut pas être modifié"))
    argv = [_7z(), "rn"] + _pw_update(password) + ["-sccUTF-8"] + SEVENZ_PROGRESS + ["--", archive, old, new]
    job.run_tool(argv, label=tr("Renommage"))


def new_folder_in_archive(job: Job, archive: str, inner_path: str, password: str = "") -> None:
    with tempfile.TemporaryDirectory(prefix="pressoir-") as tmp:
        os.makedirs(os.path.join(tmp, inner_path.strip("/")))
        top = inner_path.strip("/").split("/", 1)[0]
        argv = [_7z(), "a"] + (["-p" + password] if password else []) + SEVENZ_PROGRESS + \
            ["--", os.path.abspath(archive), top]
        job.run_tool(argv, cwd=tmp, label=tr("Nouveau dossier"))


def convert(job: Job, archive: str, out: str, o: CreateOptions, password: str = "") -> str:
    """Convertit une archive vers un autre format (extraction temporaire puis recompression)."""
    parent = os.path.dirname(os.path.abspath(out))
    with tempfile.TemporaryDirectory(prefix=".pressoir-conv-", dir=parent) as tmp:
        sub = Job(job.title, lambda j: None)
        sub._cancel, sub._procs = job._cancel, job._procs
        sub.subscribe(lambda sj: job.progress(sj.fraction * 0.5, "1/2 " + sj.status))
        extract(sub, archive, ExtractOptions(dest=tmp, password=password, subfolder="never",
                                             overwrite="overwrite"))
        items = [os.path.join(tmp, n) for n in sorted(os.listdir(tmp))]
        if not items:
            raise ToolError(tr("L'archive est vide"))
        sub2 = Job(job.title, lambda j: None)
        sub2._cancel, sub2._procs = job._cancel, job._procs
        sub2.subscribe(lambda sj: job.progress(0.5 + sj.fraction * 0.5, "2/2 " + sj.status))
        o2 = CreateOptions(**{**o.__dict__, "delete_after": False})
        create(sub2, items, out, o2)
        job.commands += sub.commands + sub2.commands
    return out


def extract_to_temp(job: Job, archive: str, files: list[str], password: str = "") -> str:
    """Extrait quelques éléments dans un dossier temporaire (aperçu / ouverture)."""
    tmp = tempfile.mkdtemp(prefix="pressoir-view-")
    extract(job, archive, ExtractOptions(dest=tmp, password=password, subfolder="never",
                                         overwrite="overwrite", files=files))
    return tmp
