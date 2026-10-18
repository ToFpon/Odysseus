"""Exécution des outils externes avec suivi de progression et annulation."""

from __future__ import annotations

import os
import stat
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from ..i18n import decimal, tr

_PERCENT_RE = re.compile(r"(\d{1,3})%")


def _read_umask() -> int:
    m = os.umask(0o022)
    os.umask(m)
    return m


UMASK = _read_umask()


def normal_mode(path: str, is_dir: bool = False, like: Optional[str] = None):
    """Droits d'un fichier produit : ceux de « like » (le fichier remplacé) s'il existe, sinon ceux
    d'un fichier ordinaire (0666 / 0777 moins l'umask). mkstemp / mkdtemp créent en 0600 / 0700."""
    try:
        if like and os.path.exists(like):
            os.chmod(path, stat.S_IMODE(os.stat(like).st_mode))
        else:
            os.chmod(path, (0o777 if is_dir else 0o666) & ~UMASK)
    except OSError:
        pass


def sevenzip_path() -> Optional[str]:
    for name in ("7zz", "7z", "7za"):
        p = shutil.which(name)
        if p:
            return p
    return None


def sevenzip_version() -> str:
    exe = sevenzip_path()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        if line.strip().startswith("7-Zip"):
            return line.strip()
    return ""


def sfx_module() -> Optional[str]:
    for p in ("/usr/lib/7zip/7zCon.sfx", "/usr/lib/p7zip/7zCon.sfx",
              "/usr/libexec/p7zip/7zCon.sfx", "/usr/local/lib/7zip/7zCon.sfx"):
        if os.path.exists(p):
            return p
    exe = sevenzip_path()
    if exe:
        cand = os.path.join(os.path.dirname(os.path.realpath(exe)), "7zCon.sfx")
        if os.path.exists(cand):
            return cand
    return None


class Cancelled(Exception):
    pass


class ToolError(Exception):
    def __init__(self, message: str, output: str = "", code: int = 0):
        super().__init__(message)
        self.output = output
        self.code = code


class PasswordRequired(ToolError):
    pass


def quote_cmd(argv: list[str]) -> str:
    """Commande lisible, mot de passe masqué."""
    shown = []
    for a in argv:
        if a.startswith("-p") and len(a) > 2 and not a.startswith("-pass"):
            a = "-p********"
        shown.append(shlex.quote(a))
    return " ".join(shown)


@dataclass
class Job:
    """Une tâche de fond : compression, extraction, test, hachage…"""
    title: str
    func: Callable[["Job"], object]
    commands: list[str] = field(default_factory=list)
    fraction: float = 0.0
    status: str = tr("En attente")
    state: str = "pending"          # pending, running, done, failed, cancelled
    result: object = None
    error: str = ""
    log: list[str] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event)
    _procs: list = field(default_factory=list)
    _listeners: list = field(default_factory=list)
    needs_password: bool = False
    _thread: Optional[threading.Thread] = None

    # -- notifications ---------------------------------------------------
    def subscribe(self, cb: Callable[["Job"], None]):
        self._listeners.append(cb)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb(self)
            except Exception:
                pass

    def progress(self, fraction: Optional[float] = None, status: Optional[str] = None):
        if fraction is not None:
            self.fraction = max(0.0, min(1.0, fraction))
        if status is not None:
            self.status = status
        self._notify()

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()
        for p in list(self._procs):
            try:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # -- exécution -------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def run_sync(self):
        self._run()
        return self

    def _run(self):
        self.state = "running"
        self.started = time.time()
        self.progress(0.0, tr("Démarrage…"))
        try:
            self.result = self.func(self)
            if self._cancel.is_set():
                raise Cancelled()
            self.state = "done"
            self.fraction = 1.0
            self.status = tr("Terminé")
        except Cancelled:
            self.state = "cancelled"
            self.status = tr("Annulé")
        except PasswordRequired as e:
            self.state = "failed"
            self.needs_password = True
            self.error = str(e) or tr("Mot de passe requis ou incorrect")
            self.status = self.error
        except ToolError as e:
            self.state = "failed"
            self.error = str(e)
            if e.output:
                self.log.append(e.output)
            self.status = tr("Échec : ") + self.error
        except OSError as e:
            # « Permission non accordée : dossier », pas « PermissionError: [Errno 13]… »
            self.state = "failed"
            what = os.path.basename(str(e.filename).rstrip("/")) if e.filename else ""
            self.error = (os.strerror(e.errno) if e.errno else str(e)) + (f" : {what}" if what else "")
            self.log.append(f"{type(e).__name__}: {e}")
            self.status = tr("Échec : ") + self.error
        except Exception as e:  # erreur inattendue
            self.state = "failed"
            self.error = f"{type(e).__name__}: {e}"
            self.status = tr("Échec : ") + self.error
        self.finished = time.time()
        self._notify()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # -- lancement de processus -----------------------------------------
    def popen(self, argv, **kw) -> subprocess.Popen:
        self.check_cancel()
        self.commands.append(quote_cmd(argv))
        kw.setdefault("stdin", subprocess.DEVNULL)
        p = subprocess.Popen(argv, start_new_session=True, **kw)
        self._procs.append(p)
        return p

    def run_tool(self, argv: list[str], weight: tuple[float, float] = (0.0, 1.0),
                 cwd: Optional[str] = None, label: str = "") -> str:
        """Lance un outil 7-Zip et suit son pourcentage (sortie -bsp1)."""
        p = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
        start, span = weight
        buf = b""
        output = []
        while True:
            chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n\x08]+", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = _PERCENT_RE.match(line)
                if m:
                    pct = int(m.group(1))
                    rest = line[m.end():].strip()
                    rest = re.sub(r"^\d+\s*", "", rest).lstrip("+-U=. ").strip()
                    self.progress(start + span * pct / 100.0,
                                  (label + " — " if label else "") + (rest or f"{pct} %"))
                else:
                    output.append(line)
        if buf.strip():
            output.append(buf.decode("utf-8", "replace").strip())
        code = p.wait()
        text = "\n".join(output)
        if self._cancel.is_set():
            raise Cancelled()
        if code not in (0, 1):  # 1 = avertissement non fatal chez 7-Zip
            raise_for_7z(text, code)
        if text:
            self.log.append(text)
        return text


