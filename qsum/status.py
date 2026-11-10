"""Read-only status for the desktop app:  python -m qsum.status --json [--config FILE]

Prints ONE JSON document (UTF-8, ensure_ascii=False) on stdout and exits 0 when ok, 1 otherwise.
It never calls Claude, Keychain (security), launchctl or pdftotext, never imports openpyxl / anthropic,
and writes nothing: it only reads config.json, the PDF folder (listing + SHA-256), cache.json, state.json,
the run lock (probe) and the launchd plist file.

JSON (schema 1):
  schema, engine_version, generated_at, ok, error_code, error, source_folder, output_file, output_exists,
  output_mtime, running, progress, last_run, schedule, counts, files[]
  error_code: null | not_installed | config | source_missing | source_unreadable
  For not_installed / config only schema, ok, error_code, error, generated_at are guaranteed (the others are
  absent); for source_* every field is present, files is [] and ok is false.
  files[].status: done | check | waiting | error, ordered error, waiting, check, done, then name (case-insensitive).

The statuses mirror what the next real run (qsum.main.process) would do with each PDF:
  cache hit, method claude, no warnings, not a dropped duplicate  -> done
  cache hit, method text                                          -> check (will_retry unless extractor is "text")
  cache hit with validation warnings / unmatched company / dropped duplicate or older Rev -> check
  no cache hit, state says the last attempt failed                -> error (message from state.json)
  no cache hit otherwise                                          -> waiting
  file cannot be read (hash fails)                                -> error

Stdlib only plus qsum.config / cache / records / pdfs / state. Python 3.9 compatible.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Tuple

from . import __version__
from .cache import MAX_CLAUDE_ATTEMPTS, Cache, sha256_file
from .config import Config, ConfigError, load_config
from .pdfs import SourceUnreadable, list_pdfs
from .records import Entry, dedupe, match_company, validate_record
from .state import is_running, iso, now_local, read_state

SCHEMA_VERSION = 1
LAUNCHD_LABEL = "com.korakod.quotation-summary"
STATUS_ORDER = {"error": 0, "waiting": 1, "check": 2, "done": 3}
WAITING_DETAIL = "ยังไม่ได้อ่าน รอรอบถัดไป"
TEXT_DETAIL = "อ่านด้วย pdftotext (ไม่ใช่ Claude) ควรตรวจเทียบกับ PDF"


# ---------------------------------------------------------------- schedule

def next_run(weekday: Optional[int], hour: int, minute: int, now: datetime) -> datetime:
    """The first moment STRICTLY after `now` that matches a launchd StartCalendarInterval.

    weekday uses launchd numbering: 0 or 7 = Sunday, 1 = Monday ... 6 = Saturday. None = every day.
    The result has the same tzinfo as `now` (naive stays naive). Wall-clock arithmetic: no DST handling beyond
    what the tzinfo of `now` does (Thailand has no DST).
    """
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        cand = base
        while cand <= now:
            cand += timedelta(days=1)
        return cand
    py_weekday = (int(weekday) - 1) % 7        # Python: Monday = 0 ... Sunday = 6
    days = (py_weekday - base.weekday()) % 7
    cand = base + timedelta(days=days)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def read_plist(agents_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The launchd plist of the weekly job as a dict, or None when it is missing / unreadable."""
    folder = agents_dir or Path(os.environ.get("QS_LAUNCH_AGENTS_DIR") or "~/Library/LaunchAgents").expanduser()
    try:
        with open(str(Path(folder) / (LAUNCHD_LABEL + ".plist")), "rb") as f:
            data = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return data if isinstance(data, dict) else None


