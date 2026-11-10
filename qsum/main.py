"""CLI entry point: scan folder -> extract new PDFs (cache by hash) -> write the Excel summary.

Usage:
    python -m qsum [--config FILE] [--folder DIR] [--offline] [--reextract] [--print-output-path]
                   [--print-keychain-service] [--print-claude-token-service]
                   [--print-claude-config-dir]

Exit codes: 0 ok, 1 config/fatal error, 2 workbook written but some PDFs could not be read,
3 another run holds the run lock (a real run is already going; nothing was started).

Env: QS_TRIGGER = schedule | app | manual (who started this run; recorded in state.json, default manual).
Every real run (not the --print-* flags) takes the run lock, keeps state.json up to date and writes
last_run + files at the end on every exit path after the config was loaded (see qsum/state.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .cache import MAX_CLAUDE_ATTEMPTS, Cache, sha256_file
from .config import Config, ConfigError, load_config
from .excel_writer import write_workbook
from .extract_claude import ClaudeExtractor, ClaudeUnavailable, get_api_key
from .extract_claude_code import ClaudeCodeExtractor, get_oauth_token, resolve_claude_path
from .extract_text import ExtractionError
from .extract_text import extract as extract_text
from .pdfs import SourceUnreadable, list_pdfs, unreadable_message
from .records import Entry, dedupe, match_company, normalize_record, validate_record
from .state import RunLock, RunRecorder, trigger_from_env

# Names that used to live here stay importable from qsum.main (tests and callers use them).
__all__ = ["MAX_CLAUDE_ATTEMPTS", "SourceUnreadable", "list_pdfs", "process", "main"]


def process(cfg: Config, claude: Optional[Any], reextract: bool = False,
            log=print, today: Optional[date] = None,
            startup_issue: Optional[str] = None,
            reporter: Optional[Any] = None) -> Tuple[List[Entry], List[str], int, Dict[str, int]]:
    """Extract every PDF (using the cache). Returns (entries, issues, n_pdf, stats).

    `claude` is any extractor with extract(path) and model_label (ClaudeExtractor or ClaudeCodeExtractor).
    `startup_issue`: why Claude could not be set up at start (auto mode); shown first in the issues list.
    `reporter` (optional) is told about progress, for the desktop app (see qsum.state.RunRecorder):
        begin(total)                                  once, after the folder was listed
        file_started(rel)                             before each PDF
        file_done(rel, sha, outcome, message)         after each PDF; outcome is cached | claude | text | failed
                                                      (sha is "" when the file could not even be hashed)
    A reporter must never raise; the return value does not depend on it."""
    today_iso = (today or date.today()).isoformat()
    cache = Cache(cfg.cache_file)
    entries: List[Entry] = []
    issues: List[str] = []
    if startup_issue:
        issues.append("Claude ใช้ไม่ได้: %s (ใช้ pdftotext แทน)" % startup_issue)
    pdfs = list_pdfs(cfg.source_folder, cfg.recursive, issues)
    claude_errors: List[str] = []       # per-file Claude failures (summarised in `issues` below)
    claude_at_start = claude is not None
    stats = {"cached": 0, "claude": 0, "text": 0, "failed": 0}
    use_claude = claude is not None

    def started(rel_name: str) -> None:
        if reporter is not None:
            reporter.file_started(rel_name)

    def done(rel_name: str, sha_hex: str, outcome: str, message: str = "") -> None:
        if reporter is not None:
            reporter.file_done(rel_name, sha_hex, outcome, message)

    if reporter is not None:
        reporter.begin(len(pdfs))
    for pdf in pdfs:
        rel = str(pdf.relative_to(cfg.source_folder))
        started(rel)
        try:
            sha = sha256_file(pdf)
        except OSError as e:
            issues.append("%s: อ่านไฟล์ไม่ได้ (%s)" % (rel, e))
            stats["failed"] += 1
            done(rel, "", "failed", "อ่านไฟล์ไม่ได้ (%s)" % (e.strerror or e))
            continue

        hit = cache.get(sha)
        # A cache entry made by the text fallback is upgraded once Claude becomes available.
        # A Claude result that had validation warnings gets one more read (it may have been truncated
        # or misread); after MAX_CLAUDE_ATTEMPTS it is kept as is.
        stale = use_claude and bool(hit) and (
            hit["method"] == "text"
            or (hit.get("warned") and hit.get("attempts", 1) < MAX_CLAUDE_ATTEMPTS))
        if hit and not reextract and not stale:
            stats["cached"] += 1
            entries.append(Entry(rel, sha, hit["record"], hit["method"], hit["first_seen"]))
            done(rel, sha, "cached")
            continue

        raw: Optional[Dict[str, Any]] = None
        method = ""
        fail_msg = ""                   # why this file ended as "failed" (shown in the app)
        if use_claude:
            try:
                raw, method = claude.extract(pdf), "claude"
            except ClaudeUnavailable as e:
                log("  Claude ใช้ไม่ได้ในรอบนี้: %s" % e)
                issues.append("Claude ใช้ไม่ได้: %s (ใช้ pdftotext แทน)" % e)
                fail_msg = "Claude ใช้ไม่ได้: %s" % e
                use_claude = False
            except ExtractionError as e:
                log("  %s: %s" % (rel, e))
                fail_msg = str(e)
                if cfg.extractor == "claude":
                    issues.append("%s: %s" % (rel, e))
                else:
                    claude_errors.append("%s: %s" % (rel, e))
        # Never replace an earlier Claude result with the weaker pdftotext one.
        if raw is None and cfg.extractor != "claude" and not (hit and hit["method"] == "claude"):
            try:
                raw, method = extract_text(pdf), "text"
            except ExtractionError as e:
                fail_msg = "อ่านไม่สำเร็จ (%s)" % e
                issues.append("%s: อ่านไม่สำเร็จ (%s)" % (rel, e))
        if raw is None:
            if hit:   # keep the older (text) result rather than dropping the file
                entries.append(Entry(rel, sha, hit["record"], hit["method"], hit["first_seen"]))
            stats["failed"] += 1
            done(rel, sha, "failed", fail_msg or "อ่านไม่สำเร็จ")
            continue

        rec = normalize_record(raw)
        first_seen = hit["first_seen"] if hit else today_iso
        attempts = (hit.get("attempts", 1) + 1) if (hit and hit["method"] == "claude") else 1
        cache.put(sha, rec, method, first_seen, claude.model_label if method == "claude" else "",
                  warned=bool(validate_record(rec)), attempts=attempts)
        stats[method] += 1
        log("  อ่านแล้ว (%s): %s" % (method, rel))
        entries.append(Entry(rel, sha, rec, method, first_seen))
        done(rel, sha, method)

    cache.save()
    if claude_errors:   # auto mode falls back to pdftotext per file; still tell the owner why
        issues.append("Claude อ่านไม่สำเร็จ %d ไฟล์ (ใช้ pdftotext แทน) เช่น %s" % (len(claude_errors), claude_errors[0]))

    for e in entries:
        e.company = match_company(e.record, cfg.companies)
        e.warnings = validate_record(e.record)
        if e.company is None:
            e.warnings.append("ผู้ขาย '%s' ไม่ตรงกับบริษัทใน config" % (e.record.get("seller_name") or "ไม่พบ"))

    kept, dropped = dedupe(entries)
    for e, why in dropped:
        issues.append("%s: %s" % (e.file_name, why))
    for e in kept:
        if e.warnings:
            issues.append("%s: %s" % (e.file_name, "; ".join(e.warnings)))
    n_text = sum(1 for e in kept if e.method == "text")
    if n_text:
        if claude_at_start:   # Claude was set up, so "set up Claude" would be the wrong advice
            issues.append("%d ไฟล์อ่านด้วย pdftotext เพราะ Claude อ่านไม่สำเร็จ/ใช้ไม่ได้ (ดูข้อความข้างบน) ควรเช็กเทียบกับ PDF; รอบหน้าจะลองใหม่" % n_text)
        else:
            issues.append("%d ไฟล์อ่านด้วย pdftotext (ไม่ได้ใช้ Claude) ควรเช็กเทียบกับ PDF; จะอ่านใหม่ด้วย Claude อัตโนมัติเมื่อตั้งค่า Claude ได้ (token ของแพ็กเกจรายเดือน หรือ API key)" % n_text)
    return kept, issues, len(pdfs), stats


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qsum", description="Weekly Excel summary of won quotations (Thai PDFs).")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--folder", help="override source_folder")
    ap.add_argument("--offline", action="store_true", help="do not call Claude; use pdftotext only")
    ap.add_argument("--reextract", action="store_true", help="ignore the cache and re-read every PDF")
    ap.add_argument("--print-output-path", action="store_true", help="print the output .xlsx path and exit")
    ap.add_argument("--print-keychain-service", action="store_true", help="print the Keychain service name and exit")
    ap.add_argument("--print-claude-token-service", action="store_true",
                    help="print the Keychain service name of the Claude Code token and exit")
    ap.add_argument("--print-claude-config-dir", action="store_true",
                    help="print the Claude Code config folder (claude_code_config_dir) and exit")
    ap.add_argument("--version", action="version", version="qsum " + __version__)
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config, args.folder)
    except ConfigError as e:
        print("ผิดพลาด: %s" % e, file=sys.stderr)
        return 1
    if args.print_output_path:
        print(cfg.output_file)
        return 0
    if args.print_keychain_service:
        print(cfg.keychain_service)
        return 0
    if args.print_claude_token_service:
        print(cfg.claude_code_keychain_service)
        return 0

    if args.print_claude_config_dir:
        print(os.path.expanduser(cfg.claude_code_config_dir))
        return 0

    # ---- a real run starts here: take the run lock (one run at a time), then keep state.json up to date.
    lock = RunLock(cfg.lock_file)
    try:
        got_lock = lock.acquire()
    except OSError as e:
        # Cannot create or lock the lock file (unwritable cache folder, or a filesystem without flock support such as
        # some network drives). Do not block the run on it; the only loss is the protection against two runs at once.
        print("คำเตือน: ใช้ไฟล์ล็อก %s ไม่ได้ (%s) รันต่อโดยไม่มีตัวกันรันซ้อน" % (cfg.lock_file, e.strerror or e),
              file=sys.stderr)
        got_lock = True
    if not got_lock:
        print("กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา) จึงไม่เริ่มรอบใหม่ รอให้รอบนั้นเสร็จก่อน", file=sys.stderr)
        return 3
    recorder = RunRecorder(cfg.state_file, trigger_from_env())
    recorder.start()
    try:
        return _run(cfg, args, recorder)
    except KeyboardInterrupt:      # Ctrl-C in Terminal: the app shows `fatal` as-is, so give it a Thai message
        recorder.finish(1, fatal="ถูกหยุดกลางทาง (กด Ctrl-C)")
        raise
    except BaseException as e:     # a crash must still leave a last_run (exit_code 1 + fatal), then propagate
        recorder.finish(1, fatal="%s: %s" % (type(e).__name__, e))
        raise
    finally:
        lock.release()


def _run(cfg: Config, args: argparse.Namespace, recorder: RunRecorder) -> int:
    """The real run (lock held). Every return path calls recorder.finish(...) exactly once."""

    def fatal(msg: str, n_pdf: int = 0, stats: Optional[Dict[str, int]] = None,
              issues: Optional[List[str]] = None) -> int:
        print("ผิดพลาด: %s" % msg, file=sys.stderr)
        recorder.finish(1, fatal=msg, n_pdf=n_pdf, stats=stats, issues=issues, output_file=cfg.output_file)
        return 1

    print("[%s] qsum %s | โฟลเดอร์: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), __version__, cfg.source_folder))
    # Path.is_dir() swallows only ENOENT-like errors on Python 3.9-3.12; EPERM/EACCES (macOS privacy
    # block) would raise, so catch OSError and report it with the same Thai hint as list_pdfs.
    try:
        folder_ok = cfg.source_folder.is_dir()
    except OSError as e:
        return fatal(unreadable_message(cfg.source_folder, e))
    if not folder_ok:
        return fatal("ไม่พบโฟลเดอร์ %s (แก้ source_folder ใน %s)" % (cfg.source_folder, cfg.config_path))

    claude: Optional[Any] = None
    startup_issue: Optional[str] = None
    if cfg.extractor != "text" and not args.offline:
        try:
            if cfg.claude_backend == "claude_code":
                claude = ClaudeCodeExtractor(
                    cfg.claude_model, get_oauth_token(cfg.claude_code_keychain_service) or "",
                    resolve_claude_path(cfg.claude_code_path),
                    timeout_sec=cfg.claude_code_timeout_sec, max_files=cfg.claude_code_max_files_per_run,
                    config_dir=cfg.claude_code_config_dir or None, max_pdf_mb=cfg.max_pdf_mb)
                print("ตัวอ่าน: Claude Code (แพ็กเกจรายเดือน, รุ่น %s)" % (cfg.claude_model or "ค่าตั้งต้น"))
            else:
                claude = ClaudeExtractor(cfg.claude_model, get_api_key(cfg.keychain_service), max_pdf_mb=cfg.max_pdf_mb)
                print("ตัวอ่าน: Claude (%s)" % cfg.claude_model)
        except ClaudeUnavailable as e:
            if cfg.extractor == "claude":
                return fatal(str(e))
            startup_issue = str(e)    # auto mode: carried into the summary sheet by process()
            print("ตัวอ่าน: pdftotext (เพราะ %s)" % e)
    else:
        print("ตัวอ่าน: pdftotext (ปิด Claude)")

    try:
        entries, issues, n_pdf, stats = process(cfg, claude, reextract=args.reextract, startup_issue=startup_issue,
                                                reporter=recorder)
    except SourceUnreadable as e:
        return fatal(str(e))
    try:
        sheets = write_workbook(cfg.output_file, cfg.companies, entries, issues, n_pdf)
    except OSError as e:
        return fatal("เขียนไฟล์ Excel ไม่ได้ (%s): %s ปิดไฟล์ใน Excel หรือเปลี่ยน output_file ใน config.json"
                     % (cfg.output_file, e.strerror or e), n_pdf=n_pdf, stats=stats, issues=issues)

    print("PDF %d ไฟล์ | ใช้ cache %d | อ่านด้วย Claude %d | pdftotext %d | อ่านไม่ได้ %d" % (
        n_pdf, stats["cached"], stats["claude"], stats["text"], stats["failed"]))
    print("ชีต: %s" % ", ".join(sheets))
    print("ไฟล์ Excel: %s" % cfg.output_file)
    if issues:
        print("ต้องตรวจสอบ %d รายการ (ดูชีต 'สรุป' ด้านล่างสุด):" % len(issues))
        for m in issues[:20]:
            print("  - %s" % m)
    rc = 2 if stats["failed"] else 0
    recorder.finish(rc, n_pdf=n_pdf, stats=stats, issues=issues, output_file=cfg.output_file, excel_written=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
