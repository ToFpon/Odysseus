"""Outils annexes : sommes de contrôle, suppression sécurisée, découpage, doublons…"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import string
import subprocess
import zlib
from dataclasses import dataclass
from typing import Iterable, Optional

from .engine import Job, ToolError, human_size, sevenzip_path
from ..i18n import tr

# ---------------------------------------------------------------------------
# Sommes de contrôle
# ---------------------------------------------------------------------------


class _CRC32:
    name = "crc32"

    def __init__(self):
        self.v = 0

    def update(self, b):
        self.v = zlib.crc32(b, self.v)

    def hexdigest(self):
        return f"{self.v & 0xFFFFFFFF:08x}"


class _Adler32(_CRC32):
    name = "adler32"

    def __init__(self):
        self.v = 1

    def update(self, b):
        self.v = zlib.adler32(b, self.v)


class _CRC64:
    """CRC-64/XZ (ECMA-182), identique à celui de 7-Zip et xz."""
    name = "crc64"
    _table = None

    def __init__(self):
        if _CRC64._table is None:
            poly = 0xC96C5795D7870F42
            t = []
            for i in range(256):
                c = i
                for _ in range(8):
                    c = (c >> 1) ^ poly if c & 1 else c >> 1
                t.append(c)
            _CRC64._table = t
        self.v = 0xFFFFFFFFFFFFFFFF

    def update(self, b):
        t, v = self._table, self.v
        for byte in b:
            v = t[(v ^ byte) & 0xFF] ^ (v >> 8)
        self.v = v

    def hexdigest(self):
        return f"{self.v ^ 0xFFFFFFFFFFFFFFFF:016x}"


HASH_ALGOS = {
    "CRC32": _CRC32,
    "CRC64": _CRC64,
    "Adler32": _Adler32,
    "MD5": lambda: hashlib.md5(),
    "SHA-1": lambda: hashlib.sha1(),
    "SHA-224": lambda: hashlib.sha224(),
    "SHA-256": lambda: hashlib.sha256(),
    "SHA-384": lambda: hashlib.sha384(),
    "SHA-512": lambda: hashlib.sha512(),
    "SHA3-256": lambda: hashlib.sha3_256(),
    "SHA3-512": lambda: hashlib.sha3_512(),
    "BLAKE2b": lambda: hashlib.blake2b(),
    "BLAKE2s": lambda: hashlib.blake2s(),
}

# Algorithmes lents en Python pur : désactivés par défaut.
SLOW_ALGOS = {"CRC64"}
DEFAULT_ALGOS = ["CRC32", "MD5", "SHA-256"]
HASH_FILE_EXT = {"MD5": "md5", "SHA-1": "sha1", "SHA-224": "sha224", "SHA-256": "sha256",
                 "SHA-384": "sha384", "SHA-512": "sha512", "SHA3-256": "sha3-256",
                 "SHA3-512": "sha3-512", "BLAKE2b": "b2", "BLAKE2s": "b2s", "CRC32": "sfv"}


def iter_files(paths: Iterable[str]) -> list[str]:
    out = []
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp) and not os.path.islink(fp):
                        out.append(fp)
        elif os.path.isfile(p):
            out.append(p)
    return out


def hash_files(job: Job, paths: list[str], algos: list[str]) -> list[dict]:
    files = iter_files(paths)
    total = sum(os.path.getsize(f) for f in files) or 1
    done = 0
    results = []
    for f in files:
        hs = {a: HASH_ALGOS[a]() for a in algos}
        size = os.path.getsize(f)
        with open(f, "rb") as fh:
            while True:
                job.check_cancel()
                b = fh.read(1 << 20)
                if not b:
                    break
                for h in hs.values():
                    h.update(b)
                done += len(b)
                job.progress(done / total, os.path.basename(f))
        results.append({"path": f, "size": size, **{a: h.hexdigest() for a, h in hs.items()}})
    return results


def hash_text(text: str, algo: str) -> str:
    h = HASH_ALGOS[algo]()
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def format_hash_file(results: list[dict], algo: str, base_dir: str) -> str:
    lines = []
    for r in results:
        rel = os.path.relpath(r["path"], base_dir)
        if algo == "CRC32":
            lines.append(f"{rel} {r[algo].upper()}")
        else:
            lines.append(f"{r[algo]}  {rel}")
    return "\n".join(lines) + "\n"


_LINE_GNU = re.compile(r"^([0-9a-fA-F]{8,128})\s+\*?(.+)$")
_LINE_BSD = re.compile(r"^(\w[\w-]*)\s*\((.+)\)\s*=\s*([0-9a-fA-F]+)$")
_LINE_SFV = re.compile(r"^(.+)\s+([0-9a-fA-F]{8})$")
_LEN_TO_ALGO = {8: "CRC32", 32: "MD5", 40: "SHA-1", 56: "SHA-224", 64: "SHA-256",
                96: "SHA-384", 128: "SHA-512"}


def _guess_algo(checksum_file: str, digest: str) -> str:
    ext = checksum_file.lower().rsplit(".", 1)[-1]
    for algo, e in HASH_FILE_EXT.items():
        if e == ext:
            return algo
    return _LEN_TO_ALGO.get(len(digest), "SHA-256")


def verify_hash_file(job: Job, checksum_file: str) -> list[dict]:
    """Vérifie un fichier .md5/.sha256/.sfv… Renvoie une ligne par fichier."""
    base = os.path.dirname(os.path.abspath(checksum_file))
    todo = []
    with open(checksum_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith((";", "#")):
                continue
            m = _LINE_BSD.match(line)
            if m:
                algo = m.group(1).upper().replace("SHA", "SHA-").replace("SHA--", "SHA-")
                algo = {"SHA-3-256": "SHA3-256", "BLAKE2B": "BLAKE2b"}.get(algo, algo)
                todo.append((m.group(2), m.group(3).lower(), algo if algo in HASH_ALGOS
                             else _guess_algo(checksum_file, m.group(3))))
                continue
            m = _LINE_GNU.match(line)
            if m and not checksum_file.lower().endswith(".sfv"):
                todo.append((m.group(2), m.group(1).lower(), _guess_algo(checksum_file, m.group(1))))
                continue
            m = _LINE_SFV.match(line)
            if m:
                todo.append((m.group(1).strip(), m.group(2).lower(), "CRC32"))
    out = []
    n = len(todo) or 1
    for i, (name, expected, algo) in enumerate(todo):
        job.check_cancel()
        path = name if os.path.isabs(name) else os.path.join(base, name)
        job.progress(i / n, name)
        if not os.path.isfile(path):
            out.append({"path": path, "algo": algo, "expected": expected, "actual": "", "status": tr("absent")})
            continue
        h = HASH_ALGOS[algo]()
        with open(path, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                job.check_cancel()
                h.update(b)
        actual = h.hexdigest()
        out.append({"path": path, "algo": algo, "expected": expected, "actual": actual,
                    "status": "ok" if actual == expected else tr("différent")})
    return out


# ---------------------------------------------------------------------------
# Suppression sécurisée
# ---------------------------------------------------------------------------

def secure_delete(job: Job, paths: list[str], passes: int = 3, zero_last: bool = True) -> int:
    """Écrase le contenu (aléatoire, puis zéros), renomme puis supprime."""
    files = iter_files(paths)
    total = sum(os.path.getsize(f) for f in files) * max(1, passes) or 1
    done = 0
    for f in files:
        size = os.path.getsize(f)
        with open(f, "r+b", buffering=0) as fh:
            for p in range(passes):
                fh.seek(0)
                remaining = size
                last = zero_last and p == passes - 1
                while remaining > 0:
                    job.check_cancel()
                    n = min(remaining, 1 << 20)
                    fh.write(b"\0" * n if last else secrets.token_bytes(n))
                    remaining -= n
                    done += n
                    job.progress(done / total, tr('Passe {0}/{passes} — {1}').format(p + 1, os.path.basename(f), passes=passes))
                os.fsync(fh.fileno())
            fh.truncate(0)
        _rename_and_remove(f)
    # dossiers (du plus profond au moins profond)
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            for root, dirs, _ in os.walk(p, topdown=False):
                for d in dirs:
                    _rename_and_remove(os.path.join(root, d), is_dir=True)
            _rename_and_remove(p, is_dir=True)
        elif os.path.islink(p):
            os.remove(p)
    return len(files)


def _rename_and_remove(path: str, is_dir: bool = False):
    parent = os.path.dirname(path)
    target = path
    try:
        for _ in range(3):
            new = os.path.join(parent, secrets.token_hex(8))
            os.rename(target, new)
            target = new
    except OSError:
        pass
    if is_dir:
        try:
            os.rmdir(target)
        except OSError:
            import shutil
            shutil.rmtree(target, ignore_errors=True)
    else:
        os.remove(target)


# ---------------------------------------------------------------------------
# Découper / joindre
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([kmgt]?)(i?b|o)?\s*$", re.I)
PRESET_VOLUMES = [
    (tr("Personnalisé"), ""),
    (tr("1,44 Mo (disquette)"), "1457664b"),
    (tr("10 Mo (pièce jointe)"), "10m"),
    (tr("25 Mo (courriel)"), "25m"),
    (tr("100 Mo"), "100m"),
    (tr("650 Mo (CD)"), "650m"),
    (tr("700 Mo (CD)"), "700m"),
    (tr("2 Go"), "2g"),
    (tr("4 Go (FAT32)"), "4095m"),
    (tr("4,7 Go (DVD)"), "4480m"),
    (tr("8,5 Go (DVD DL)"), "8100m"),
    (tr("25 Go (Blu-ray)"), "23800m"),
]


def parse_size(text: str) -> int:
    t = text.strip().lower()
    if t.endswith("b") and t[:-1].isdigit():
        return int(t[:-1])
    m = _SIZE_RE.match(t)
    if not m:
        raise ToolError(tr('Taille invalide : {text}').format(text=text))
    num = float(m.group(1).replace(",", "."))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2).lower()]
    value = int(num * mult)
    if value <= 0:
        raise ToolError(tr("La taille doit être positive"))
    return value


def split_file(job: Job, path: str, chunk: int, out_dir: str = "", remove_original: bool = False,
               write_hash: bool = False) -> list[str]:
    size = os.path.getsize(path)
    out_dir = out_dir or os.path.dirname(os.path.abspath(path))
    base = os.path.join(out_dir, os.path.basename(path))
    parts = []
    done = 0
    sha = hashlib.sha256() if write_hash else None
    with open(path, "rb") as fin:
        i = 1
        while True:
            job.check_cancel()
            name = f"{base}.{i:03d}"
            written = 0
            with open(name, "wb") as fout:
                while written < chunk:
                    data = fin.read(min(1 << 20, chunk - written))
                    if not data:
                        break
                    fout.write(data)
                    if sha:
                        sha.update(data)
                    written += len(data)
                    done += len(data)
                    job.progress(done / (size or 1), tr('Volume {i} — {0}').format(human_size(done), i=i))
            if written == 0 and i > 1:
                os.remove(name)
                break
            parts.append(name)
            if written < chunk:
                break
            i += 1
    if sha:
        with open(base + ".sha256", "w") as fh:
            fh.write(f"{sha.hexdigest()}  {os.path.basename(path)}\n")
    if remove_original:
        os.remove(path)
    return parts


def find_volumes(first: str) -> list[str]:
    m = re.match(r"^(.*)\.(\d{3})$", first)
    if not m:
        raise ToolError(tr("Choisissez le premier volume (fichier se terminant par .001)"))
    root = m.group(1)
    vols, i = [], 1
    while os.path.exists(f"{root}.{i:03d}"):
        vols.append(f"{root}.{i:03d}")
        i += 1
    if not vols:
        raise ToolError(tr("Aucun volume trouvé"))
    return vols


def join_files(job: Job, first: str, out: str = "") -> str:
    vols = find_volumes(first)
    out = out or re.sub(r"\.\d{3}$", "", first)
    if os.path.exists(out):
        raise ToolError(tr('Le fichier existe déjà : {out}').format(out=out))
    total = sum(os.path.getsize(v) for v in vols) or 1
    done = 0
    from .fileops import _tmp_name
    part = _tmp_name(os.path.dirname(os.path.abspath(out)), "volumes")   # jamais un « X.part » existant
    try:
        with open(part, "wb") as fout:
            for v in vols:
                with open(v, "rb") as fin:
                    for b in iter(lambda: fin.read(1 << 20), b""):
                        job.check_cancel()
                        fout.write(b)
                        done += len(b)
                        job.progress(done / total, os.path.basename(v))
        from .fileops import place_new
        out = place_new(part, out)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return out


# ---------------------------------------------------------------------------
# Doublons et comparaison
# ---------------------------------------------------------------------------

def find_duplicates(job: Job, paths: list[str], min_size: int = 1) -> list[list[str]]:
    job.progress(0, tr("Inventaire des fichiers…"))
    by_size: dict[int, list[str]] = {}
    for f in iter_files(paths):
        try:
            s = os.path.getsize(f)
        except OSError:
            continue
        if s >= min_size:
            by_size.setdefault(s, []).append(f)
    candidates = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in candidates) or 1
    done = 0
    groups = []
    for group in candidates:
        # premier tri rapide sur les 64 premiers Kio, puis empreinte complète
        by_head: dict[bytes, list[str]] = {}
        for f in group:
            with open(f, "rb") as fh:
                by_head.setdefault(fh.read(65536), []).append(f)
        for sub in by_head.values():
            if len(sub) < 2:
                done += len(sub)
                continue
            by_hash: dict[str, list[str]] = {}
            for f in sub:
                job.check_cancel()
                h = hashlib.blake2b()
                with open(f, "rb") as fh:
                    for b in iter(lambda: fh.read(1 << 20), b""):
                        h.update(b)
                by_hash.setdefault(h.hexdigest(), []).append(f)
                done += 1
                job.progress(done / total, os.path.basename(f))
            groups += [sorted(g) for g in by_hash.values() if len(g) > 1]
    groups.sort(key=lambda g: -os.path.getsize(g[0]) * (len(g) - 1))
    return groups


@dataclass
class CompareResult:
    identical: bool
    size_a: int
    size_b: int
    first_difference: Optional[int]
    sha_a: str
    sha_b: str


def compare_files(job: Job, a: str, b: str) -> CompareResult:
    sa, sb = os.path.getsize(a), os.path.getsize(b)
    ha, hb = hashlib.sha256(), hashlib.sha256()
    first = None
    offset = 0
    total = max(sa, sb) or 1
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            job.check_cancel()
            ba, bb = fa.read(1 << 20), fb.read(1 << 20)
            if not ba and not bb:
                break
            ha.update(ba)
            hb.update(bb)
            if first is None and ba != bb:
                for i in range(min(len(ba), len(bb))):
                    if ba[i] != bb[i]:
                        first = offset + i
                        break
                else:
                    first = offset + min(len(ba), len(bb))
            offset += max(len(ba), len(bb))
            job.progress(offset / total, tr("Comparaison"))
    return CompareResult(first is None and sa == sb, sa, sb, first, ha.hexdigest(), hb.hexdigest())


def compare_folders(job: Job, a: str, b: str) -> dict:
    def index(root):
        out = {}
        for f in iter_files([root]):
            out[os.path.relpath(f, root)] = f
        return out
    ia, ib = index(a), index(b)
    only_a = sorted(set(ia) - set(ib))
    only_b = sorted(set(ib) - set(ia))
    common = sorted(set(ia) & set(ib))
    different = []
    for i, rel in enumerate(common):
        job.check_cancel()
        job.progress(i / (len(common) or 1), rel)
        if os.path.getsize(ia[rel]) != os.path.getsize(ib[rel]):
            different.append(rel)
            continue
        with open(ia[rel], "rb") as x, open(ib[rel], "rb") as y:
            while True:
                bx, by = x.read(1 << 20), y.read(1 << 20)
                if bx != by:
                    different.append(rel)
                    break
                if not bx:
                    break
    return {"only_a": only_a, "only_b": only_b, "different": different,
            "same": len(common) - len(different)}


# ---------------------------------------------------------------------------
# Mots de passe et banc d'essai
# ---------------------------------------------------------------------------

def generate_password(length: int = 24, lower=True, upper=True, digits=True, symbols=True,
                      avoid_ambiguous=False) -> str:
    pools = []
    if lower:
        pools.append(string.ascii_lowercase)
    if upper:
        pools.append(string.ascii_uppercase)
    if digits:
        pools.append(string.digits)
    if symbols:
        pools.append("!#$%&*+-=?@^_~.,:;")
    if not pools:
        pools.append(string.ascii_letters)
    if avoid_ambiguous:
        pools = ["".join(c for c in p if c not in "Il1O0o") for p in pools]
    alphabet = "".join(pools)
    length = max(length, len(pools))
    while True:
        pw = [secrets.choice(alphabet) for _ in range(length)]
        if all(any(c in p for c in pw) for p in pools):
            return "".join(pw)


def password_strength(pw: str) -> tuple[float, str]:
    """Entropie estimée en bits et appréciation."""
    import math
    if not pw:
        return 0.0, tr("Aucun")
    pool = 0
    if any(c.islower() for c in pw):
        pool += 26
    if any(c.isupper() for c in pw):
        pool += 26
    if any(c.isdigit() for c in pw):
        pool += 10
    if any(not c.isalnum() for c in pw):
        pool += 32
    bits = len(pw) * math.log2(pool or 1)
    if bits < 40:
        label = tr("Faible")
    elif bits < 64:
        label = tr("Moyen")
    elif bits < 100:
        label = tr("Fort")
    else:
        label = tr("Très fort")
    return bits, label


def benchmark(job: Job, threads: int = 0, dictionary_mb: int = 0) -> dict:
    exe = sevenzip_path()
    if not exe:
        raise ToolError(tr("7-Zip est introuvable"))
    argv = [exe, "b", "1"]
    if threads:
        argv.append(f"-mmt{threads}")
    if dictionary_mb:
        argv.append(f"-md{dictionary_mb}m")
    job.progress(0.05, tr("Banc d'essai en cours…"))
    p = job.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        lines.append(line)
        m = re.match(r"^(\d+):", line.strip())
        if m:
            job.progress(min(0.9, 0.1 + 0.03 * len(lines)), tr('Dictionnaire 2^{0}').format(m.group(1)))
    p.wait()
    job.check_cancel()
    text = "\n".join(lines)
    res = {"raw": text}
    for line in lines:
        if line.startswith("Avr:"):
            nums = re.findall(r"\d+", line)
            if len(nums) >= 6:
                res["compress_kbs"] = int(nums[0]); res["compress_mips"] = int(nums[2])
                res["decompress_kbs"] = int(nums[3]); res["decompress_mips"] = int(nums[5])
        if line.startswith("Tot:"):
            nums = re.findall(r"\d+", line)
            if nums:
                res["total_mips"] = int(nums[-1])
    return res
