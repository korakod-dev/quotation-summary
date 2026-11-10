"""Run state shared with the desktop app: state.json (what is running / what happened) and the run lock.

state.json (schema 1), written ONLY by a real run of `python -m qsum` while it holds the run lock:

    {"schema": 1,
     "current":  null | {started_at, trigger, pid, total, done, current_file},
     "last_run": null | {started_at, finished_at, trigger, exit_code, fatal, n_pdf,
                         stats{cached, claude, text, failed}, issues[], output_file, excel_written},
     "files":    {"<sha256>": {rel, outcome, message, at}}}        # only the hashes seen in the last run

`current` is non-null while a run is going. If `current` is non-null but nobody holds the lock, the run
crashed or was killed (qsum.status reports that as running:false + last_run.interrupted:true).

The lock is `fcntl.flock` on <state_dir>/run.lock. The kernel drops it when the process dies, so it can never
go stale. Readers (qsum.status) probe it with is_running(), which never creates a file or folder.

Stdlib only (qsum.status imports this module and must stay light). Python 3.9 compatible.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SCHEMA_VERSION = 1
TRIGGERS = ("schedule", "app", "manual")
MAX_STATE_ISSUES = 100          # keep state.json small even when hundreds of files warn
MAX_ISSUE_CHARS = 600
OUTCOMES = ("cached", "claude", "text", "failed")


# ---------------------------------------------------------------- time helpers

def now_local() -> datetime:
    """Timezone-aware local time (fixed offset), the base of every timestamp we write."""
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    """ISO 8601 with local offset, seconds precision (e.g. 2026-11-10T08:00:00+07:00)."""
    return dt.isoformat(timespec="seconds")


def trigger_from_env(env: Optional[Dict[str, str]] = None) -> str:
    """QS_TRIGGER = schedule | app | manual (anything else, or unset, = manual)."""
    value = str((env if env is not None else os.environ).get("QS_TRIGGER", "")).strip().lower()
    return value if value in TRIGGERS else "manual"


# ---------------------------------------------------------------- state file

def empty_state() -> Dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "current": None, "last_run": None, "files": {}}


def read_state(path: Path) -> Dict[str, Any]:
    """Read state.json; a missing, unreadable, corrupt or foreign-schema file gives an empty state (never raises)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return empty_state()
    state = empty_state()
    if isinstance(data.get("current"), dict):
        state["current"] = data["current"]
    if isinstance(data.get("last_run"), dict):
        state["last_run"] = data["last_run"]
    if isinstance(data.get("files"), dict):
        state["files"] = {k: v for k, v in data["files"].items() if isinstance(v, dict)}
    return state


def write_state(path: Path, state: Dict[str, Any]) -> None:
    """Atomic write: temp file in the same folder + os.replace, so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- run lock

class RunLock:
    """Exclusive, non-blocking run lock (flock). Use as: lock = RunLock(path); if lock.acquire(): ... lock.release().

    The file descriptor is opened close-on-exec (Python default), so child processes such as `claude -p`
    never inherit the lock.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    # A status probe (is_running) holds a SHARED lock for a few microseconds; a real run starting at that very
    # moment would see "busy". Retrying briefly makes that clash harmless.
    ACQUIRE_ATTEMPTS = 3
    ACQUIRE_RETRY_SECONDS = 0.1

    def acquire(self) -> bool:
        """True = we hold the lock. False = someone else runs (after a few short retries).

        Raises OSError if the lock file cannot be created or the filesystem cannot lock at all (ENOTSUP / ENOLCK on a
        network drive): only BlockingIOError means "held elsewhere"; the caller decides what to do with the rest.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        for attempt in range(self.ACQUIRE_ATTEMPTS):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:          # EWOULDBLOCK / EAGAIN = held elsewhere
                if attempt + 1 < self.ACQUIRE_ATTEMPTS:
                    time.sleep(self.ACQUIRE_RETRY_SECONDS)
                continue
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
            return True
        os.close(fd)
        return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def is_running(lock_path: Path) -> bool:
    """True when another process holds the run lock. Read-only: never creates the file or its folders.

    Takes a shared lock without blocking; it succeeds only when no exclusive holder exists. Any error other
    than "held" (missing file, no permission) means "not running".
    """
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------- recorder used by a real run

class RunRecorder:
    """Writes state.json for one real run. Also the `reporter` passed to qsum.main.process().

    The previous last_run / files stay in the file while this run is going, so the app can still show them.
    A failing disk write is swallowed (the run itself must not die because the status file is unwritable);
    `write_errors` counts them.
    """

    def __init__(self, path: Path, trigger: str, clock: Callable[[], datetime] = now_local):
        self.path = Path(path)
        self.trigger = trigger if trigger in TRIGGERS else "manual"
        self.clock = clock
        self.write_errors = 0
        self.state = read_state(self.path)
        self.started_at = iso(self.clock())
        self.total = 0
        self.done = 0
        self.began = False                          # begin() was called = this run listed the folder
        self.files: Dict[str, Dict[str, Any]] = {}   # sha -> entry of THIS run

    # -- persistence
    def _save(self) -> None:
        try:
            write_state(self.path, self.state)
        except OSError:
            self.write_errors += 1

    def _current(self, current_file: Optional[str]) -> Dict[str, Any]:
        return {"started_at": self.started_at, "trigger": self.trigger, "pid": os.getpid(),
                "total": self.total, "done": self.done, "current_file": current_file}

    # -- run lifecycle
    def start(self) -> None:
        """Call right after the lock was acquired."""
        self.state["current"] = self._current(None)
        self._save()

    # -- reporter interface (called by process())
    def begin(self, total: int) -> None:
        self.began = True
        self.total = int(total)
        self.done = 0
        self.state["current"] = self._current(None)
        self._save()

    def file_started(self, rel: str) -> None:
        self.state["current"] = self._current(rel)
        self._save()

    def file_done(self, rel: str, sha: str, outcome: str, message: str = "") -> None:
        self.done += 1
        if sha:     # a file that could not even be hashed has no key (qsum.status reports it itself)
            self.files[sha] = {"rel": rel, "outcome": outcome if outcome in OUTCOMES else "failed",
                               "message": message or "", "at": iso(self.clock())}
        self.state["current"] = self._current(None)
        self._save()

    def finish(self, exit_code: int, fatal: Optional[str] = None, n_pdf: int = 0,
               stats: Optional[Dict[str, int]] = None, issues: Optional[List[str]] = None,
               output_file: Optional[Path] = None, excel_written: bool = False) -> None:
        """Write last_run (+ files of this run) and clear `current`. Call on EVERY exit path after the lock."""
        clean_issues = [str(m)[:MAX_ISSUE_CHARS] for m in (issues or [])[:MAX_STATE_ISSUES]]
        self.state["last_run"] = {
            "started_at": self.started_at,
            "finished_at": iso(self.clock()),
            "trigger": self.trigger,
            "exit_code": int(exit_code),
            "fatal": fatal,
            "n_pdf": int(n_pdf),
            "stats": {k: int((stats or {}).get(k, 0)) for k in ("cached", "claude", "text", "failed")},
            "issues": clean_issues,
            "output_file": str(output_file) if output_file else None,
            "excel_written": bool(excel_written),
        }
        if self.began:      # a run that died before listing the folder keeps the previous per-file results
            self.state["files"] = self.files
        self.state["current"] = None
        self._save()
