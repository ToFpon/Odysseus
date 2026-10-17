"""Audio / vidéo : durée (colonne Durée, avec cache disque) et extraction de la piste audio."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

from ..i18n import tr
from ..settings import CACHE_DIR
from .engine import Job, ToolError

MEDIA_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts",
    ".ogv", ".vob", ".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".opus", ".aac", ".wma", ".aiff",
})
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg",
                        ".3gp", ".ts", ".ogv", ".vob"})

# (extension, codec ffmpeg)
AUDIO_FORMATS = [("mp3", "libmp3lame"), ("m4a", "aac"), ("ogg", "libvorbis"), ("opus", "libopus"),
                 ("flac", "flac"), ("wav", "pcm_s16le")]
QUALITIES = ["high", "medium", "low", "copy"]
QUALITY_BITRATES = {"high": "320k", "medium": "192k", "low": "128k"}
LOSSLESS = {"flac", "wav"}

CACHE_FILE = os.path.join(CACHE_DIR, "durations.json")
CACHE_DAYS = 30


def is_media(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(round(seconds))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def probe(path: str) -> dict:
    """Informations ffprobe : durée, codecs, dimensions, étiquettes."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def probe_duration(path: str) -> Optional[float]:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", path],
                           capture_output=True, text=True, timeout=8)
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class DurationCache:
    """Cache JSON des durées (repris de duration-column), invalidé si le fichier change."""

    def __init__(self, path: str = CACHE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._dirty = 0
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}
        cutoff = time.time() - CACHE_DAYS * 86400
        for k in [k for k, v in self.data.items() if v.get("a", 0) < cutoff]:
            del self.data[k]

    def get(self, path: str, mtime: float) -> Optional[float]:
        e = self.data.get(path)
        if e and e.get("m") == int(mtime):
            e["a"] = int(time.time())
            return e.get("d")
        return None

    def put(self, path: str, mtime: float, dur: Optional[float]):
        with self._lock:
            self.data[path] = {"d": dur, "m": int(mtime), "a": int(time.time())}
            self._dirty += 1
            if self._dirty >= 10:
                self.save_locked()

    def save_locked(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, separators=(",", ":"))
            os.replace(self.path + ".tmp", self.path)
            self._dirty = 0
        except OSError:
            pass

    def flush(self):
        with self._lock:
            if self._dirty:
                self.save_locked()

    def duration(self, path: str, mtime: float) -> Optional[float]:
        d = self.get(path, mtime)
        if d is not None or path in self.data and self.data[path].get("m") == int(mtime):
            return d
        d = probe_duration(path)
        self.put(path, mtime, d)
        return d


durations = DurationCache()


def _parse_time(t: str) -> float:
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


def audio_output(video: str, ext: str, dest_dir: str = "") -> str:
    base = os.path.splitext(os.path.basename(video))[0]
    out = os.path.join(dest_dir or os.path.dirname(video), f"{base}.{ext}")
    n = 2
    while os.path.exists(out):
        out = os.path.join(dest_dir or os.path.dirname(video), f"{base} ({n}).{ext}")
        n += 1
    return out


def extract_audio(job: Job, videos: list[str], fmt: str, quality: str, dest_dir: str = "",
                  on_file=None) -> list[tuple[str, bool]]:
    """Extrait la piste audio de chaque vidéo. on_file(index, ok) signale chaque fin de fichier."""
    codec = dict(AUDIO_FORMATS)[fmt]
    results = []
    total = len(videos)
    for i, video in enumerate(videos):
        job.check_cancel()
        out = audio_output(video, fmt, dest_dir)
        duration = probe_duration(video) or 0.0
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", video, "-vn", "-progress", "pipe:1", "-nostats"]
        if quality == "copy":
            cmd += ["-acodec", "copy"]
        elif fmt in LOSSLESS:
            cmd += ["-acodec", codec]
        else:
            cmd += ["-acodec", codec, "-b:a", QUALITY_BITRATES[quality]]
        cmd.append(out)
        p = job.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tail = []
        for line in p.stdout:
            line = line.strip()
            if line.startswith("out_time=") and duration > 0:
                cur = _parse_time(line.split("=", 1)[1])
                frac = min(cur / duration, 1.0)
                job.progress((i + frac) / total,
                             tr("{0} ({1} sur {2})").format(os.path.basename(video), i + 1, total))
            elif not re.match(r"^\w+=", line):
                tail.append(line)
        ok = p.wait() == 0 and not job.cancelled
        if not ok and os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        if not ok and not job.cancelled:
            job.log.append("\n".join(tail[-20:]))
        results.append((out, ok))
        if on_file:
            on_file(i, ok)
    job.check_cancel()
    if results and not any(ok for _o, ok in results):
        raise ToolError(tr("Aucune piste audio n'a pu être extraite"), "\n".join(job.log))
    return results
