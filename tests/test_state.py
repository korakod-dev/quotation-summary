"""Tests for qsum/state.py: atomic state.json, the run lock, is_running() and the RunRecorder.

Everything lives in temp folders; no Keychain, no Claude, no launchd.
Run:  .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from qsum import state as st

PROJECT_DIR = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=7))


def fake_clock():
    """A clock that moves one second per call, so timestamps are predictable."""
    t = {"n": 0}

    def clock():
        t["n"] += 1
        return datetime(2026, 11, 9, 8, 0, 0, tzinfo=TZ) + timedelta(seconds=t["n"])
    return clock


class StateFileTests(unittest.TestCase):
    def test_roundtrip_thai_and_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "sub" / "state.json"       # the folder does not exist yet
            data = st.empty_state()
            data["files"] = {"abc": {"rel": "ใบเสนอราคา.pdf", "outcome": "claude", "message": "", "at": "x"}}
            st.write_state(p, data)
            self.assertEqual(st.read_state(p), data)
            self.assertIn("ใบเสนอราคา.pdf", p.read_text(encoding="utf-8"))   # not \u-escaped
            self.assertEqual([x.name for x in p.parent.iterdir()], ["state.json"])

    def test_failed_replace_keeps_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            old = st.empty_state()
            old["last_run"] = {"exit_code": 0}
            st.write_state(p, old)
            with mock.patch("qsum.state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    st.write_state(p, st.empty_state())
            self.assertEqual(st.read_state(p)["last_run"], {"exit_code": 0})      # old content intact
            self.assertEqual([x.name for x in Path(t).iterdir()], ["state.json"])  # temp file removed

    def test_write_goes_through_a_temp_file_and_os_replace(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            real_replace = os.replace
            seen = []

            def spy(src, dst):
                seen.append((os.path.dirname(src), os.path.basename(src) != "state.json", str(dst)))
                return real_replace(src, dst)
            with mock.patch("qsum.state.os.replace", side_effect=spy):
                st.write_state(p, st.empty_state())
            self.assertEqual(seen, [(t, True, str(p))])      # same folder (atomic rename), different name

    def test_read_is_tolerant(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            self.assertEqual(st.read_state(p), st.empty_state())              # missing
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(st.read_state(p), st.empty_state())              # corrupt
            p.write_text(json.dumps({"schema": 99, "current": {"a": 1}}), encoding="utf-8")
            self.assertEqual(st.read_state(p), st.empty_state())              # other schema
            p.write_text(json.dumps([1, 2]), encoding="utf-8")
            self.assertEqual(st.read_state(p), st.empty_state())              # not an object
            p.write_text(json.dumps({"schema": 1, "current": "x", "last_run": 5, "files": {"a": 1, "b": {"rel": "r"}}}),
                         encoding="utf-8")
            self.assertEqual(st.read_state(p), {"schema": 1, "current": None, "last_run": None, "files": {"b": {"rel": "r"}}})

    def test_trigger_from_env(self):
        self.assertEqual(st.trigger_from_env({"QS_TRIGGER": "schedule"}), "schedule")
        self.assertEqual(st.trigger_from_env({"QS_TRIGGER": " App "}), "app")
        self.assertEqual(st.trigger_from_env({"QS_TRIGGER": "weird"}), "manual")
        self.assertEqual(st.trigger_from_env({}), "manual")

    def test_iso_has_offset_and_seconds_only(self):
        self.assertEqual(st.iso(datetime(2026, 11, 10, 8, 0, 0, 123456, tzinfo=TZ)), "2026-11-10T08:00:00+07:00")


class RunLockTests(unittest.TestCase):
    def test_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "deep" / "run.lock"
            a, b = st.RunLock(path), st.RunLock(path)
            self.assertTrue(a.acquire())                 # creates the folder
            self.assertFalse(b.acquire())                # busy
            self.assertTrue(st.is_running(path))
            a.release()
            self.assertFalse(st.is_running(path))
            self.assertTrue(b.acquire())
            b.release()
            b.release()                                  # releasing twice is harmless

    def test_acquire_retries_a_few_times_before_saying_busy(self):
        # a shared probe lock that clashes for an instant must not turn a real run into exit 3.
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "run.lock"
            holder = st.RunLock(path)
            self.assertTrue(holder.acquire())
            sleeps = []
            with mock.patch("qsum.state.time.sleep", side_effect=sleeps.append):
                self.assertFalse(st.RunLock(path).acquire())
            self.assertEqual(sleeps, [0.1, 0.1])                 # 3 attempts, 2 pauses, then "busy"
            holder.release()

    def test_acquire_succeeds_when_the_lock_frees_during_the_retries(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "run.lock"
            holder = st.RunLock(path)
            self.assertTrue(holder.acquire())
            # the first pause is when the "probe" lets go
            with mock.patch("qsum.state.time.sleep", side_effect=lambda s: holder.release()):
                late = st.RunLock(path)
                self.assertTrue(late.acquire())
            late.release()

    def test_acquire_raises_on_flock_errors_other_than_busy(self):
        # ENOLCK / ENOTSUP (network drive) is not "another run is going"; the caller warns and runs on.
        import errno
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "run.lock"
            with mock.patch("qsum.state.fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
                with self.assertRaises(OSError):
                    st.RunLock(path).acquire()
            self.assertTrue(st.RunLock(path).acquire())          # the fd was closed, nothing is stuck

    def test_is_running_never_creates_anything(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "nope" / "run.lock"
            self.assertFalse(st.is_running(path))
            self.assertEqual(list(Path(t).iterdir()), [])        # no folder, no file
            path.parent.mkdir()
            self.assertFalse(st.is_running(path))
            self.assertEqual(list(path.parent.iterdir()), [])    # still no lock file created by the probe

    def test_is_running_does_not_disturb_the_holder(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "run.lock"
            holder = st.RunLock(path)
            self.assertTrue(holder.acquire())
            for _ in range(3):
                self.assertTrue(st.is_running(path))
            self.assertFalse(st.RunLock(path).acquire())         # holder still owns it after the probes
            holder.release()

    def test_lock_is_held_by_another_process_and_dies_with_it(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "run.lock"
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from qsum.state import RunLock\n"
                    "l = RunLock(sys.argv[1]); assert l.acquire()\n"
                    "print('locked', flush=True); sys.stdin.read()\n") % str(PROJECT_DIR)
            proc = subprocess.Popen([sys.executable, "-I", "-c", code, str(path)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                self.assertTrue(st.is_running(path))
                self.assertFalse(st.RunLock(path).acquire())
            finally:
                proc.kill()          # SIGKILL: no cleanup code runs, the kernel must drop the lock
                proc.wait()
                proc.stdin.close()
                proc.stdout.close()
            self.assertFalse(st.is_running(path))
            again = st.RunLock(path)
            self.assertTrue(again.acquire())
            again.release()


class RunRecorderTests(unittest.TestCase):
    def test_full_run_sequence(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            rec = st.RunRecorder(p, "app", clock=fake_clock())
            rec.start()
            cur = st.read_state(p)["current"]
            self.assertEqual((cur["trigger"], cur["pid"], cur["total"], cur["done"], cur["current_file"]),
                             ("app", os.getpid(), 0, 0, None))
            rec.begin(2)
            rec.file_started("a.pdf")
            cur = st.read_state(p)["current"]
            self.assertEqual((cur["total"], cur["done"], cur["current_file"]), (2, 0, "a.pdf"))
            rec.file_done("a.pdf", "sha-a", "claude", "")
            cur = st.read_state(p)["current"]
            self.assertEqual((cur["done"], cur["current_file"]), (1, None))
            rec.file_started("b.pdf")
            rec.file_done("b.pdf", "sha-b", "failed", "อ่านไม่ได้")
            rec.finish(2, n_pdf=2, stats={"claude": 1, "failed": 1}, issues=["x"], output_file=Path("/o.xlsx"),
                       excel_written=True)
            s = st.read_state(p)
            self.assertIsNone(s["current"])
            lr = s["last_run"]
            self.assertEqual((lr["exit_code"], lr["fatal"], lr["n_pdf"], lr["trigger"], lr["excel_written"]),
                             (2, None, 2, "app", True))
            self.assertEqual(lr["stats"], {"cached": 0, "claude": 1, "text": 0, "failed": 1})
            self.assertEqual(lr["issues"], ["x"])
            self.assertEqual(lr["output_file"], "/o.xlsx")
            self.assertTrue(lr["started_at"].endswith("+07:00") and lr["finished_at"] > lr["started_at"])
            self.assertEqual(set(s["files"]), {"sha-a", "sha-b"})
            self.assertEqual(s["files"]["sha-b"]["outcome"], "failed")
            self.assertEqual(s["files"]["sha-b"]["message"], "อ่านไม่ได้")
            self.assertEqual(s["files"]["sha-a"]["rel"], "a.pdf")

    def test_previous_results_stay_visible_while_running_and_files_are_replaced_after(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            r1 = st.RunRecorder(p, "manual", clock=fake_clock())
            r1.start(); r1.begin(1); r1.file_started("old.pdf"); r1.file_done("old.pdf", "old", "claude")
            r1.finish(0, n_pdf=1)
            r2 = st.RunRecorder(p, "schedule", clock=fake_clock())
            r2.start()
            s = st.read_state(p)
            self.assertIsNotNone(s["current"])
            self.assertEqual(s["last_run"]["trigger"], "manual")        # previous run still there
            self.assertIn("old", s["files"])
            r2.begin(1); r2.file_started("new.pdf"); r2.file_done("new.pdf", "new", "cached")
            r2.finish(0, n_pdf=1)
            self.assertEqual(set(st.read_state(p)["files"]), {"new"})   # only hashes seen last run

    def test_fatal_before_listing_keeps_previous_files(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            r1 = st.RunRecorder(p, "manual", clock=fake_clock())
            r1.start(); r1.begin(1); r1.file_started("a.pdf"); r1.file_done("a.pdf", "a", "claude")
            r1.finish(0, n_pdf=1)
            r2 = st.RunRecorder(p, "manual", clock=fake_clock())
            r2.start()
            r2.finish(1, fatal="ไม่พบโฟลเดอร์")
            s = st.read_state(p)
            self.assertEqual((s["last_run"]["exit_code"], s["last_run"]["fatal"]), (1, "ไม่พบโฟลเดอร์"))
            self.assertIn("a", s["files"])
            self.assertIsNone(s["current"])

    def test_unhashable_file_counts_as_done_but_has_no_key(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state.json"
            r = st.RunRecorder(p, "manual", clock=fake_clock())
            r.start(); r.begin(1); r.file_started("x.pdf"); r.file_done("x.pdf", "", "failed", "อ่านไฟล์ไม่ได้")
            self.assertEqual(st.read_state(p)["current"]["done"], 1)
            r.finish(2, n_pdf=1, stats={"failed": 1})
            self.assertEqual(st.read_state(p)["files"], {})

    def test_unknown_trigger_and_outcome_are_normalised(self):
        with tempfile.TemporaryDirectory() as t:
            r = st.RunRecorder(Path(t) / "s.json", "bogus", clock=fake_clock())
            self.assertEqual(r.trigger, "manual")
            r.start(); r.begin(1); r.file_done("a.pdf", "a", "???")
            r.finish(0)
            self.assertEqual(st.read_state(Path(t) / "s.json")["files"]["a"]["outcome"], "failed")

    def test_unwritable_state_does_not_break_the_run(self):
        with tempfile.TemporaryDirectory() as t:
            blocker = Path(t) / "blocker"
            blocker.write_text("a file, so the state folder cannot be created", encoding="utf-8")
            r = st.RunRecorder(blocker / "state.json", "manual", clock=fake_clock())
            r.start(); r.begin(1); r.file_started("a.pdf"); r.file_done("a.pdf", "a", "claude"); r.finish(0)
            self.assertGreaterEqual(r.write_errors, 1)

    def test_issue_list_is_capped(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.json"
            r = st.RunRecorder(p, "manual", clock=fake_clock())
            r.start()
            r.finish(0, issues=["x" * 5000] * 500)
            issues = st.read_state(p)["last_run"]["issues"]
            self.assertEqual(len(issues), st.MAX_STATE_ISSUES)
            self.assertEqual(len(issues[0]), st.MAX_ISSUE_CHARS)


if __name__ == "__main__":
    unittest.main()
