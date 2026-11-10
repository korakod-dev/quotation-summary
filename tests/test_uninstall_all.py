"""Tests for the full uninstaller: ถอนการติดตั้ง.command -> scripts/uninstall-all.command.

Same approach as tests/test_setup_scripts.py: a throw-away fake $HOME, and PATH stubs for security / launchctl /
claude that only record their arguments (and keep a little state, so "delete" really removes the fake item).
Nothing touches the real Keychain, launchd, Claude Code or the real home folder.

Run:  .venv/bin/python -m unittest tests.test_uninstall_all -v      (macOS only: uses /bin/bash 3.2)
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_setup_scripts import (APP, BASH, FAKE_CONFIG, GUI_ID, GUI_NAME, IS_MAC, SERVICE, STUB_CLAUDE, Sandbox,
                                      _write_exec, gui_payload, make_gui_bundle)

LAUNCHER = "ถอนการติดตั้ง.command"
UNINSTALL = "scripts/uninstall-all.command"
API_SERVICE = "quotation-summary-anthropic"   # default of keychain_service when config.json does not set it
WORD = "ลบ"
CLAUDE_WORD = "ย้าย .claude"      # the separate phrase for moving ~/.claude aside (needs --include-claude-home)
FLAG = "--include-claude-home"
VENV_TOKEN = "quotation-summary-venv-token"
VENV_API = "quotation-summary-venv-api"

# security stub with per-service state: find/delete look at $STUB_STATE/kc-<service>. add is never used here.
STUB_SECURITY_KC = """#!/bin/bash
echo "security $*" >> "$STUB_LOG"
svc=""; prev=""
for a in "$@"; do [ "$prev" = "-s" ] && svc="$a"; prev="$a"; done
case "$1" in
  find-generic-password)   [ -f "$STUB_STATE/kc-$svc" ] && { echo 'keychain: "stub item"'; exit 0; }; exit 44;;
  delete-generic-password) [ -f "$STUB_STATE/kc-$svc" ] && { rm -f "$STUB_STATE/kc-$svc"; exit 0; }; exit 44;;
esac
exit 0
"""
# launchctl stub: `print` succeeds while $STUB_STATE/registered exists, `bootout` removes it.
STUB_LAUNCHCTL_UN = """#!/bin/bash
echo "launchctl $*" >> "$STUB_LOG"
case "$1" in
  print)   [ -f "$STUB_STATE/registered" ] && exit 0; exit 113;;
  bootout) rm -f "$STUB_STATE/registered"; exit 0;;
