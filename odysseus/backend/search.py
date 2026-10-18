"""Recherche et remplacement dans le contenu des fichiers (reprise de Search Content).

ripgrep si présent (bien plus rapide), sinon grep. Les résultats arrivent au fil de l'eau ;
le séparateur nul (--null) rend fiables les chemins qui contiennent « : ».
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from ..i18n import tr

MAX_RESULTS = 5000
SKIP_DIRS = (".git", "node_modules", "__pycache__", ".venv", ".tox")


def tool() -> str:
    return "rg" if shutil.which("rg") else "grep"


@dataclass
class Query:
    pattern: str
    folder: str
    exts: str = ""
    recursive: bool = True
    case: bool = False
    regex: bool = False
    word: bool = False
    hidden: bool = False


@dataclass
class Match:
    path: str
    line: int
    text: str


def ext_list(text: str) -> list[str]:
    return [e.strip().lstrip("*").lstrip(".") for e in re.split(r"[,;\s]+", text) if e.strip()]


def build_cmd(q: Query) -> list[str]:
    exts = ext_list(q.exts)
    if tool() == "rg":
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--null", "--max-columns=400",
               "--max-columns-preview"]
        if not q.case:
            cmd.append("--ignore-case")
        if not q.regex:
            cmd.append("--fixed-strings")
        if q.word:
            cmd.append("--word-regexp")
        if not q.recursive:
            cmd.append("--max-depth=1")
        if q.hidden:
            cmd.append("--hidden")
        for d in SKIP_DIRS:
            cmd += ["--glob", f"!{d}"]
        for e in exts:
            cmd += ["--glob", f"*.{e}"]
        return cmd + ["--", q.pattern, q.folder]
    cmd = ["grep", "-n", "-H", "--null", "--binary-files=without-match"]
    for d in SKIP_DIRS:
        cmd.append(f"--exclude-dir={d}")
    if not q.hidden:
        cmd += ["--exclude=.*", "--exclude-dir=.*"]
    if q.recursive:
        cmd.append("-r")
    if not q.case:
        cmd.append("-i")
    cmd.append("-E" if q.regex else "-F")
    if q.word:
        cmd.append("-w")
    for e in exts:
        cmd.append(f"--include=*.{e}")
    if q.recursive:
        return cmd + ["--", q.pattern, q.folder]
    files = [os.path.join(q.folder, f) for f in sorted(os.listdir(q.folder))
             if os.path.isfile(os.path.join(q.folder, f))]
    return cmd + ["--", q.pattern] + (files or ["/dev/null"])


def parse_line(raw: str) -> Optional[Match]:
    if "\0" not in raw:
        return None
    path, rest = raw.split("\0", 1)
    num, _, text = rest.partition(":")
    if not num.isdigit():
        return None
    return Match(path, int(num), text.rstrip("\n"))


def run(q: Query, on_batch: Callable[[list], None], cancelled: Callable[[], bool], limit: int = MAX_RESULTS) -> tuple:
    """Recherche ; on_batch(list[Match]) appelé régulièrement. Renvoie (nombre, limite atteinte, erreur)."""
    try:
        p = subprocess.Popen(build_cmd(q), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             errors="replace", start_new_session=True)
    except OSError as e:
        return 0, False, str(e)
    batch, count, truncated = [], 0, False
    for raw in p.stdout:
        if cancelled():
            p.kill()
            break
        m = parse_line(raw)
        if m is None:
            continue
        batch.append(m)
        count += 1
        if len(batch) >= 100:
            on_batch(batch)
            batch = []
        if count >= limit:
            truncated = True
            p.kill()
            break
    if batch:
        on_batch(batch)
    p.wait()
    err = ""
    if p.returncode not in (0, 1, -9) and not cancelled() and not truncated:
        err = (p.stderr.read() or "").strip().splitlines()[-1:] or [tr("code {0}").format(p.returncode)]
        err = err[0]
    return count, truncated, err


def compile_rx(q: Query) -> re.Pattern:
    flags = 0 if q.case else re.IGNORECASE
    pat = q.pattern if q.regex else re.escape(q.pattern)
    if q.word:
        pat = rf"\b(?:{pat})\b"
    return re.compile(pat, flags)


_CUT = re.compile(r" \[\.\.\. (omitted end of long line|\d+ more matches?)\]$")


def _full_line(path: str, number: int, cache: dict) -> Optional[str]:
    """Ligne complète du fichier (coupée sur « \n » comme ripgrep, comme apply_replace)."""
    if path not in cache:
        try:
            with open(path, "rb") as fh:
                cache[path] = fh.read().decode("utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            cache[path] = None
    lines = cache[path]
    return lines[number - 1] if lines is not None and 0 < number <= len(lines) else None


def preview_replace(q: Query, matches: list[Match], replacement: str) -> list[tuple[Match, str]]:
    """Aperçu calculé sur la ligne entière : ripgrep coupe les lignes de plus de 400 colonnes
    (« [... omitted end of long line] »), et l'aperçu ne montrait alors qu'une partie de ce que
    apply_replace remplaçait (ou ne proposait pas un remplacement situé après la colonne 400)."""
    rx = compile_rx(q)
    out, cache = [], {}
    for m in matches:
        text = m.text
        if _CUT.search(text):
            full = _full_line(m.path, m.line, cache)
            if full is None:
                continue
            text = full
        try:
            after = rx.sub(replacement if q.regex else replacement.replace("\\", "\\\\"), text)
        except re.error:
            continue
        if after != text:
            out.append((m, after))
    return out


def backup_name(path: str) -> str:
    """« fichier.bak », ou « .bak2 », « .bak3 »… : jamais par-dessus une sauvegarde existante."""
    out, n = path + ".bak", 1
    while os.path.lexists(out):
        n += 1
        out = f"{path}.bak{n}"
    return out


def apply_replace(q: Query, targets: dict, replacement: str, backup: bool = True,
                  backups: Optional[list] = None) -> tuple[int, int, list]:
    """targets : chemin → ensemble de numéros de ligne. Renvoie (remplacements, fichiers, erreurs).
    backups, si fourni, reçoit les sauvegardes créées."""
    rx = compile_rx(q)
    repl = replacement if q.regex else replacement.replace("\\", "\\\\")
    total = files = 0
    errors = []
    for path, lines in targets.items():
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append((path, tr("encodage non UTF-8, fichier laissé intact")))
            continue
        except OSError as e:
            errors.append((path, e.strerror or str(e)))
            continue
        # lignes coupées sur « \n » seulement, comme ripgrep qui a donné les numéros : splitlines()
        # coupe aussi sur \r, \f, \x85, U+2028… et la mauvaise ligne était modifiée
        parts = text.split("\n")
        n_file = 0
        for i in range(len(parts)):
            if i + 1 in lines:
                new, n = rx.subn(repl, parts[i])
                if n:
                    parts[i] = new
                    n_file += n
        if not n_file:
            continue
        try:
            if backup:
                bak = backup_name(path)
                shutil.copy2(path, bak)
                if backups is not None:
                    backups.append(bak)
            st = os.stat(path)
            tmp = os.path.join(os.path.dirname(path), f".odysseus-remplacer-{os.getpid()}")
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(parts))
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, path)
            total += n_file
            files += 1
        except OSError as e:
            errors.append((path, e.strerror or str(e)))
    return total, files, errors
