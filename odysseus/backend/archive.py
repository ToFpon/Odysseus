"""Opérations sur les archives : lister, créer, extraire, tester, modifier, convertir."""

from __future__ import annotations

import os
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
                     sevenzip_path, sfx_module, quote_cmd)

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
    prog, _, dargs = formats.STREAM_TOOLS[stream]
    exe = shutil.which(prog)
    if not exe:
        raise ToolError(tr('Le programme « {prog} » est nécessaire pour ce format').format(prog=prog))
    return [exe] + dargs


def _compress_cmd(stream: str, level: Optional[int]) -> list[str]:
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
    update_mode: str = "add"         # add, update, sync, fresh
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
    cmd = {"add": "a", "update": "u", "sync": "u", "fresh": "u"}.get(o.update_mode, "a")
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
    if o.delete_after:
        argv.append("-sdel")
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


def _remove_sources(sources: list[str]):
    for s in sources:
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.rmtree(s, ignore_errors=True)
        elif os.path.lexists(s):
            os.remove(s)


def create(job: Job, sources: list[str], out: str, o: CreateOptions) -> str:
    fmt = formats.FORMATS_BY_KEY[o.fmt]
    if not sources:
        raise ToolError(tr("Aucun fichier à compresser"))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if fmt.engine == "7z":
        job.run_tool(build_7z_create(sources, out, o), label=tr("Compression"))
        return out

    tmp_out = out + ".part"
    total = total_size(sources)
    try:
        if fmt.engine == "tar":
            tar_argv, comp_argv = build_tar_create(sources, o)
            with open(tmp_out, "wb") as fout:
                tar = job.popen(tar_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                comp = job.popen(comp_argv, stdin=subprocess.PIPE, stdout=fout, stderr=subprocess.PIPE)
                try:
                    relay(job, tar.stdout, comp.stdin, int(total * 1.02) + 10240,
                          label="Compression ")
                finally:
                    comp.stdin.close()
                terr = tar.stderr.read().decode("utf-8", "replace")
                tar.wait()
                cerr = comp.stderr.read().decode("utf-8", "replace")
                comp.wait()
            job.check_cancel()
            if tar.returncode not in (0, 1):
                raise ToolError("tar : " + terr.strip()[-300:], terr, tar.returncode)
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
                    relay(job, fin, comp.stdin, total, label="Compression ")
                finally:
                    comp.stdin.close()
                cerr = comp.stderr.read().decode("utf-8", "replace")
                comp.wait()
            job.check_cancel()
            if comp.returncode != 0:
                raise ToolError("compression : " + cerr.strip()[-300:], cerr, comp.returncode)
        os.replace(tmp_out, out)
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    if o.volume_size:
        from .tools import split_file, parse_size
        split_file(job, out, parse_size(o.volume_size), remove_original=True)
    if o.delete_after:
        _remove_sources(sources)
    return out


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
        create(sub, [s], out, o)
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
        return os.path.join(dest, base)
    if o.subfolder == "smart" and not o.files:
        if listing is None:
            try:
                listing = list_archive(archive, o.password)
            except ToolError:
                return os.path.join(dest, base)
        top = listing.top_level()
        if len(top) == 1 and (top[0].is_dir or listing.kind == "stream"):
            return dest
        return os.path.join(dest, base)
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


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def extract(job: Job, archive: str, o: ExtractOptions) -> str:
    det = formats.detect(archive)
    dest = resolve_destination(archive, o)
    os.makedirs(dest, exist_ok=True)
    if det.kind == "tar-stream":
        _run_decompress_into(job, archive, det.stream, build_7z_extract(archive, dest, o, stdin_tar=True),
                             "Extraction ")
    elif det.kind == "stream":
        target = os.path.join(dest, det.inner_name or "contenu")
        if os.path.exists(target):
            if o.overwrite == "skip":
                return dest
            if o.overwrite == "rename":
                target = _unique_path(target)
            elif o.overwrite == "rename_existing":
                os.replace(target, _unique_path(target))
        part = target + ".part"
        try:
            with open(part, "wb") as fout:
                _run_decompress_into(job, archive, det.stream, [], "Extraction ", consumer_stdout=fout)
            os.replace(part, target)
        finally:
            if os.path.exists(part):
                os.remove(part)
    else:
        job.run_tool(build_7z_extract(archive, dest, o), label=tr("Extraction"))
    if o.delete_after:
        for v in volume_set(archive):
            os.remove(v)
    return dest


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
                                    [_7z(), "t", "-si", "-ttar", "-bso1", "-bse1"], "Test ")
    if det.kind == "stream":
        with open(os.devnull, "wb") as null:
            _run_decompress_into(job, archive, det.stream, [], "Test ", consumer_stdout=null)
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