def _schedule(plist_data: Optional[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    empty = {"installed": False, "label": None, "weekday": None, "hour": None, "minute": None, "next_run": None}
    if not plist_data:
        return empty
    sci = plist_data.get("StartCalendarInterval")
    intervals = [x for x in sci if isinstance(x, dict)] if isinstance(sci, list) else ([sci] if isinstance(sci, dict) else [])
    out = dict(empty, installed=True, label=plist_data.get("Label"))
    if not intervals:
        return out                      # installed, but no calendar schedule we understand
    first = intervals[0]
    out["weekday"] = first.get("Weekday")
    out["hour"] = first.get("Hour", 0)
    out["minute"] = first.get("Minute", 0)
    upcoming = []
    for it in intervals:
        try:
            upcoming.append(next_run(it.get("Weekday"), int(it.get("Hour", 0)), int(it.get("Minute", 0)), now))
        except (TypeError, ValueError):
            continue
    if upcoming:
        out["next_run"] = iso(min(upcoming))
    return out


# ---------------------------------------------------------------- per-file rows

def _added_date(path: Path, st: Optional[os.stat_result]) -> Optional[str]:
    """Date the file landed in the folder: birth time where the filesystem has it (macOS), else mtime."""
    if st is None:
        return None
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts).date().isoformat()


def _row(name: str, sha: Optional[str], size: Optional[int], added: Optional[str], status: str,
         method: Optional[str] = None, will_retry: bool = False, detail: str = "",
         record: Optional[Dict[str, Any]] = None, company: Optional[str] = None) -> Dict[str, Any]:
    rec = record or {}
    return {"name": name, "sha256": sha, "size": size, "added": added, "status": status, "method": method,
            "will_retry": will_retry, "detail": detail, "quote_no": rec.get("quote_no") or None,
            "company": company, "total": rec.get("total")}


def _file_rows(cfg: Config, cache_entries: Dict[str, Dict[str, Any]], state: Dict[str, Any],
               pdfs: List[Path]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    entries: List[Entry] = []
    meta: Dict[str, Tuple[Optional[int], Optional[str]]] = {}      # rel -> (size, added)
    state_files = state.get("files") or {}
    for pdf in pdfs:
        rel = str(pdf.relative_to(cfg.source_folder))
        try:
            st: Optional[os.stat_result] = pdf.stat()
        except OSError:
            st = None
        size = st.st_size if st else None
        added = _added_date(pdf, st)
        try:
            sha = sha256_file(pdf)
        except OSError as e:
            rows.append(_row(rel, None, size, added, "error", will_retry=True,
                             detail="อ่านไฟล์ไม่ได้ (%s)" % (e.strerror or e)))
            continue
        hit = cache_entries.get(sha)
        if hit and isinstance(hit.get("record"), dict):
            entries.append(Entry(rel, sha, hit["record"], hit.get("method", "text"), hit.get("first_seen") or added or ""))
            meta[rel] = (size, added)
            continue
        failed = state_files.get(sha) or {}
        if failed.get("outcome") == "failed":
            rows.append(_row(rel, sha, size, added, "error", will_retry=True,
                             detail=str(failed.get("message") or "อ่านไม่สำเร็จ")))
        else:
            rows.append(_row(rel, sha, size, added, "waiting", will_retry=True, detail=WAITING_DETAIL))

    # Same judgement as process(): company match, validation warnings, then de-duplication.
    for e in entries:
        e.company = match_company(e.record, cfg.companies)
        e.warnings = validate_record(e.record)
        if e.company is None:
            e.warnings.append("ผู้ขาย '%s' ไม่ตรงกับบริษัทใน config" % (e.record.get("seller_name") or "ไม่พบ"))
    kept, dropped = dedupe(entries)
    for e, why in dropped:
        size, added = meta[e.file_name]
        rows.append(_row(e.file_name, e.sha256, size, e.first_seen or added, "check", method=e.method,
                         detail=why, record=e.record, company=e.company.sheet_name if e.company else None))
    for e in kept:
        size, added = meta[e.file_name]
        hit = cache_entries.get(e.sha256) or {}
        reasons: List[str] = []
        will_retry = False
        if e.method == "text":
            will_retry = cfg.extractor != "text"
            reasons.append(TEXT_DETAIL + ("; รอบหน้าจะลองอ่านด้วย Claude ใหม่" if will_retry else ""))
        elif hit.get("warned") and hit.get("attempts", 1) < MAX_CLAUDE_ATTEMPTS and cfg.extractor != "text":
            will_retry = True           # process() reads a warned Claude result once more
        reasons.extend(e.warnings)
        status = "check" if reasons else "done"
        rows.append(_row(e.file_name, e.sha256, size, e.first_seen or added, status, method=e.method,
                         will_retry=will_retry, detail="; ".join(reasons), record=e.record,
                         company=e.company.sheet_name if e.company else None))

    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["name"].lower(), r["name"]))
    return rows


# ---------------------------------------------------------------- run info

def _last_run(state: Dict[str, Any], running: bool) -> Optional[Dict[str, Any]]:
    """last_run for the JSON. A `current` left behind with no lock holder = a crashed run (interrupted)."""
    current = state.get("current")
    last = state.get("last_run")
    if current and not running:
        return {"started_at": current.get("started_at"), "finished_at": None, "trigger": current.get("trigger"),
                "exit_code": None, "interrupted": True, "stats": None, "issues": []}
    if not last:
        return None
    issues = [str(m) for m in (last.get("issues") or [])]
    if last.get("fatal"):
        issues.insert(0, str(last["fatal"]))        # the fatal reason is shown like any other issue
    return {"started_at": last.get("started_at"), "finished_at": last.get("finished_at"),
            "trigger": last.get("trigger"), "exit_code": last.get("exit_code"), "interrupted": False,
            "stats": last.get("stats"), "issues": issues}


