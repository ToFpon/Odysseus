"""Recherche et remplacement dans le contenu des fichiers (reprise de Search Content).

ripgrep si présent (bien plus rapide), sinon grep. Les résultats arrivent au fil de l'eau ;
le séparateur nul (--null) rend fiables les chemins qui contiennent « : ».
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
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
        # --no-ignore : comme grep (et Nautilus), les fichiers ignorés par git sont cherchés aussi — sinon une
        # recherche dans build/ ou *.log d'un dépôt git ne trouvait rien (audit 2026)
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--null", "--max-columns=400",
               "--max-columns-preview", "--no-ignore"]
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
    # fichiers ordinaires seulement, jamais un lien (grep le suivrait, et « Remplacer » remplaçait le lien
    # par un fichier — audit 2026)
    files = [os.path.join(q.folder, f) for f in sorted(os.listdir(q.folder))
             if os.path.isfile(os.path.join(q.folder, f)) and not os.path.islink(os.path.join(q.folder, f))]
    return cmd + ["--", q.pattern] + (files or ["/dev/null"])


def parse_records(buf: bytes) -> tuple[list, bytes]:
    """Résultats complets de `buf` (octets de grep / rg --null) et le reste à compléter. Un enregistrement =
    chemin \0 numéro « : » texte \n — le CHEMIN peut contenir « \n » (nom de fichier permis sous Linux), le
    texte non. Avant (lecture ligne par ligne), « a\nb.txt » donnait le chemin RELATIF « b.txt », et
    « Remplacer » modifiait le b.txt du dossier courant d'Odysseus (audit 2026). Noms non UTF-8 gardés tels
    quels (surrogateescape) : ils s'ouvrent et se remplacent."""
    out = []
    while True:
        z = buf.find(b"\0")
        if z < 0:
            break
        nl = buf.find(b"\n", z + 1)
        if nl < 0:
            break
        path, rest = buf[:z], buf[z + 1:nl]
        buf = buf[nl + 1:]
        num, _, text = rest.partition(b":")
        if not num.isdigit():
            continue
        p = os.fsdecode(path)
        if not os.path.isabs(p):
            continue
        out.append(Match(p, int(num), text.decode("utf-8", "replace").rstrip("\r")))
    return out, buf


def run(q: Query, on_batch: Callable[[list], None], cancelled: Callable[[], bool], limit: int = MAX_RESULTS) -> tuple:
    """Recherche ; on_batch(list[Match]) appelé régulièrement. Renvoie (nombre, limite atteinte, erreur)."""
    try:
        p = subprocess.Popen(build_cmd(q), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    except OSError as e:
        return 0, False, str(e)
    # sortie d'erreur lue à part (dernières lignes gardées) : non lue, au-delà de 64 Ko de « Permission
    # non accordée » (recherche dans / ou /etc), grep se bloquait en écrivant et nous en lisant — pour
    # toujours ; « Arrêter » tue l'outil tout de suite, même s'il ne trouve rien (audit de novembre 2026)
    tail: list = []

    def drain():
        for line in p.stderr:
            tail.append(line.decode("utf-8", "replace"))
            del tail[:-5]

    def watch():
        while p.poll() is None:
            if cancelled():
                _kill(p)
                return
            time.sleep(0.2)
    threading.Thread(target=drain, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    batch, count, truncated = [], 0, False
    pending = b""
    while not truncated:
        chunk = p.stdout.read1(65536)
        if not chunk:
            break
        if cancelled():
            _kill(p)
            break
        found, pending = parse_records(pending + chunk)
        for m in found:
            batch.append(m)
            count += 1
            if len(batch) >= 100:
                on_batch(batch)
                batch = []
            if count >= limit:
                truncated = True
                _kill(p)
                break
    if batch:
        on_batch(batch)
    p.wait()
    err = ""
    if p.returncode not in (0, 1, -9) and not cancelled() and not truncated:
        lines = "".join(tail).strip().splitlines()
        err = lines[-1] if lines else tr("code {0}").format(p.returncode)
    return count, truncated, err


def _kill(p):
    """L'outil et ses enfants (groupe de processus : start_new_session)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


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
        tmp = None
        try:
            from .fileops import _fsync_dir, _tmp_name
            # lien symbolique : c'est le VRAI fichier qu'on remplace, le lien reste (audit 2026 : le lien
            # devenait un fichier, le vrai restait tel quel)
            real = os.path.realpath(path)
            if backup:
                bak = backup_name(path)
                shutil.copy2(real, bak)
                if backups is not None:
                    backups.append(bak)
            st = os.stat(real)
            folder = os.path.dirname(real)
            tmp = _tmp_name(folder, "remplacer")
            # création exclusive (jamais à travers un lien posé sous ce nom), mise sur disque avant l'échange
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(parts))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, real)
            tmp = None
            _fsync_dir(folder)
            total += n_file
            files += 1
        except OSError as e:
            errors.append((path, e.strerror or str(e)))
        finally:
            if tmp is not None and os.path.lexists(tmp):
                os.unlink(tmp)
    return total, files, errors