esac
exit 0
"""
# pgrep stub: "a claude process is running" while $STUB_STATE/claude-running exists (never looks at real processes).
STUB_PGREP = """#!/bin/bash
[ -f "$STUB_STATE/claude-running" ] && { echo 4242; exit 0; }
exit 1
"""
# Stand-in for <app>/.venv/bin/python: run.sh calls `python -m qsum FLAG`; the answer comes from the file
# $STUB_STATE/lookup<FLAG> (e.g. lookup--print-output-path); no file = no output, like an unknown flag.
STUB_VENV_PYTHON = """#!/bin/bash
f="$STUB_STATE/lookup$3"
[ -f "$f" ] && cat "$f"
exit 0
"""


def cfg(source="~/QuotationSummary/ใบเสนอราคาทดสอบ", token=SERVICE, api=None, claude_dir=None, output=None):
    """A minimal config.json text."""
    parts = ['"source_folder": "%s"' % source, '"claude_code_keychain_service": "%s"' % token]
    if api:
        parts.append('"keychain_service": "%s"' % api)
    if claude_dir is not None:
        parts.append('"claude_code_config_dir": "%s"' % claude_dir)
    if output is not None:
        parts.append('"output_file": "%s"' % output)
    return "{\n  %s,\n  \"companies\": []\n}\n" % ",\n  ".join(parts)


class Fixture:
    """A fake machine with a complete installation: schedule, Keychain items, cache, logs, program, PDFs, Claude Code."""

    def __init__(self, root: Path, config=FAKE_CONFIG, installed=True):
        self.sb = Sandbox(root, with_claude=True)
        sb = self.sb
        _write_exec(sb.stubs / "security", STUB_SECURITY_KC)
        _write_exec(sb.stubs / "launchctl", STUB_LAUNCHCTL_UN)
        _write_exec(sb.stubs / "pgrep", STUB_PGREP)
        h = sb.home
        self.home = h
        self.appsup = h / "Library" / "Application Support" / "QuotationSummary"
        self.logs = h / "Library" / "Logs" / "QuotationSummary"
        self.plist = h / "Library" / "LaunchAgents" / "com.korakod.quotation-summary.plist"
        self.target = sb.target
        self.container = h / "QuotationSummary"
        self.source = self.container / "ใบเสนอราคาทดสอบ"
        self.dl_dir = h / "Downloads" / "quotation-summary"
        self.dl_zip = h / "Downloads" / "quotation-summary.zip"
        self.claude_bin = h / ".local" / "bin" / "claude"
        self.claude_data = h / ".local" / "share" / "claude"
        self.claude_home = h / ".claude"
        self.claude_json = h / ".claude.json"

        (self.appsup / "claude-code").mkdir(parents=True)
        (self.appsup / "cache.json").write_text("{}")
        (self.appsup / "claude-code" / "settings.json").write_text("{}")
        self.logs.mkdir(parents=True)
        (self.logs / "run.log").write_text("log")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text("<plist/>")
        (sb.state / "registered").write_text("")
        (sb.state / ("kc-" + SERVICE)).write_text("")
        (sb.state / ("kc-" + API_SERVICE)).write_text("")
        if installed:
            sb.make_app(self.target, config=config)
        self.source.mkdir(parents=True)
        (self.source / "QT0001.pdf").write_text("%PDF fake")
        (self.source / "สรุปใบเสนอราคา.xlsx").write_text("xlsx")
        self.dl_dir.mkdir(parents=True)
        (self.dl_dir / "config.json").write_text("{}")
        self.dl_zip.write_text("zip")
        # native Claude Code as the docs describe it: launcher symlink into ~/.local/share/claude/versions/
        (self.claude_data / "versions").mkdir(parents=True)
        (self.claude_data / "versions" / "2.1.0").write_text("binary")
        self.claude_bin.parent.mkdir(parents=True)
        os.symlink(str(self.claude_data / "versions" / "2.1.0"), str(self.claude_bin))
        self.claude_home.mkdir()
        (self.claude_home / "settings.json").write_text("{}")
        self.claude_json.write_text("{}")

    def make_venv(self, lookups, app=None):
        """Give the installed app a stub .venv so run_lookup works; lookups = {"--print-output-path": "value", ...}."""
        app = app or self.target
        _write_exec(app / ".venv" / "bin" / "python", STUB_VENV_PYTHON)
        for flag, value in lookups.items():
            (self.sb.state / ("lookup" + flag)).write_text(str(value) + "\n", encoding="utf-8")

    def run(self, args=(), answers="", script=None, **kw):
        script = script or (self.target / UNINSTALL)
        return self.sb.run(script, list(args), answers=answers, **kw)

    def deletions(self):
        return [l for l in self.sb.log_lines() if "delete-generic-password" in l or l.startswith("launchctl bootout")]


@unittest.skipUnless(IS_MAC, "macOS only (bash 3.2)")
class UninstallAllTests(unittest.TestCase):
    """Question order (each defaults to N): 6 source folder [, Excel outside it], 7a Claude Code,
    [7b ~/.claude: only with --include-claude-home AND "y" to 7a, answered by a typed phrase], 8 Downloads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.sb = self.fx.sb

    def tearDown(self):
        self._tmp.cleanup()

    # ---- helpers ----
    def assertExists(self, *paths):
        for p in paths:
            self.assertTrue(os.path.lexists(str(p)), "should still exist: %s" % p)

    def assertGone(self, *paths):
        for p in paths:
            self.assertFalse(os.path.lexists(str(p)), "should be gone: %s" % p)

    def mandatory(self):
        f = self.fx
        return [f.plist, f.appsup, f.logs, f.target]

    def optional(self):
        """Optional items that a plain run (no --include-claude-home) may remove."""
        f = self.fx
        return [f.source, f.dl_dir, f.dl_zip, f.claude_bin, f.claude_data]

    def claude_home_items(self):
        return [self.fx.claude_home, self.fx.claude_json]

    def deletes(self):
        return [l for l in self.sb.log_lines() if "delete-generic-password" in l]

    def backups(self):
        return sorted(p.name for p in self.fx.home.iterdir() if ".claude" in p.name and ".bak-" in p.name)

    def setUp_fresh(self):
        self._tmp.cleanup()
        self.setUp()

    # ---- bash 3.2 syntax + launcher ----
    def test_bash_syntax(self):
        for name in (LAUNCHER, UNINSTALL):
            r = subprocess.run([BASH, "-n", str(APP / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "%s: %s" % (name, r.stderr))
        self.assertTrue(os.access(str(APP / LAUNCHER), os.X_OK))
        self.assertTrue(os.access(str(APP / UNINSTALL), os.X_OK))

    def test_unknown_option_is_rejected(self):
        r = self.fx.run(["--bogus"])
        self.assertEqual(r.returncode, 2)
        self.assertIn(FLAG, r.stderr)
        self.assertEqual(self.fx.deletions(), [])

    # ---- dry run ----
    def test_dry_run_deletes_nothing_even_when_everything_is_answered_yes(self):
        # source y, Claude Code y, ~/.claude phrase, Downloads y
        r = self.fx.run(["--dry-run", FLAG], answers="y\ny\n" + CLAUDE_WORD + "\ny\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("(ทดลอง)", r.stdout)
        self.assertExists(*self.mandatory(), *self.optional(), *self.claude_home_items())
        self.assertEqual(self.fx.deletions(), [], "dry run must not call bootout / delete-generic-password")
        self.assertEqual(self.backups(), [])
        self.assertTrue((self.sb.state / "registered").exists())
        # the plan still lists what would happen, including the optional items that were answered yes
        self.assertIn(str(self.fx.source), r.stdout)
        self.assertIn(".claude.bak-", r.stdout)

    # ---- confirmation ----
    def test_confirmation_is_required_and_wrong_word_aborts(self):
        for answers in ("", "n\nn\nn\n", "n\nn\nn\nใช่\n", "n\nn\nn\nyes\n", "n\nn\nn\n\n", "n\nn\nn\nลบๆ\n"):
            r = self.fx.run([], answers=answers)
            self.assertEqual(r.returncode, 1, "answers=%r\n%s" % (answers, r.stdout))
            self.assertIn("ยกเลิก", r.stdout)
            self.assertExists(*self.mandatory(), *self.optional(), *self.claude_home_items())
        self.assertEqual(self.fx.deletions(), [])

    def test_confirmation_word_in_english_works_and_prompt_mentions_it(self):
        # a keyboard that is not on the Thai layout can still confirm
        for word in ("DELETE", "delete", "  Delete  "):
            with self.subTest(word=word):
                self.setUp_fresh()
                r = self.fx.run([], answers="n\nn\nn\n" + word + "\n")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertGone(*self.mandatory())
                self.assertIn("DELETE", r.stdout)
                self.assertIn("แป้นพิมพ์", r.stdout)

    def test_correct_word_removes_the_mandatory_items_and_keeps_optional_ones(self):
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")      # default N for all 3 optional questions
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(*self.mandatory())
        self.assertExists(*self.optional(), *self.claude_home_items(), self.fx.container)   # PDFs keep the container alive
        log = self.sb.log_lines()
        self.assertIn("launchctl bootout gui/%d/com.korakod.quotation-summary" % os.getuid(), log)
        self.assertIn("security delete-generic-password -s " + SERVICE, log)
        self.assertIn("security delete-generic-password -s " + API_SERVICE, log)
        self.assertFalse((self.sb.state / "registered").exists())
        self.assertIn("ตรวจระบบ.command", r.stdout)
        self.assertIn("ติดตั้ง.command", r.stdout)

    def test_empty_answers_default_to_no_for_optional_items(self):
        r = self.fx.run([], answers="\n\n\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(*self.optional(), *self.claude_home_items())

    def test_yes_flag_skips_only_the_confirmation_word(self):
        r = self.fx.run(["--yes"], answers="")      # no stdin at all: optional questions fall back to N
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(*self.mandatory())
        self.assertExists(*self.optional(), *self.claude_home_items())

    def test_no_secret_is_ever_requested(self):
        self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        for line in self.sb.calls("security"):
            self.assertNotRegex(line, r"(^|\s)-[wg](\s|$)", line)   # -w / -g would print or prompt for the secret

    # ---- optional items answered yes ----
    def test_optional_items_are_removed_when_answered_yes(self):
        r = self.fx.run([], answers="y\ny\ny\n" + WORD + "\n")      # no ~/.claude question without the flag
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(*self.mandatory(), *self.optional())
        self.assertGone(self.fx.container)          # emptied by the steps before -> removed with rmdir
        self.assertExists(self.fx.home, self.fx.home / "Downloads", *self.claude_home_items())

    def test_container_with_other_files_is_kept(self):
        (self.fx.container / "my-notes.txt").write_text("keep me")
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.source, self.fx.target)
        self.assertExists(self.fx.container / "my-notes.txt")

    def test_only_one_optional_item_answered_yes(self):
        r = self.fx.run([], answers="n\ny\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.claude_bin, self.fx.claude_data)
        self.assertExists(self.fx.source, self.fx.claude_home, self.fx.claude_json, self.fx.dl_dir, self.fx.dl_zip)

    # ---- running from the installed copy ----
    def test_script_started_from_the_installed_folder_can_delete_it(self):
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n", cwd=str(self.fx.target))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.target)
        self.assertEqual([p.name for p in (self.sb.root / "tmp").glob("qs-uninstall.*")], [], "temp copy must be cleaned up")

    def test_launcher_runs_the_uninstaller(self):
        r = self.sb.run(self.fx.target / LAUNCHER, ["--dry-run"], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ถอนการติดตั้ง", r.stdout)
        self.assertExists(*self.mandatory())

    # ---- idempotent ----
    def test_second_run_finds_nothing_and_succeeds(self):
        self.fx.run([], answers="y\ny\ny\n" + WORD + "\n")
        before = self.sb.log_lines()
        # the installed copy is gone: run the uninstaller from another copy of the app
        other = self.sb.make_app(self.sb.root / "copy" / "quotation-summary")
        r = self.sb.run(other / UNINSTALL, [], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่มีอยู่แล้ว", r.stdout)
        self.assertIn("ไม่มีอะไรต้องลบ", r.stdout)
        self.assertEqual([l for l in self.sb.log_lines()[len(before):] if "delete-generic-password" in l or "bootout" in l], [])
        self.assertExists(other)        # the copy we ran from is not the installed one: untouched

    def test_missing_keychain_items_are_reported_not_deleted(self):
        (self.sb.state / ("kc-" + API_SERVICE)).unlink()
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deletes(), ["security delete-generic-password -s " + SERVICE])

    def test_service_names_come_from_config(self):
        self.fx.target.joinpath("config.json").write_text(
            cfg(token="quotation-summary-my-token", api="quotation-summary-my-api"), encoding="utf-8")
        (self.sb.state / ("kc-" + SERVICE)).unlink()
        (self.sb.state / "kc-quotation-summary-my-token").write_text("")
        (self.sb.state / "kc-quotation-summary-my-api").write_text("")
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(self.deletes()), ["security delete-generic-password -s quotation-summary-my-api",
                                                  "security delete-generic-password -s quotation-summary-my-token"])
        self.assertTrue((self.sb.state / ("kc-" + API_SERVICE)).exists())   # default name untouched: config said otherwise

    # ---- keychain names must start with quotation-summary ----
    def test_keychain_names_of_other_programs_are_never_deleted(self):
        for token, api in (("Claude Code-credentials", "Claude Code-credentials"), ("other-app", "quotation-summary-ok"),
                           ("quotation-summary-ok", "Claude Code")):
            with self.subTest(token=token, api=api):
                self.setUp_fresh()
                self.fx.target.joinpath("config.json").write_text(cfg(token=token, api=api), encoding="utf-8")
                for name in (token, api, "quotation-summary-ok"):
                    (self.sb.state / ("kc-" + name)).write_text("")
                r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                for line in self.deletes():
                    self.assertTrue(line.split("-s ", 1)[1].startswith("quotation-summary"), line)
                for name in (token, api):
                    if not name.startswith("quotation-summary"):
                        self.assertTrue((self.sb.state / ("kc-" + name)).exists(), name)
                        self.assertIn("ไม่ขึ้นต้นด้วย quotation-summary", r.stdout)
                if "quotation-summary-ok" in (token, api):
                    self.assertFalse((self.sb.state / "kc-quotation-summary-ok").exists())    # the valid one is still removed

    # ---- developer checkout ----
    def test_dev_checkout_is_never_deleted_but_installed_copy_still_is(self):
        dev = self.sb.make_app(self.sb.root / "work" / "quotation-summary")
        (self.sb.root / "work" / ".git").mkdir()
        r = self.sb.run(dev / UNINSTALL, [], answers="n\nn\nn\n" + WORD + "\n", cwd=str(dev))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.target)         # the installed copy under ~/QuotationSummary is removed
        self.assertExists(dev, dev / UNINSTALL, dev / "qsum" / "main.py", dev / "config.json", self.sb.root / "work" / ".git")

    def test_installed_config_wins_over_the_config_of_the_folder_we_started_from(self):
        # run from a dev checkout whose config names other Keychain items: the installed copy's names are used
        dev = self.sb.make_app(self.sb.root / "work" / "quotation-summary",
                               config=cfg(token="quotation-summary-dev-token", api="quotation-summary-dev-api"))
        (self.sb.root / "work" / ".git").mkdir()
        (self.sb.state / "kc-quotation-summary-dev-token").write_text("")
        (self.sb.state / "kc-quotation-summary-dev-api").write_text("")
        r = self.sb.run(dev / UNINSTALL, [], answers="n\nn\nn\n" + WORD + "\n", cwd=str(dev))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(self.deletes()), ["security delete-generic-password -s " + API_SERVICE,
                                                  "security delete-generic-password -s " + SERVICE])
        self.assertTrue((self.sb.state / "kc-quotation-summary-dev-token").exists())
        self.assertTrue((self.sb.state / "kc-quotation-summary-dev-api").exists())

    def test_config_of_started_folder_is_used_when_nothing_is_installed(self):
        # no installed copy: fall back to the config of the folder we were started from
        self.setUp_fresh_without_install()
        other = self.sb.make_app(self.sb.root / "copy" / "quotation-summary",
                                 config=cfg(token="quotation-summary-copy-token"))
        (self.sb.state / "kc-quotation-summary-copy-token").write_text("")
        r = self.sb.run(other / UNINSTALL, [], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("security delete-generic-password -s quotation-summary-copy-token", self.sb.log_lines())

    def setUp_fresh_without_install(self):
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name), installed=False)
        self.sb = self.fx.sb

    def test_installed_folder_that_is_a_git_work_tree_is_refused(self):
        (self.fx.target / ".git").mkdir()
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("git", r.stdout)
        self.assertExists(self.fx.target, self.fx.target / ".git", self.fx.target / "qsum" / "main.py")
        self.assertGone(self.fx.appsup, self.fx.logs, self.fx.plist)       # the other items are still removed

    def test_folder_that_is_not_this_app_is_refused(self):
        (self.fx.target / "qsum" / "main.py").unlink()
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n", script=self.sb.make_app(self.sb.root / "copy" / "quotation-summary") / UNINSTALL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(self.fx.target)
        self.assertIn("ไม่ใช่โฟลเดอร์ของโปรแกรมนี้", r.stdout)

    def test_download_copy_that_is_a_git_work_tree_is_refused(self):
        (self.fx.dl_dir / ".git").mkdir()
        r = self.fx.run([], answers="n\nn\ny\n" + WORD + "\n")     # source n, Claude Code n, Downloads y
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(self.fx.dl_dir, self.fx.dl_dir / ".git")
        self.assertGone(self.fx.dl_zip)         # the plain zip next to it is a leftover copy and goes

    # ---- unsafe paths ----
    def _unsafe_source(self, source):
        """Run with a config whose source_folder is `source`; return (result, a folder that must survive).
        A refused source folder is never offered, so only 2 questions remain (Claude Code, Downloads).
        If it were wrongly accepted the first "n" would be eaten by the source question, the confirmation word
        would be misread and the run would abort with status 1 (the callers assert status 0)."""
        self.fx.target.joinpath("config.json").write_text(cfg(source=source), encoding="utf-8")
        keep = self.fx.home / "Documents" / "Quotes"
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "keep.pdf").write_text("x")
        r = self.fx.run([], answers="n\nn\n" + WORD + "\n")
        return r, keep

    def test_unsafe_source_folders_are_refused(self):
        for source in ("~", "/", "HOME", "HOME/", "~/Documents", "~/Library/Application Support", "~/.ssh",
                       "~/Documents/../Documents/Quotes", "/tmp", "/private", "~/QuotationSummary/../.."):
            with self.subTest(source=source):
                self.setUp_fresh()
                h = self.fx.home
                if source.startswith("HOME"):
                    source = str(h) + source[4:]          # the fake home itself, with and without a trailing slash
                r, keep = self._unsafe_source(source)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertExists(h, h / "Documents", h / "Library", keep / "keep.pdf", self.fx.source / "QT0001.pdf")
                self.assertNotIn("ลบเฉพาะไฟล์ PDF/Excel", r.stdout)

    def test_source_folder_that_is_a_symlink_is_refused(self):
        real = self.fx.home / "Documents" / "RealQuotes"
        real.mkdir(parents=True)
        (real / "a.pdf").write_text("x")
        link = self.fx.home / "Documents" / "LinkQuotes"
        os.symlink(str(real), str(link))
        self.fx.target.joinpath("config.json").write_text(cfg(source="~/Documents/LinkQuotes"), encoding="utf-8")
        r = self.fx.run([], answers="n\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(real / "a.pdf", link)
        self.assertNotIn("ลบเฉพาะไฟล์ PDF/Excel", r.stdout)

    def test_source_folder_with_git_inside_is_refused(self):
        (self.fx.source / ".git").mkdir()
        r = self.fx.run([], answers="n\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(self.fx.source / "QT0001.pdf")
        self.assertIn("git", r.stdout)

    def test_source_folder_with_a_deeply_nested_git_repo_is_refused(self):
        # the old check looked only 3 levels deep; a repo at depth 6 must refuse the whole folder too
        deep = self.fx.source / "clients" / "acme" / "2026" / "q1" / "repo"
        (deep / ".git").mkdir(parents=True)
        (deep / "code.py").write_text("x")
        r = self.fx.run([], answers="n\nn\n" + WORD + "\n")        # refused -> no source question
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(self.fx.source / "QT0001.pdf", self.fx.source / "สรุปใบเสนอราคา.xlsx", deep / ".git", deep / "code.py")
        self.assertIn("มีโฟลเดอร์ git อยู่ข้างใน", r.stdout)
        self.assertNotIn("ลบเฉพาะไฟล์ PDF/Excel", r.stdout)

    # ---- only *.pdf / *.xlsx are removed from the owner's folder ----
    def test_source_folder_with_other_files_keeps_them_and_the_folder(self):
        src = self.fx.source
        precious = self.fx.home / "Documents" / "precious.txt"
        precious.parent.mkdir(parents=True, exist_ok=True)
        precious.write_text("keep")
        (src / "notes.docx").write_text("keep")
        (src / "sub" / "deep").mkdir(parents=True)
        (src / "sub" / "deep" / "data.txt").write_text("keep")
        (src / "sub" / "B.PDF").write_text("pdf")                   # upper-case extension counts as PDF
        (src / "tmpab12.xlsx.tmp").write_text("tmp")                # the program's own temp file (in the output's folder)
        (src / ".DS_Store").write_text("x")
        os.symlink(str(precious), str(src / "shortcut.pdf"))        # a symlink is "other": never touched
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไฟล์อื่นอีก 3 ไฟล์", r.stdout)   # notes.docx, data.txt, shortcut.pdf
        self.assertIn("จะไม่ลบทั้งโฟลเดอร์", r.stdout)
        self.assertGone(src / "QT0001.pdf", src / "สรุปใบเสนอราคา.xlsx", src / "sub" / "B.PDF", src / "tmpab12.xlsx.tmp",
                        src / ".DS_Store")
        self.assertExists(src, src / "notes.docx", src / "sub" / "deep" / "data.txt", src / "shortcut.pdf", precious)
        self.assertIn("เก็บโฟลเดอร์ไว้", r.stdout)
        self.assertExists(self.fx.container)

    def test_source_folder_with_only_pdf_and_excel_is_removed_with_empty_subfolders(self):
        (self.fx.source / "2026" / "q1").mkdir(parents=True)
        (self.fx.source / "2026" / "q1" / "QT0002.PDF").write_text("pdf")
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไฟล์อื่นอีก 0 ไฟล์", r.stdout)
        self.assertGone(self.fx.source, self.fx.container)

    def test_the_question_for_the_source_folder_shows_counts_and_size(self):
        r = self.fx.run(["--dry-run"], answers="")
        self.assertIn("PDF 1 ไฟล์ และ Excel สรุปของโปรแกรม 1 ไฟล์", r.stdout)
        self.assertRegex(r.stdout, r"ขนาดรวมทั้งโฟลเดอร์ \S+\)")
        self.assertIn("ลบเฉพาะไฟล์ PDF/Excel", r.stdout)

    # ---- only the program's own files go; the owner's other Excel files and folders stay ----
    def test_other_excel_files_and_a_package_inside_are_kept(self):
        # an .xlsx that is not output_file is the owner's own work; a package folder's PDFs are never touched
        src = self.fx.source
        (src / "ราคา.xlsx").write_text("owner")
        (src / "งานอื่น").mkdir()
        (src / "งานอื่น" / "Budget.XLSX").write_text("owner")
        pkg = src / "package"
        pkg.mkdir()
        (pkg / "inside.pdf").write_text("pdf")
        (pkg / "data.xlsx").write_text("owner")
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(src / "QT0001.pdf", src / "สรุปใบเสนอราคา.xlsx", pkg / "inside.pdf")   # pdf anywhere + output_file
        self.assertExists(src, src / "ราคา.xlsx", src / "งานอื่น" / "Budget.XLSX", pkg / "data.xlsx", self.fx.container)
        self.assertIn("Excel ที่โปรแกรมไม่ได้สร้าง 3 ไฟล์", r.stdout)
        self.assertIn("ไฟล์อื่นอีก 3 ไฟล์", r.stdout)
        self.assertIn("ราคา.xlsx", r.stdout)                  # example names are shown
        self.assertIn("เก็บโฟลเดอร์ไว้", r.stdout)

    def test_resolved_output_file_in_a_subfolder_is_the_only_excel_removed(self):
        src = self.fx.source
        out = src / "out" / "my-summary.xlsx"
        out.parent.mkdir()
        out.write_text("xlsx")
        (out.parent / (out.name + ".tmp")).write_text("tmp")
        (out.parent / "tmpq1w2e3.xlsx.tmp").write_text("tmp")
        (src / "สรุปใบเสนอราคา.xlsx").write_text("owner")      # the default name, but NOT output_file here
        (src / "elsewhere.xlsx.tmp").write_text("owner")       # a temp-looking file outside the output's folder
        self.fx.make_venv({"--print-output-path": out})
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(out, out.parent / (out.name + ".tmp"), out.parent / "tmpq1w2e3.xlsx.tmp", src / "QT0001.pdf")
        self.assertExists(src / "สรุปใบเสนอราคา.xlsx", src / "elsewhere.xlsx.tmp")

    def test_output_file_that_is_a_symlink_to_an_owner_file_is_not_followed(self):
        src = self.fx.source
        (src / "mine.xlsx").write_text("owner")
        os.symlink(str(src / "mine.xlsx"), str(src / "link-out.xlsx"))
        self.fx.make_venv({"--print-output-path": src / "link-out.xlsx"})
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(src / "mine.xlsx", src / "link-out.xlsx")

    def test_question_counts_match_what_is_shown(self):
        # the total in the question = PDF + program Excel shown above it
        (self.fx.source / "extra.pdf").write_text("pdf")
        (self.fx.source / "ราคา.xlsx").write_text("owner")
        r = self.fx.run(["--dry-run"], answers="")
        self.assertIn("PDF 2 ไฟล์ และ Excel สรุปของโปรแกรม 1 ไฟล์ และไฟล์อื่นอีก 1 ไฟล์", r.stdout)
        self.assertIn("(รวม 3 ไฟล์)", r.stdout)

    def test_folder_with_only_ds_store_gets_the_empty_folder_question(self):
        # .DS_Store is noise, not a PDF/Excel of the owner
        self._tmp.cleanup(); self.setUp()
        for p in self.fx.source.iterdir():
            p.unlink()
        (self.fx.source / ".DS_Store").write_text("x")
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PDF 0 ไฟล์ และ Excel สรุปของโปรแกรม 0 ไฟล์", r.stdout)
        self.assertIn("ลบโฟลเดอร์ว่าง", r.stdout)
        self.assertNotIn("ลบเฉพาะไฟล์ PDF/Excel", r.stdout)
        self.assertNotIn("คำเตือน: นี่คือ PDF", r.stdout)
        self.assertGone(self.fx.source, self.fx.container)

    def test_folder_with_only_empty_subfolders_is_removed_when_the_owner_says_yes(self):
        for p in self.fx.source.iterdir():
            p.unlink()
        (self.fx.source / "2027" / "q1").mkdir(parents=True)
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ลบโฟลเดอร์ว่าง", r.stdout)
        self.assertGone(self.fx.source, self.fx.container)

    def test_subfolders_that_were_already_empty_are_kept(self):
        # only sub-folders emptied by this run are removed
        src = self.fx.source
        (src / "2027").mkdir()                                  # waiting for next year, empty
        (src / "2028").mkdir()
        (src / "2028" / ".DS_Store").write_text("x")            # Finder noise only: still "empty"
        (src / "2026" / "q1").mkdir(parents=True)
        (src / "2026" / "q1" / "QT9.pdf").write_text("pdf")     # becomes empty because of us
        r = self.fx.run([], answers="y\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(src / "QT0001.pdf", src / "2026")
        self.assertExists(src / "2027", src / "2028", self.fx.container)
        self.assertIn("โฟลเดอร์ย่อยว่างที่มีอยู่เดิม", r.stdout)

    # ---- the Excel output outside the PDF folder (via .venv stub, config fallback, unsafe variants) ----
    def _excel(self, rel="Documents/Out/summary.xlsx"):
        x = self.fx.home / rel
        x.parent.mkdir(parents=True, exist_ok=True)
        x.write_text("xlsx")
        return x

    def test_lookups_from_run_py_decide_keychain_names_claude_dir_and_excel(self):
        # run_lookup answers (via a stub .venv) win over config.json; this is the only path to an Excel outside the folder
        xls = self._excel()
        ours = self.fx.home / "Documents" / "QuotationSummary" / "claude-code"
        ours.mkdir(parents=True)
        (self.sb.state / ("kc-" + VENV_TOKEN)).write_text("")
        (self.sb.state / ("kc-" + VENV_API)).write_text("")
        self.fx.make_venv({"--print-claude-token-service": VENV_TOKEN, "--print-keychain-service": VENV_API,
                           "--print-claude-config-dir": ours, "--print-output-path": xls})
        r = self.fx.run([], answers="n\ny\nn\nn\n" + WORD + "\n")      # source n, Excel y, Claude Code n, Downloads n
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("อยู่นอกโฟลเดอร์ PDF", r.stdout)
        self.assertGone(xls, ours)
        self.assertExists(xls.parent, ours.parent, self.fx.source / "QT0001.pdf")
        self.assertEqual(sorted(self.deletes()), ["security delete-generic-password -s " + VENV_API,
                                                  "security delete-generic-password -s " + VENV_TOKEN])
        self.assertTrue((self.sb.state / ("kc-" + SERVICE)).exists())    # config's name was not used

    def test_excel_outside_the_folder_is_kept_when_answered_no(self):
        xls = self._excel()
        self.fx.make_venv({"--print-output-path": xls})
        r = self.fx.run([], answers="n\nn\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(xls)
        self.assertIn("ไม่ลบไฟล์ Excel", r.stdout)

    def test_excel_that_is_a_symlink_is_refused(self):
        real = self._excel("Documents/Real/real.xlsx")
        link = self.fx.home / "Documents" / "Out" / "link.xlsx"
        link.parent.mkdir(parents=True)
        os.symlink(str(real), str(link))
        self.fx.make_venv({"--print-output-path": link})
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")      # refused: no Excel question
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(real, link)
        self.assertIn("symlink", r.stdout)

    def test_excel_outside_home_is_refused(self):
        out = self.sb.root / "outside" / "summary.xlsx"
        out.parent.mkdir()
        out.write_text("xlsx")
        self.fx.make_venv({"--print-output-path": out})
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(out)
        self.assertIn("อยู่นอกโฟลเดอร์ home", r.stdout)

    def test_excel_output_that_is_not_xlsx_is_refused(self):
        notes = self._excel("Documents/Out/notes.txt")
        self.fx.make_venv({"--print-output-path": notes})
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(notes)
        self.assertIn("ไม่ใช่ไฟล์ .xlsx", r.stdout)

    def test_excel_output_that_is_a_folder_is_refused(self):
        folder = self.fx.home / "Documents" / "Other.xlsx"      # a folder that merely looks like an Excel file
        folder.mkdir(parents=True)
        (folder / "f.txt").write_text("x")
        self.fx.make_venv({"--print-output-path": folder})
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(folder / "f.txt")
        self.assertIn("ไม่ใช่ไฟล์ธรรมดา", r.stdout)

    def test_excel_in_a_git_working_tree_is_refused(self):
        xls = self._excel("Documents/Repo/summary.xlsx")
        (xls.parent / ".git").mkdir()
        self.fx.make_venv({"--print-output-path": xls})
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(xls)
        self.assertIn("git", r.stdout)

    def test_excel_path_falls_back_to_output_file_in_config_without_venv(self):
        # no .venv -> run_lookup is silent -> output_file from config.json is used
        xls = self._excel()
        self.fx.target.joinpath("config.json").write_text(cfg(output="~/Documents/Out/summary.xlsx"), encoding="utf-8")
        r = self.fx.run([], answers="n\ny\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("อยู่นอกโฟลเดอร์ PDF", r.stdout)
        self.assertGone(xls)

    def test_missing_configured_excel_is_reported(self):
        self.fx.target.joinpath("config.json").write_text(cfg(output="~/Documents/Out/none.xlsx"), encoding="utf-8")
        r = self.fx.run(["--dry-run"], answers="")
        self.assertIn("ไม่มีอยู่แล้ว", r.stdout)
        self.assertRegex(r.stdout, r"none\.xlsx\): ไม่มีอยู่แล้ว")

    def test_excel_inside_the_source_folder_is_handled_with_the_folder(self):
        # default output (inside the source folder): no separate Excel question
        self.fx.make_venv({"--print-output-path": self.fx.source / "สรุปใบเสนอราคา.xlsx"})
        r = self.fx.run(["--dry-run"], answers="")
        self.assertNotIn("อยู่นอกโฟลเดอร์ PDF", r.stdout)

    def test_claude_config_dir_outside_our_folder_is_not_deleted(self):
        # a config pointing at the user's own ~/.claude (or any folder not named .../QuotationSummary/claude-code)
        for bad in ("~/.claude", "~/Documents/stuff", "~/Library/Application Support/Other/claude-code"):
            with self.subTest(claude_dir=bad):
                self.setUp_fresh()
                other = self.fx.home / "Documents" / "stuff"
                other.mkdir(parents=True, exist_ok=True)
                other.joinpath("f.txt").write_text("x")
                (self.fx.home / "Library" / "Application Support" / "Other" / "claude-code").mkdir(parents=True)
                self.fx.target.joinpath("config.json").write_text(cfg(claude_dir=bad), encoding="utf-8")
                r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertExists(self.fx.claude_home / "settings.json", other / "f.txt",
                                  self.fx.home / "Library" / "Application Support" / "Other" / "claude-code")

    def test_claude_config_dir_elsewhere_but_clearly_ours_is_deleted(self):
        ours = self.fx.home / "Documents" / "QuotationSummary" / "claude-code"
        ours.mkdir(parents=True)
        (ours / "settings.json").write_text("{}")
        self.fx.target.joinpath("config.json").write_text(cfg(claude_dir="~/Documents/QuotationSummary/claude-code"), encoding="utf-8")
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(ours)
        self.assertExists(ours.parent)          # only the claude-code folder, not its parent

    def test_symlinked_home_items_are_not_followed(self):
        # ~/Library/Logs/QuotationSummary replaced by a symlink to a folder with real data: must not be deleted
        precious = self.sb.root / "precious"
        precious.mkdir()
        (precious / "data.txt").write_text("x")
        for p in (self.fx.logs / "run.log", self.fx.logs):
            p.unlink() if p.is_file() else p.rmdir()
        os.symlink(str(precious), str(self.fx.logs))
        r = self.fx.run([], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(precious / "data.txt", self.fx.logs)
        self.assertIn("symlink", r.stdout)

    # ---- Claude Code ----
    def test_claude_not_native_is_only_reported_with_docs_link_and_both_brew_commands(self):
        # a plain file at ~/.local/bin/claude and no ~/.local/share/claude: unknown origin -> never deleted
        os.unlink(str(self.fx.claude_bin))
        self.fx.claude_bin.write_text("#!/bin/bash\n")
        for p in sorted(self.fx.claude_data.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.fx.claude_data.rmdir()
        r = self.fx.run([], answers="n\nn\n" + WORD + "\n")           # asked: source folder, Downloads
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://code.claude.com/docs", r.stdout)
        self.assertIn("brew uninstall --cask claude-code ", r.stdout)       # both Homebrew names
        self.assertIn("brew uninstall --cask claude-code@latest", r.stdout)
        self.assertExists(self.fx.claude_bin)

    def test_running_claude_gets_a_warning_to_close_it_first(self):
        # a running claude (pgrep stub) -> warn before offering to delete its program files
        (self.sb.state / "claude-running").write_text("")
        r = self.fx.run(["--dry-run"], answers="")
        self.assertIn("มี claude เปิดอยู่", r.stdout)
        self.assertIn("ปิด Claude Code ทุกหน้าต่าง", r.stdout)
        self.assertExists(self.fx.claude_bin)

    def test_no_running_warning_when_claude_is_closed(self):
        r = self.fx.run(["--dry-run"], answers="")
        self.assertNotIn("มี claude เปิดอยู่", r.stdout)
        self.assertIn("ควรปิด claude ทุกหน้าต่างก่อน", r.stdout)          # the generic reminder is always there

    # ---- ~/.claude is never asked by default, never deleted, and moved aside only with a typed phrase ----
    def test_claude_home_is_never_touched_without_the_flag(self):
        r = self.fx.run([], answers="y\ny\ny\n" + WORD + "\n")      # 3 questions only; nothing about ~/.claude is asked
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("พิมพ์ \"ย้าย .claude\"", r.stdout)
        self.assertExists(self.fx.claude_home / "settings.json", self.fx.claude_json)
        self.assertEqual(self.backups(), [])
        self.assertIn(FLAG, r.stdout)

    def test_claude_home_is_not_asked_when_claude_code_is_kept(self):
        r = self.fx.run([FLAG], answers="n\nn\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่ได้เลือก", r.stdout)
        self.assertExists(self.fx.claude_home / "settings.json", self.fx.claude_json)
        self.assertEqual(self.backups(), [])

    def test_claude_home_needs_its_own_typed_phrase(self):
        for phrase in ("y", "yes", "ลบ", "ใช่", "", "ย้าย", "DELETE", "MOVE .CLAUDE please"):
            with self.subTest(phrase=phrase):
                self.setUp_fresh()
                # source n, Claude Code y, ~/.claude phrase, Downloads n, final word
                r = self.fx.run([FLAG], answers="n\ny\n" + phrase + "\nn\n" + WORD + "\n")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertGone(self.fx.claude_bin, self.fx.claude_data)      # Claude Code itself was chosen
                self.assertExists(self.fx.claude_home / "settings.json", self.fx.claude_json)
                self.assertEqual(self.backups(), [])

    def test_claude_home_is_moved_aside_not_deleted(self):
        (self.fx.claude_home / "CLAUDE.md").write_text("my rules")
        self.fx.claude_json.write_text('{"account": "x"}')
        r = self.fx.run([FLAG], answers="n\ny\n" + CLAUDE_WORD + "\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.claude_home, self.fx.claude_json, self.fx.claude_bin)
        names = self.backups()
        self.assertEqual(len(names), 2, names)
        bak_dir = [self.fx.home / n for n in names if n.startswith(".claude.bak-")][0]
        bak_json = [self.fx.home / n for n in names if n.startswith(".claude.json.bak-")][0]
        self.assertEqual((bak_dir / "CLAUDE.md").read_text(), "my rules")      # nothing lost
        self.assertEqual((bak_dir / "settings.json").read_text(), "{}")
        self.assertEqual(bak_json.read_text(), '{"account": "x"}')
        self.assertIn("ไม่ลบ", r.stdout)

    def test_claude_home_phrase_in_english_and_lower_case_works(self):
        r = self.fx.run([FLAG], answers="n\ny\nmove .claude\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGone(self.fx.claude_home)
        self.assertEqual(len(self.backups()), 2)

    def test_claude_home_warning_lists_what_is_lost(self):
        # the warning names what lives in ~/.claude and the side effects
        r = self.fx.run([FLAG, "--dry-run"], answers="n\ny\nn\nn\n")
        for needle in ("CLAUDE.md", "agents", "skills", "hooks", "plugins", "MCP", "ประวัติ", "login", ".claude.json",
                       "Claude Desktop", "VS Code", "JetBrains", "ปิด Claude Code ทุกตัวก่อน"):
            self.assertIn(needle, r.stdout)

    def test_claude_home_that_is_a_symlink_is_not_moved(self):
        dot = self.sb.root / "dotfiles-claude"
        dot.mkdir()
        (dot / "settings.json").write_text("{}")
        for p in (self.fx.claude_home / "settings.json", self.fx.claude_home):
            p.unlink() if p.is_file() else p.rmdir()
        os.symlink(str(dot), str(self.fx.claude_home))
        r = self.fx.run([FLAG], answers="n\ny\n" + CLAUDE_WORD + "\nn\n" + WORD + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertExists(dot / "settings.json", self.fx.claude_home)
        self.assertEqual([n for n in self.backups() if n.startswith(".claude.bak-")], [])

    def test_output_never_asks_for_claude_home_when_missing(self):
        import shutil
        shutil.rmtree(str(self.fx.claude_home))
        self.fx.claude_json.unlink()
        r = self.fx.run(["--dry-run", FLAG], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"~/\.claude\): ไม่มีอยู่แล้ว")

    def test_every_item_is_listed_with_its_state(self):
        r = self.fx.run(["--dry-run"], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for n in range(1, 9):
            self.assertRegex(r.stdout, r"== \[%d/8\]" % n)
        for needle in ("com.korakod.quotation-summary", SERVICE, API_SERVICE, "Application Support", "ล็อก",
                       "โฟลเดอร์โปรแกรม", "PDF", "Claude Code", "Downloads"):
            self.assertIn(needle, r.stdout)
        self.assertFalse(re.search(r"\bsk-ant|oauth|token=", r.stdout, re.I))


@unittest.skipUnless(IS_MAC, "macOS only (bash 3.2)")
class UninstallGuiAppTests(unittest.TestCase):
    """Item 5 also removes the installed desktop app, but only the exact path ~/Applications/สรุปใบเสนอราคา.app
    (safe_check mode guiapp: a real folder, not a symlink, bundle id dev.korakod.QuotationSummary)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.sb = self.fx.sb
        self.dest = self.sb.gui_dest
        self.apps = self.sb.applications

    def tearDown(self):
        self._tmp.cleanup()

    def go(self, args=(), **kw):
        return self.fx.run(list(args), answers="n\nn\nn\n" + WORD + "\n", **kw)

    def test_app_is_removed_with_the_rest_and_applications_folder_stays(self):
        make_gui_bundle(self.dest)
        other = make_gui_bundle(self.apps / "Other.app", bundle_id="com.example.other")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.dest.exists())
        self.assertTrue(self.apps.is_dir(), "~/Applications itself must stay")
        self.assertTrue(other.is_dir(), "other apps in ~/Applications must stay")
        self.assertFalse(self.fx.target.exists())
        self.assertIn("แอปหน้าต่าง สรุปใบเสนอราคา", r.stdout)

    def test_item_numbering_is_unchanged(self):
        make_gui_bundle(self.dest)
        r = self.fx.run(["--dry-run"], answers="")
        for n in range(1, 9):
            self.assertRegex(r.stdout, r"== \[%d/8\]" % n)
        self.assertNotRegex(r.stdout, r"\[9/")
        self.assertIn(str(self.dest), r.stdout)

    def test_running_app_is_quit_before_it_is_deleted(self):
        make_gui_bundle(self.dest)
        self.sb.state.joinpath("app-running").touch()
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.sb.calls('osascript -e tell application id "%s" to quit' % GUI_ID)), 1)
        self.assertFalse(self.dest.exists())

    def test_a_closed_app_is_not_launched_just_to_quit_it(self):
        make_gui_bundle(self.dest)
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.calls('osascript -e tell application id'), [])

    def test_dry_run_keeps_the_app_and_does_not_touch_it(self):
        make_gui_bundle(self.dest)
        self.sb.state.joinpath("app-running").touch()
        r = self.fx.run(["--dry-run"], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.sb.calls("osascript"), [])

    def test_same_name_but_another_bundle_id_is_refused_and_kept(self):
        make_gui_bundle(self.dest, bundle_id="com.example.other", payload="theirs")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(gui_payload(self.dest), "theirs")
        self.assertIn("bundle id ไม่ใช่", r.stdout)
        self.assertFalse(self.fx.target.exists(), "the rest of the uninstall still happens")

    def test_a_symlink_is_never_followed_or_removed(self):
        real = make_gui_bundle(self.sb.root / "elsewhere" / "Real.app", payload="theirs")
        self.apps.mkdir(parents=True)
        os.symlink(str(real), str(self.dest))
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(gui_payload(real), "theirs")
        self.assertIn("symlink", r.stdout)

    def test_an_app_with_our_id_under_another_name_is_not_touched(self):
        other_name = make_gui_bundle(self.apps / "Renamed.app", payload="mine-renamed")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(other_name.is_dir())

    def test_a_plain_file_with_the_name_is_refused(self):
        self.apps.mkdir(parents=True)
        self.dest.write_text("not a bundle")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.dest.is_file())

    def test_applications_folder_outside_home_is_refused(self):
        outside = self.sb.root / "outside" / GUI_NAME
        make_gui_bundle(outside)
        r = self.go(env_extra={"QS_APPLICATIONS_DIR": str(outside.parent)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(outside.is_dir())
        self.assertIn("อยู่นอกโฟลเดอร์ home", r.stdout)

    def test_missing_app_is_reported_not_an_error(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"แอปหน้าต่าง สรุปใบเสนอราคา: ไม่มีอยู่แล้ว")

    def test_app_in_a_git_work_tree_is_refused(self):
        (self.apps / ".git").mkdir(parents=True)
        make_gui_bundle(self.dest)
        r = self.go()
        self.assertTrue(self.dest.is_dir())
        self.assertIn("git", r.stdout)


if __name__ == "__main__":
    unittest.main()