def raise_for_7z(text: str, code: int):
    low = text.lower()
    if "wrong password" in low or "can not open encrypted" in low or \
            "cannot open encrypted" in low or "data error in encrypted" in low:
        raise PasswordRequired(tr("Mot de passe manquant ou incorrect"), text, code)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # les récapitulatifs (« Sub items Errors: 1 », « Archives with Errors: 1 », « ERRORS: ») ne disent
    # rien : on garde la première vraie ligne d'erreur
    summary = re.compile(r"^((sub items |archives with )?errors?:?\s*\d*|warnings?:?\s*\d*)$", re.I)
    real = [re.sub(r"^.*?ERROR:\s*", "", l) for l in lines if "ERROR:" in l]
    others = [l for l in lines if ("error" in l.lower() or "erreur" in l.lower()) and not summary.match(l)]
    plain = [l for l in lines if not summary.match(l)]
    msg = (real[0] if real else others[0] if others else plain[-1] if plain else
           (lines[-1] if lines else tr("code {0}").format(code)))
    raise ToolError(msg.strip(), text, code)


def run_capture(argv: list[str], timeout: Optional[float] = None, input_bytes=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout,
                          input=input_bytes, stdin=None if input_bytes is not None else subprocess.DEVNULL)


def relay(job: Job, src, dst, total: int, weight=(0.0, 1.0), label: str = "",
          block: int = 1 << 20) -> int:
    """Copie un flux en mettant à jour la progression ; renvoie le nombre d'octets."""
    done = 0
    last = 0.0
    start, span = weight
    while True:
        job.check_cancel()
        data = src.read(block)
        if not data:
            break
        dst.write(data)
        done += len(data)
        now = time.monotonic()
        if now - last > 0.1:
            last = now
            frac = done / total if total else 0.0
            job.progress(start + span * min(frac, 1.0),
                         f"{label}{human_size(done)}" + (f" / {human_size(total)}" if total else ""))
    return done


def human_size(n: Optional[float]) -> str:
    if n is None:
        return ""
    # Unités décimales (1 Ko = 1000 octets), comme Nautilus et g_format_size
    n = float(n)
    units = (tr("o"), tr("Ko"), tr("Mo"), tr("Go"), tr("To"))
    for i, unit in enumerate(units):
        if abs(n) < 1000 or i == len(units) - 1:
            if i == 0:
                return f"{int(n)} {unit}"
            return f"{decimal(n)} {unit}"
        n /= 1000
    return f"{decimal(n)} {units[-1]}"
