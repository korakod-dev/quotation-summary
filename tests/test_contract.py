"""Contract test between the Python engine and the SwiftUI app.

The Swift tests (gui/Tests/QuotationSummaryAppTests) decode the fixtures gui/Tests/QuotationSummaryAppTests/Fixtures/
status-*.json. Those files must be REAL qsum.status output. This test builds the same synthetic world again (the
generator is tests/make_gui_fixtures.py), runs the real code, and checks that:

  - the key set and the value types of every object equal the fixture (so a renamed/added/removed field in
    qsum/status.py fails here until the fixtures are regenerated and the Swift decoder is looked at),
  - the enum values (files[].status, error_code, trigger, ...) used by the fixtures are in the contract (C3),
  - the values equal the fixture too, except the two that depend on the machine (output_mtime time zone and
    engine_version, which is the version string of qsum/__init__.py).

If it fails after an intended change:  .venv/bin/python tests/make_gui_fixtures.py  then  swift test --package-path gui

Nothing here touches the real Keychain, launchd, ~/Library or Claude: every path points into a temp folder.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from qsum import __version__

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_DIR / "gui" / "Tests" / "QuotationSummaryAppTests" / "Fixtures"
STATUS_ENUM = {"done", "check", "waiting", "error"}
ERROR_CODES = {None, "not_installed", "config", "source_missing", "source_unreadable"}
TRIGGERS = {"schedule", "app", "manual"}
TOP_KEYS = {"schema", "engine_version", "generated_at", "ok", "error_code", "error", "source_folder", "output_file",
            "output_exists", "output_mtime", "running", "progress", "last_run", "schedule", "counts", "files"}
MINIMAL_KEYS = {"schema", "ok", "error_code", "error", "generated_at"}      # guaranteed for not_installed / config


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def shape(value):
    """Keys and JSON types, recursively. A list is described by the shape of its first element."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__


def real(kind):
    """Real qsum.status output (imports the generator lazily: it builds a temp world, nothing else)."""
    sys.path.insert(0, str(PROJECT_DIR / "tests"))
    try:
        import make_gui_fixtures as gen
    finally:
        sys.path.pop(0)
    return gen.real_status(kind)


def without_machine_values(doc):
    doc = dict(doc)
    doc.pop("output_mtime", None)           # time zone of the machine that ran the test
    doc.pop("engine_version", None)         # asserted separately
    return doc


