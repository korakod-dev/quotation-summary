"""Tests for qsum/status.py (read-only JSON status for the desktop app) and scripts/status.sh.

Everything lives in temp folders. The launchd plist folder comes from QS_LAUNCH_AGENTS_DIR (a temp folder);
the cache / state / lock files are temp files. No Keychain, no Claude, no launchctl, no real ~/Library.
Run:  .venv/bin/python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from qsum import records, state as st
from qsum import status as S
from qsum.cache import MAX_CLAUDE_ATTEMPTS, sha256_file
from qsum.config import Company, Config

PROJECT_DIR = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=7))
NOW = datetime(2026, 11, 10, 9, 30, 0, tzinfo=TZ)          # a Tuesday

COMPANY_A = Company("Company A", "บริษัท ตัวอย่าง เอ จำกัด", "0100000000001", ["ตัวอย่าง เอ"])
COMPANY_B = Company("Company B", "บริษัท ตัวอย่าง บี จำกัด", "0100000000002", ["ตัวอย่าง บี"])


def good_record(quote_no="QT0000000001", seller="บริษัท ตัวอย่าง เอ จำกัด", tax="0100000000001", total=1070.0):
    return records.normalize_record(dict(
        quote_no=quote_no, quote_date="2026-11-03", seller_name=seller, seller_tax_id=tax,
        customer_name="บริษัท ลูกค้า จำกัด",
        items=[dict(description="x", quantity=1, unit="PCS.", unit_price=1000.0, amount=1000.0)],
        subtotal=1000.0, vat=70.0, total=total))


def cache_entry(rec, method="claude", first_seen="2026-11-03", warned=False, attempts=1):
    return {"record": rec, "method": method, "first_seen": first_seen, "model": "m", "warned": warned,
            "attempts": attempts}


class Env:
    """A temp world: config, source folder, cache entries, state, plist folder."""

    def __init__(self, tmp: Path, extractor="auto"):
        self.tmp = tmp
        self.src = tmp / "src"
        self.src.mkdir()
        self.cfg = Config(source_folder=self.src, output_file=tmp / "out.xlsx", cache_file=tmp / "cache" / "c.json",
                          extractor=extractor, companies=[COMPANY_A, COMPANY_B])
        self.cache = {}
        self.state = st.empty_state()

    def pdf(self, name, content, entry=None):
        p = self.src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if entry is not None:
            self.cache[sha256_file(p)] = entry
        return p

    def status(self, **kw):
        kw.setdefault("running", False)
        return S.build_status(self.cfg, self.cache, self.state, None, NOW, **kw)

    def rows(self, **kw):
        return {r["name"]: r for r in self.status(**kw)["files"]}


class NextRunTests(unittest.TestCase):
    def test_monday_0800(self):
        # Tuesday 2026-11-10 09:30 -> next Monday 2026-11-16 08:00
        self.assertEqual(S.next_run(1, 8, 0, NOW), datetime(2026, 11, 16, 8, 0, tzinfo=TZ))

    def test_same_day_later_and_strictly_after(self):
        monday_early = datetime(2026, 11, 9, 7, 59, 59, tzinfo=TZ)
        self.assertEqual(S.next_run(1, 8, 0, monday_early), datetime(2026, 11, 9, 8, 0, tzinfo=TZ))
        exactly = datetime(2026, 11, 9, 8, 0, 0, tzinfo=TZ)
        self.assertEqual(S.next_run(1, 8, 0, exactly), datetime(2026, 11, 16, 8, 0, tzinfo=TZ))   # not "now" itself
        just_after = exactly + timedelta(seconds=1)
        self.assertEqual(S.next_run(1, 8, 0, just_after), datetime(2026, 11, 16, 8, 0, tzinfo=TZ))

    def test_sunday_is_0_and_7_and_saturday_is_6(self):
        sat = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)
        self.assertEqual(sat.weekday(), 5)
        self.assertEqual(S.next_run(0, 8, 0, sat), datetime(2026, 11, 15, 8, 0, tzinfo=TZ))
        self.assertEqual(S.next_run(7, 8, 0, sat), datetime(2026, 11, 15, 8, 0, tzinfo=TZ))
        self.assertEqual(S.next_run(6, 13, 0, sat), datetime(2026, 11, 14, 13, 0, tzinfo=TZ))
        self.assertEqual(S.next_run(6, 11, 0, sat), datetime(2026, 11, 21, 11, 0, tzinfo=TZ))

    def test_every_weekday_number(self):
        names = {}
        for wd in range(0, 8):
            names[wd] = S.next_run(wd, 0, 0, NOW).strftime("%A")
        self.assertEqual(names, {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
                                 5: "Friday", 6: "Saturday", 7: "Sunday"})
        self.assertEqual(S.next_run(2, 9, 30, NOW), datetime(2026, 11, 17, 9, 30, tzinfo=TZ))   # equal to now -> next week

    def test_month_and_year_rollover_and_daily(self):
        dec31 = datetime(2026, 12, 31, 23, 0, tzinfo=TZ)                      # Thursday
        self.assertEqual(S.next_run(1, 8, 0, dec31), datetime(2027, 1, 4, 8, 0, tzinfo=TZ))
        self.assertEqual(S.next_run(None, 8, 0, dec31), datetime(2027, 1, 1, 8, 0, tzinfo=TZ))

    def test_naive_now_stays_naive(self):
        self.assertEqual(S.next_run(1, 8, 0, datetime(2026, 11, 10, 9, 30)), datetime(2026, 11, 16, 8, 0))


class ScheduleTests(unittest.TestCase):
    def test_not_installed_is_all_null(self):
        self.assertEqual(S._schedule(None, NOW), {"installed": False, "label": None, "weekday": None,
                                                  "hour": None, "minute": None, "next_run": None})

    def test_installed_plist(self):
        plist = {"Label": S.LAUNCHD_LABEL, "StartCalendarInterval": {"Weekday": 1, "Hour": 8, "Minute": 0}}
        self.assertEqual(S._schedule(plist, NOW), {"installed": True, "label": S.LAUNCHD_LABEL, "weekday": 1,
                                                   "hour": 8, "minute": 0, "next_run": "2026-11-16T08:00:00+07:00"})

    def test_list_of_intervals_picks_the_earliest_next_run(self):
        plist = {"Label": "l", "StartCalendarInterval": [{"Weekday": 1, "Hour": 8, "Minute": 0},
                                                         {"Weekday": 3, "Hour": 8, "Minute": 0}]}
        self.assertEqual(S._schedule(plist, NOW)["next_run"], "2026-11-11T08:00:00+07:00")

    def test_plist_without_calendar_interval(self):
        out = S._schedule({"Label": "l"}, NOW)
        self.assertEqual((out["installed"], out["next_run"]), (True, None))

    def test_read_plist_from_env_folder(self):
        with tempfile.TemporaryDirectory() as t:
            agents = Path(t)
            self.assertIsNone(S.read_plist(agents))                              # no file
            with open(str(agents / (S.LAUNCHD_LABEL + ".plist")), "wb") as f:
                plistlib.dump({"Label": S.LAUNCHD_LABEL, "StartCalendarInterval": {"Weekday": 0, "Hour": 7, "Minute": 5}}, f)
            self.assertEqual(S.read_plist(agents)["StartCalendarInterval"]["Weekday"], 0)
            with mock.patch.dict(os.environ, {"QS_LAUNCH_AGENTS_DIR": t}):
                self.assertEqual(S.read_plist()["Label"], S.LAUNCHD_LABEL)       # default folder comes from the env hook
            (agents / (S.LAUNCHD_LABEL + ".plist")).write_bytes(b"not a plist")
            self.assertIsNone(S.read_plist(agents))                              # corrupt = not installed


class FileStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = Env(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_done_claude_clean(self):
        self.env.pdf("a.pdf", b"A", cache_entry(good_record()))
        r = self.env.rows()["a.pdf"]
        self.assertEqual((r["status"], r["method"], r["will_retry"], r["detail"]), ("done", "claude", False, ""))
        self.assertEqual((r["quote_no"], r["company"], r["total"], r["size"]), ("QT0000000001", "Company A", 1070.0, 1))
        self.assertEqual(r["sha256"], sha256_file(self.env.src / "a.pdf"))
        self.assertEqual(r["added"], "2026-11-03")                              # first_seen from the cache

    def test_text_method_is_check_and_retried_unless_extractor_is_text(self):
        self.env.pdf("t.pdf", b"T", cache_entry(good_record(), method="text"))
        r = self.env.rows()["t.pdf"]
        self.assertEqual((r["status"], r["method"], r["will_retry"]), ("check", "text", True))
        self.assertIn("pdftotext", r["detail"])
        self.env.cfg.extractor = "text"
        r = self.env.rows()["t.pdf"]
        self.assertEqual((r["status"], r["will_retry"]), ("check", False))

    def test_validation_warnings_make_it_check(self):
        bad = good_record(total=9999.0)                                          # does not add up
        self.env.pdf("w.pdf", b"W", cache_entry(bad))
        r = self.env.rows()["w.pdf"]
        self.assertEqual(r["status"], "check")
        self.assertIn("ยอดไม่ลงตัว", r["detail"])
        self.assertEqual(r["method"], "claude")

    def test_warned_claude_result_is_marked_will_retry_until_attempts_run_out(self):
        bad = good_record(total=9999.0)
        self.env.pdf("w.pdf", b"W", cache_entry(bad, warned=True, attempts=1))
        self.assertTrue(self.env.rows()["w.pdf"]["will_retry"])
        self.env.pdf("w.pdf", b"W", cache_entry(bad, warned=True, attempts=MAX_CLAUDE_ATTEMPTS))
        self.assertFalse(self.env.rows()["w.pdf"]["will_retry"])

    def test_unmatched_company_is_check_with_reason(self):
        other = good_record(seller="บริษัท อื่น จำกัด", tax="1111111111111")
        self.env.pdf("o.pdf", b"O", cache_entry(other))
        r = self.env.rows()["o.pdf"]
        self.assertEqual((r["status"], r["company"]), ("check", None))
        self.assertIn("ไม่ตรงกับบริษัทใน config", r["detail"])

    def test_older_rev_is_dropped_and_check_newer_stays_done(self):
        old = good_record(quote_no="QT0000000009")
        new = good_record(quote_no="QT0000000009 Rev.1")
        self.env.pdf("old.pdf", b"old", cache_entry(old))
        self.env.pdf("new.pdf", b"new", cache_entry(new))
        rows = self.env.rows()
        self.assertEqual(rows["new.pdf"]["status"], "done")
        self.assertEqual(rows["old.pdf"]["status"], "check")
        self.assertIn("มีฉบับแก้ไขใหม่กว่า", rows["old.pdf"]["detail"])

    def test_exact_duplicate_is_check_for_the_loser_only(self):
        rec = good_record(quote_no="QT0000000010")
        self.env.pdf("a.pdf", b"copy-1", cache_entry(rec))
        self.env.pdf("b.pdf", b"copy-2", cache_entry(rec))
        rows = self.env.rows()
        self.assertEqual((rows["a.pdf"]["status"], rows["b.pdf"]["status"]), ("done", "check"))
        self.assertIn("ซ้ำกับไฟล์ a.pdf", rows["b.pdf"]["detail"])

    def test_failed_in_state_without_cache_is_error_with_message(self):
        p = self.env.pdf("f.pdf", b"F")
        self.env.state["files"][sha256_file(p)] = {"rel": "f.pdf", "outcome": "failed", "message": "Claude หมดเวลา", "at": "x"}
        r = self.env.rows()["f.pdf"]
        self.assertEqual((r["status"], r["detail"], r["will_retry"], r["method"]), ("error", "Claude หมดเวลา", True, None))

    def test_state_with_other_outcome_but_no_cache_is_waiting(self):
        p = self.env.pdf("n.pdf", b"N")
        self.env.state["files"][sha256_file(p)] = {"rel": "n.pdf", "outcome": "claude", "message": "", "at": "x"}
        self.assertEqual(self.env.rows()["n.pdf"]["status"], "waiting")

    def test_failed_in_state_but_cache_hit_is_not_error(self):
        p = self.env.pdf("c.pdf", b"C", cache_entry(good_record(), method="text"))
        self.env.state["files"][sha256_file(p)] = {"rel": "c.pdf", "outcome": "failed", "message": "x", "at": "x"}
        self.assertEqual(self.env.rows()["c.pdf"]["status"], "check")             # the older text result is kept

    def test_waiting_text(self):
        self.env.pdf("new.pdf", b"brand new")
        r = self.env.rows()["new.pdf"]
        self.assertEqual((r["status"], r["detail"], r["sha256"] is not None), ("waiting", "ยังไม่ได้อ่าน รอรอบถัดไป", True))
        self.assertRegex(r["added"], r"^\d{4}-\d{2}-\d{2}$")                      # file birth/mtime date

    def test_hash_oserror_is_error(self):
        self.env.pdf("ok.pdf", b"ok", cache_entry(good_record()))
        self.env.pdf("bad.pdf", b"bad")
        real = S.sha256_file

        def flaky(path):
            if path.name == "bad.pdf":
                raise PermissionError(1, "Operation not permitted")
            return real(path)
        with mock.patch.object(S, "sha256_file", side_effect=flaky):
            rows = self.env.rows()
        self.assertEqual((rows["bad.pdf"]["status"], rows["bad.pdf"]["sha256"]), ("error", None))
        self.assertIn("อ่านไฟล์ไม่ได้", rows["bad.pdf"]["detail"])
        self.assertEqual(rows["ok.pdf"]["status"], "done")

    def test_order_and_counts(self):
        self.env.pdf("d-done.pdf", b"1", cache_entry(good_record("QT1")))
        self.env.pdf("C-check.pdf", b"2", cache_entry(good_record("QT2"), method="text"))
        self.env.pdf("b-wait.pdf", b"3")
        self.env.pdf("A-wait.pdf", b"4")
        p = self.env.pdf("z-err.pdf", b"5")
        self.env.state["files"][sha256_file(p)] = {"rel": "z-err.pdf", "outcome": "failed", "message": "m", "at": "x"}
        self.env.pdf("e-done.pdf", b"6", cache_entry(good_record("QT3")))
        doc = self.env.status()
        self.assertEqual([r["name"] for r in doc["files"]],
                         ["z-err.pdf", "A-wait.pdf", "b-wait.pdf", "C-check.pdf", "d-done.pdf", "e-done.pdf"])
        self.assertEqual(doc["counts"], {"total": 6, "done": 2, "check": 1, "waiting": 2, "error": 1})

    def test_subfolders_hidden_and_office_lock_files(self):
        self.env.pdf("sub/inner.pdf", b"I")
        self.env.pdf(".hidden.pdf", b"H")
        self.env.pdf("~$lock.pdf", b"L")
        self.env.pdf("notes.txt", b"T")
        self.assertEqual(list(self.env.rows()), [os.path.join("sub", "inner.pdf")])

    def test_empty_folder(self):
        doc = self.env.status()
        self.assertEqual((doc["ok"], doc["files"], doc["counts"]["total"]), (True, [], 0))


class TopLevelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = Env(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    EXPECTED_KEYS = {"schema", "engine_version", "generated_at", "ok", "error_code", "error", "source_folder",
                     "output_file", "output_exists", "output_mtime", "running", "progress", "last_run", "schedule",
                     "counts", "files"}

    def test_idle_document_shape(self):
        doc = self.env.status()
        self.assertEqual(set(doc), self.EXPECTED_KEYS)
        self.assertEqual((doc["schema"], doc["ok"], doc["error_code"], doc["error"]), (1, True, None, None))
        self.assertEqual((doc["running"], doc["progress"], doc["last_run"]), (False, None, None))
        self.assertEqual(doc["generated_at"], "2026-11-10T09:30:00+07:00")
        self.assertEqual((doc["output_exists"], doc["output_mtime"]), (False, None))
        self.assertFalse(doc["schedule"]["installed"])
        self.assertEqual(doc["source_folder"], str(self.env.src))
        json.dumps(doc, ensure_ascii=False)                                         # serialisable

    def test_output_file_info(self):
        self.env.cfg.output_file.write_bytes(b"x")
        doc = self.env.status()
        self.assertTrue(doc["output_exists"])
        self.assertRegex(doc["output_mtime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_running_with_progress(self):
        self.env.state["current"] = {"started_at": "2026-11-10T09:29:00+07:00", "trigger": "app", "pid": 1,
                                     "total": 10, "done": 2, "current_file": "x.pdf"}
        self.env.state["last_run"] = {"started_at": "a", "finished_at": "b", "trigger": "schedule", "exit_code": 0,
                                      "fatal": None, "n_pdf": 3, "stats": {"cached": 3, "claude": 0, "text": 0, "failed": 0},
                                      "issues": ["i"], "output_file": None, "excel_written": True}
        doc = self.env.status(running=True)
        self.assertTrue(doc["running"])
        self.assertEqual(doc["progress"], {"total": 10, "done": 2, "current_file": "x.pdf",
                                           "started_at": "2026-11-10T09:29:00+07:00", "trigger": "app"})
        self.assertFalse(doc["last_run"]["interrupted"])                            # previous run, not the one going now
        self.assertEqual(doc["last_run"]["trigger"], "schedule")

    def test_current_with_free_lock_is_an_interrupted_run(self):
        self.env.state["current"] = {"started_at": "2026-11-10T09:00:00+07:00", "trigger": "schedule", "pid": 99999,
                                     "total": 5, "done": 1, "current_file": "x.pdf"}
        doc = self.env.status(running=False)
        self.assertFalse(doc["running"])
        self.assertIsNone(doc["progress"])
        self.assertEqual(doc["last_run"], {"started_at": "2026-11-10T09:00:00+07:00", "finished_at": None,
                                           "trigger": "schedule", "exit_code": None, "interrupted": True,
                                           "stats": None, "issues": []})

    def test_running_is_probed_from_the_real_lock_file(self):
        holder = st.RunLock(self.env.cfg.lock_file)
        self.assertFalse(self.env.status(running=None)["running"])
        self.assertTrue(holder.acquire())
        try:
            self.assertTrue(self.env.status(running=None)["running"])               # a real flock held in this process
        finally:
            holder.release()
        self.assertFalse(self.env.status(running=None)["running"])

    def test_last_run_fatal_is_first_issue(self):
        self.env.state["last_run"] = {"started_at": "a", "finished_at": "b", "trigger": "manual", "exit_code": 1,
                                      "fatal": "Claude ใช้ไม่ได้", "n_pdf": 0,
                                      "stats": {"cached": 0, "claude": 0, "text": 0, "failed": 0}, "issues": ["อื่น"],
                                      "output_file": None, "excel_written": False}
        lr = self.env.status()["last_run"]
        self.assertEqual((lr["exit_code"], lr["interrupted"], lr["issues"]), (1, False, ["Claude ใช้ไม่ได้", "อื่น"]))
        self.assertEqual(set(lr), {"started_at", "finished_at", "trigger", "exit_code", "interrupted", "stats", "issues"})

    def test_source_missing(self):
        self.env.cfg.source_folder = self.env.tmp / "nope"
        doc = self.env.status()
        self.assertEqual((doc["ok"], doc["error_code"], doc["files"]), (False, "source_missing", []))
        self.assertEqual(set(doc), self.EXPECTED_KEYS)                              # all fields present
        self.assertIn("ไม่พบโฟลเดอร์ PDF", doc["error"])

    def test_source_unreadable_from_listing(self):
        with mock.patch.object(S, "list_pdfs", side_effect=S.SourceUnreadable("อ่านโฟลเดอร์ x ไม่ได้")):
            doc = self.env.status()
        self.assertEqual((doc["ok"], doc["error_code"], doc["files"], doc["error"]),
                         (False, "source_unreadable", [], "อ่านโฟลเดอร์ x ไม่ได้"))
        self.assertEqual(set(doc), self.EXPECTED_KEYS)

    def test_source_unreadable_from_is_dir(self):
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError(1, "Operation not permitted")):
            doc = self.env.status()
        self.assertEqual((doc["ok"], doc["error_code"]), (False, "source_unreadable"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_real_unreadable_folder(self):
        os.chmod(self.env.src, 0)
        try:
            doc = self.env.status()
        finally:
            os.chmod(self.env.src, 0o755)
        self.assertEqual((doc["ok"], doc["error_code"]), (False, "source_unreadable"))

    def test_never_calls_external_programs(self):
        self.env.pdf("a.pdf", b"A", cache_entry(good_record()))
        boom = AssertionError("status must not start a process")
        with mock.patch("subprocess.Popen", side_effect=boom), mock.patch("os.system", side_effect=boom), \
                mock.patch("os.execv", side_effect=boom), mock.patch("os.posix_spawn", side_effect=boom):
            self.assertEqual(self.env.status()["counts"]["done"], 1)


class CollectAndCliTests(unittest.TestCase):
    """collect() / main() read config + cache + state + plist from disk. All paths point into a temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "src").mkdir()
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({
            "source_folder": str(self.tmp / "src"), "output_file": str(self.tmp / "out.xlsx"),
            "cache_file": str(self.tmp / "app" / "cache.json"), "extractor": "auto",
            "companies": [{"sheet_name": "Company A", "tax_id": "0100000000001"}]}), encoding="utf-8")
        self.agents = self.tmp / "agents"
        self.agents.mkdir()
        # Point every default lookup into the temp folder, never at the real ~/Library.
        self._env = mock.patch.dict(os.environ, {"QS_LAUNCH_AGENTS_DIR": str(self.agents), "HOME": str(self.tmp)})
        self._env.start()
        os.environ.pop("QS_CONFIG", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def run_main(self, *argv):
        out = io.BytesIO()
        rc = S.main(list(argv), out=out)
        return rc, out.getvalue()

    def test_run_finishing_between_state_read_and_lock_probe_is_not_reported_as_interrupted(self):
        # state.json was read while the run was going; by the time the lock is probed the run has finished.
        current = {"started_at": "2026-11-10T09:00:00+07:00", "trigger": "app", "pid": 1, "total": 1, "done": 0,
                   "current_file": None}
        last = {"started_at": "2026-11-10T09:00:00+07:00", "finished_at": "2026-11-10T09:00:05+07:00",
                "trigger": "app", "exit_code": 0, "fatal": None, "n_pdf": 0,
                "stats": {"cached": 0, "claude": 0, "text": 0, "failed": 0}, "issues": [], "output_file": None,
                "excel_written": False}
        before = dict(st.empty_state(), current=current)            # what the first read sees
        after = dict(st.empty_state(), last_run=last)                # the run finished meanwhile
        with mock.patch("qsum.status.read_state", side_effect=[before, after]) as rs, \
                mock.patch("qsum.status.is_running", return_value=False):
            doc = S.collect(str(self.cfg_path))
        self.assertEqual(rs.call_count, 2)
        self.assertFalse(doc["last_run"]["interrupted"])
        self.assertEqual(doc["last_run"]["exit_code"], 0)

    def test_a_real_crash_is_still_reported_as_interrupted(self):
        current = {"started_at": "2026-11-10T09:00:00+07:00", "trigger": "app", "pid": 1, "total": 1, "done": 0,
                   "current_file": None}
        crashed = dict(st.empty_state(), current=current)
        with mock.patch("qsum.status.read_state", return_value=crashed), \
                mock.patch("qsum.status.is_running", return_value=False):
            doc = S.collect(str(self.cfg_path))
        self.assertTrue(doc["last_run"]["interrupted"])

    def test_main_prints_utf8_json_and_exit_0(self):
        (self.tmp / "src" / "ใบเสนอราคา.pdf").write_bytes(b"x")
        rc, raw = self.run_main("--json", "--config", str(self.cfg_path))
        self.assertEqual(rc, 0)
        text = raw.decode("utf-8")
        self.assertIn("ใบเสนอราคา.pdf", text)                                       # not \u-escaped
        self.assertNotIn("\\u0e", text)
        doc = json.loads(text)
        self.assertEqual((doc["ok"], doc["counts"]["waiting"]), (True, 1))
        self.assertEqual(doc["engine_version"], __import__("qsum").__version__)

    def test_main_reads_cache_state_and_plist(self):
        pdf = self.tmp / "src" / "a.pdf"
        pdf.write_bytes(b"A")
        sha = sha256_file(pdf)
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "cache.json").write_text(json.dumps(
            {"schema": 1, "entries": {sha: cache_entry(good_record())}}), encoding="utf-8")
        st.write_state(self.tmp / "app" / "state.json", {
            "schema": 1, "current": None, "files": {},
            "last_run": {"started_at": "s", "finished_at": "f", "trigger": "schedule", "exit_code": 0, "fatal": None,
                         "n_pdf": 1, "stats": {"cached": 0, "claude": 1, "text": 0, "failed": 0}, "issues": [],
                         "output_file": None, "excel_written": True}})
        with open(str(self.agents / (S.LAUNCHD_LABEL + ".plist")), "wb") as f:
            plistlib.dump({"Label": S.LAUNCHD_LABEL, "StartCalendarInterval": {"Weekday": 1, "Hour": 8, "Minute": 0}}, f)
        rc, raw = self.run_main("--config", str(self.cfg_path))
        doc = json.loads(raw)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["files"][0]["status"], "done")
        self.assertEqual(doc["last_run"]["trigger"], "schedule")
        self.assertTrue(doc["schedule"]["installed"])
        self.assertEqual((doc["schedule"]["weekday"], doc["schedule"]["hour"]), (1, 8))
        self.assertRegex(doc["schedule"]["next_run"], r"^\d{4}-\d{2}-\d{2}T08:00:00[+-]\d{2}:\d{2}$")

    def test_main_is_strictly_read_only(self):
        (self.tmp / "src" / "a.pdf").write_bytes(b"A")

        def snapshot():
            return sorted((str(p.relative_to(self.tmp)), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in self.tmp.rglob("*"))
        before = snapshot()
        rc, _ = self.run_main("--config", str(self.cfg_path))
        self.assertEqual(rc, 0)
        self.assertEqual(snapshot(), before)                  # no state.json, no run.lock, no app/ folder created
        self.assertFalse((self.tmp / "app").exists())

    def test_config_error_gives_minimal_document_and_exit_1(self):
        self.cfg_path.write_text("{oops", encoding="utf-8")
        rc, raw = self.run_main("--config", str(self.cfg_path))
        doc = json.loads(raw)
        self.assertEqual(rc, 1)
        self.assertEqual(set(doc), {"schema", "ok", "error_code", "error", "generated_at"})
        self.assertEqual((doc["ok"], doc["error_code"]), (False, "config"))
        self.assertIn("JSON ผิดรูปแบบ", doc["error"])

    def test_missing_config_file(self):
        rc, raw = self.run_main("--config", str(self.tmp / "missing.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(raw)["error_code"], "config")

    def test_source_missing_exit_1_with_full_document(self):
        shutil.rmtree(self.tmp / "src")
        rc, raw = self.run_main("--config", str(self.cfg_path))
        doc = json.loads(raw)
        self.assertEqual((rc, doc["error_code"], doc["files"]), (1, "source_missing", []))
        self.assertIn("counts", doc)

    def test_unexpected_exception_still_yields_json(self):
        with mock.patch.object(S, "build_status", side_effect=RuntimeError("boom")):
            rc, raw = self.run_main("--config", str(self.cfg_path))
        doc = json.loads(raw)
        self.assertEqual((rc, doc["ok"], doc["error_code"]), (1, False, "config"))
        self.assertIn("boom", doc["error"])

    def test_module_entry_point_in_a_subprocess(self):
        (self.tmp / "src" / "a.pdf").write_bytes(b"A")
        env = dict(os.environ, PYTHONIOENCODING="ascii", LANG="C", LC_ALL="C")   # a hostile locale must not matter
        p = subprocess.run([sys.executable, "-m", "qsum.status", "--json", "--config", str(self.cfg_path)],
                           cwd=str(PROJECT_DIR), env=env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout.decode("utf-8"))["counts"]["total"], 1)


class ImportHygieneTests(unittest.TestCase):
    def test_status_does_not_import_openpyxl_or_anthropic(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import qsum.status\n"
                "bad = [m for m in ('openpyxl', 'anthropic', 'qsum.main', 'qsum.excel_writer', 'qsum.extract_claude',\n"
                "                  'qsum.extract_claude_code', 'qsum.extract_text') if m in sys.modules]\n"
                "print(bad)\n"
                "sys.exit(1 if bad else 0)\n") % str(PROJECT_DIR)
        p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class StatusShellTests(unittest.TestCase):
    """scripts/status.sh: without a .venv it prints the not_installed JSON and exits 1."""

    def test_not_installed_without_venv(self):
        with tempfile.TemporaryDirectory() as t:
            app = Path(t) / "app"
            (app / "scripts").mkdir(parents=True)
            shutil.copy(str(PROJECT_DIR / "scripts" / "status.sh"), str(app / "scripts" / "status.sh"))
            p = subprocess.run(["/bin/bash", str(app / "scripts" / "status.sh")], capture_output=True)
        self.assertEqual(p.returncode, 1)
        doc = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(set(doc), {"schema", "ok", "error_code", "error", "generated_at"})
        self.assertEqual((doc["schema"], doc["ok"], doc["error_code"]), (1, False, "not_installed"))
        self.assertIn("ยังไม่ได้ติดตั้ง", doc["error"])
        self.assertRegex(doc["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_script_is_executable(self):
        self.assertTrue(os.access(str(PROJECT_DIR / "scripts" / "status.sh"), os.X_OK))

    def test_passes_arguments_to_the_module_with_the_venv_python(self):
        # A fake .venv python that just echoes its arguments proves the exec line (module + --json + extra args).
        with tempfile.TemporaryDirectory() as t:
            app = Path(t) / "app"
            (app / "scripts").mkdir(parents=True)
            (app / ".venv" / "bin").mkdir(parents=True)
            shutil.copy(str(PROJECT_DIR / "scripts" / "status.sh"), str(app / "scripts" / "status.sh"))
            fake = app / ".venv" / "bin" / "python"
            fake.write_text('#!/bin/bash\necho "ARGS: $*"\n', encoding="utf-8")
            fake.chmod(0o755)
            p = subprocess.run(["/bin/bash", str(app / "scripts" / "status.sh"), "--config", "x.json"],
                               capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "ARGS: -m qsum.status --json --config x.json")


if __name__ == "__main__":
    unittest.main()