def _progress(state: Dict[str, Any], running: bool) -> Optional[Dict[str, Any]]:
    cur = state.get("current")
    if not (running and cur):
        return None
    return {"total": cur.get("total", 0), "done": cur.get("done", 0), "current_file": cur.get("current_file"),
            "started_at": cur.get("started_at"), "trigger": cur.get("trigger")}


def _output_info(cfg: Config) -> Tuple[bool, Optional[str]]:
    try:
        st = cfg.output_file.stat()
    except OSError:
        return False, None
    return True, iso(datetime.fromtimestamp(st.st_mtime).astimezone())


def _minimal(error_code: str, error: str, now: datetime) -> Dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "ok": False, "error_code": error_code, "error": error,
            "generated_at": iso(now)}


def build_status(cfg: Config, cache_entries: Dict[str, Dict[str, Any]], state: Dict[str, Any],
                 plist_data: Optional[Dict[str, Any]], now: datetime,
                 running: Optional[bool] = None) -> Dict[str, Any]:
    """The status document (see the module docstring). `running` None = probe the lock file of `cfg`."""
    if running is None:
        running = is_running(cfg.lock_file)
    out_exists, out_mtime = _output_info(cfg)
    doc: Dict[str, Any] = {
        "schema": SCHEMA_VERSION, "engine_version": __version__, "generated_at": iso(now),
        "ok": True, "error_code": None, "error": None,
        "source_folder": str(cfg.source_folder), "output_file": str(cfg.output_file),
        "output_exists": out_exists, "output_mtime": out_mtime,
        "running": running, "progress": _progress(state, running),
        "last_run": _last_run(state, running), "schedule": _schedule(plist_data, now),
        "counts": {"total": 0, "done": 0, "check": 0, "waiting": 0, "error": 0}, "files": [],
    }
    try:
        is_dir = cfg.source_folder.is_dir()
    except OSError as e:
        return dict(doc, ok=False, error_code="source_unreadable",
                    error="อ่านโฟลเดอร์ PDF ไม่ได้ (%s) macOS อาจกันสิทธิ์ไว้" % (e.strerror or e))
    if not is_dir:
        return dict(doc, ok=False, error_code="source_missing",
                    error="ไม่พบโฟลเดอร์ PDF: %s" % cfg.source_folder)
    try:
        pdfs = list_pdfs(cfg.source_folder, cfg.recursive)
    except SourceUnreadable as e:
        return dict(doc, ok=False, error_code="source_unreadable", error=str(e))
    rows = _file_rows(cfg, cache_entries, state, pdfs)
    counts = {"total": len(rows), "done": 0, "check": 0, "waiting": 0, "error": 0}
    for r in rows:
        counts[r["status"]] += 1
    doc["files"] = rows
    doc["counts"] = counts
    return doc


# ---------------------------------------------------------------- CLI

def collect(config_path: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Load everything from disk (read-only) and build the status document. Never raises."""
    now = now or now_local()
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        return _minimal("config", str(e), now)
    except Exception as e:                       # noqa: BLE001 - the app needs JSON, whatever went wrong
        return _minimal("config", "อ่านไฟล์ตั้งค่าไม่ได้: %s: %s" % (type(e).__name__, e), now)
    try:
        # Read state.json BEFORE probing the lock. A run that finished in between leaves `current` seen in our copy but
        # the lock free, which looks like a crash: re-read both once before believing "interrupted". A run that
        # takes the lock first and writes `current` after, can never look interrupted this way.
        state = read_state(cfg.state_file)
        running = is_running(cfg.lock_file)
        if state.get("current") and not running:
            state = read_state(cfg.state_file)
            running = is_running(cfg.lock_file)
        return build_status(cfg, Cache(cfg.cache_file).entries, state, read_plist(), now, running=running)
    except Exception as e:                       # noqa: BLE001
        return _minimal("config", "อ่านสถานะไม่ได้: %s: %s" % (type(e).__name__, e), now)


def main(argv: Optional[List[str]] = None, out: Optional[IO[bytes]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qsum.status", description="Read-only JSON status for the desktop app.")
    ap.add_argument("--json", action="store_true", help="print JSON (the only output format; accepted for clarity)")
    ap.add_argument("--config", help="path to config.json")
    args = ap.parse_args(argv)
    doc = collect(args.config)
    data = (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    stream = out if out is not None else sys.stdout.buffer      # bytes: independent of the locale
    stream.write(data)
    stream.flush()
    return 0 if doc.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