class ContractTests(unittest.TestCase):
    # ---- the fixtures themselves are in the contract -------------------------------------------------
    def test_fixture_files_exist(self):
        for name in ("status-v1.json", "status-running.json", "status-error.json", "status-not-installed.json"):
            self.assertTrue((FIXTURES / name).is_file(), name)

    def test_full_fixtures_have_every_top_level_key(self):
        for name in ("status-v1.json", "status-running.json", "status-error.json"):
            self.assertEqual(set(fixture(name)), TOP_KEYS, name)

    def test_minimal_fixture_has_only_the_guaranteed_keys(self):
        doc = fixture("status-not-installed.json")
        self.assertTrue(MINIMAL_KEYS <= set(doc))
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error_code"], "not_installed")

    def test_enums_in_fixtures_are_in_the_contract(self):
        for name in ("status-v1.json", "status-running.json", "status-error.json", "status-not-installed.json"):
            doc = fixture(name)
            self.assertIn(doc["error_code"], ERROR_CODES, name)
            self.assertEqual(doc["schema"], 1)
            for row in doc.get("files", []):
                self.assertIn(row["status"], STATUS_ENUM, name)
                self.assertIn(row["method"], {None, "claude", "text"}, name)
            if doc.get("last_run"):
                self.assertIn(doc["last_run"]["trigger"], TRIGGERS, name)
            if doc.get("progress"):
                self.assertIn(doc["progress"]["trigger"], TRIGGERS, name)

    def test_v1_fixture_covers_all_four_row_statuses_and_is_sorted(self):
        rows = fixture("status-v1.json")["files"]
        self.assertEqual({r["status"] for r in rows}, STATUS_ENUM)
        order = {"error": 0, "waiting": 1, "check": 2, "done": 3}
        self.assertEqual([order[r["status"]] for r in rows], sorted(order[r["status"]] for r in rows))
        counts = fixture("status-v1.json")["counts"]
        self.assertEqual(counts["total"], len(rows))
        for status in STATUS_ENUM:
            self.assertEqual(counts[status], len([r for r in rows if r["status"] == status]))

    # ---- fresh real output == fixture ---------------------------------------------------------------
    def check_same(self, kind, name):
        fresh, fixed = real(kind), fixture(name)
        self.assertEqual(shape(fresh), shape(fixed),
                         "qsum.status output changed shape: regenerate with tests/make_gui_fixtures.py and check the Swift decoder")
        self.assertEqual(without_machine_values(fresh), without_machine_values(fixed),
                         "qsum.status output differs from %s: regenerate with tests/make_gui_fixtures.py" % name)
        return fresh

    def test_idle_output_matches_status_v1(self):
        fresh = self.check_same("v1", "status-v1.json")
        self.assertEqual(fresh["engine_version"], __version__)
        self.assertEqual(fixture("status-v1.json")["engine_version"], __version__,
                         "fixture was made by another engine version: regenerate it")

    def test_running_output_matches_status_running(self):
        fresh = self.check_same("running", "status-running.json")
        self.assertTrue(fresh["running"])
        self.assertIsNotNone(fresh["progress"])

    def test_source_missing_output_matches_status_error(self):
        fresh = self.check_same("error", "status-error.json")
        self.assertFalse(fresh["ok"])
        self.assertEqual(fresh["error_code"], "source_missing")
        self.assertEqual(fresh["files"], [])

    def test_not_installed_output_of_status_sh_matches(self):
        fresh = real("not-installed")
        fixed = fixture("status-not-installed.json")
        self.assertEqual(shape(fresh), shape(fixed))
        self.assertEqual(fresh, fixed)

    def test_every_row_has_the_same_keys(self):
        rows = real("v1")["files"]
        keys = {"name", "sha256", "size", "added", "status", "method", "will_retry", "detail", "quote_no", "company",
                "total"}
        for r in rows:
            self.assertEqual(set(r), keys, r["name"])

    # ---- C1: exit codes, as the app interprets them ---------------------------------------------------
    def test_exit_code_3_text_is_the_one_the_app_shows(self):
        """The Thai busy message of qsum.main and the one of the app must tell the same story (exit code 3)."""
        main_py = (PROJECT_DIR / "qsum" / "main.py").read_text(encoding="utf-8")
        self.assertIn("กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา)", main_py)
        labels = (PROJECT_DIR / "gui" / "Sources" / "QuotationSummaryApp" / "Model" / "Labels.swift").read_text(encoding="utf-8")
        self.assertIn("กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา)", labels)

    def test_status_cli_prints_utf8_json_and_exit_code_matches_ok(self):
        """End to end through `python -m qsum.status --json --config FILE` on a config that does not parse."""
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "config.json"
            bad.write_text("{ broken", encoding="utf-8")
            env = dict(os.environ, QS_LAUNCH_AGENTS_DIR=str(Path(t) / "agents"), PYTHONIOENCODING="ascii", LC_ALL="C")
            env.pop("QS_CONFIG", None)
            p = subprocess.run([sys.executable, "-m", "qsum.status", "--json", "--config", str(bad)], cwd=str(PROJECT_DIR),
                               capture_output=True, env=env)
        self.assertEqual(p.returncode, 1, p.stderr)
        doc = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(doc["error_code"], "config")
        self.assertTrue(MINIMAL_KEYS <= set(doc))
        self.assertIn("JSON", doc["error"])        # Thai text came out as UTF-8 even with an ASCII locale


if __name__ == "__main__":
    unittest.main()
