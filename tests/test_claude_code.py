"""Tests for the Claude Code backend (headless `claude -p`) with a FAKE runner.

No network, no real `claude` process, no real token: the runner, the `which` lookup and the token are all
fake (token = "sk-ant-oat01-FAKE"). Run:  .venv/bin/python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

from qsum.cache import Cache
from qsum.config import Company, Config, ConfigError, load_config
from qsum.extract_claude import SCHEMA, ClaudeUnavailable
from qsum.extract_claude_code import (CC_PROMPT, SETTINGS_JSON, ClaudeCodeExtractor, RunResult, _kill_group,
                                      build_env, default_runner, get_oauth_token, resolve_claude_path)
from qsum.extract_text import ExtractionError
from qsum.main import main as qsum_main
from qsum.main import process

TOKEN = "sk-ant-oat01-FAKE"
CLAUDE = "/fake/bin/claude"
COMPANY = Company("Company A", "บริษัท ตัวอย่าง เอ จำกัด", "0100000000001", ["ตัวอย่าง เอ"])

GOOD = dict(quote_no="QT0000000001", quote_date="2026-09-08", seller_name="บริษัท ตัวอย่าง เอ จำกัด",
            seller_tax_id="0100000000001", customer_name="บริษัท ลูกค้า หนึ่ง จำกัด",
            items=[dict(description="Non-woven handbag", quantity=1000, unit="PCS.", unit_price=10.0, amount=10000.0)],
            subtotal=10000.0, vat=700.0, total=10700.0)
# What the text fallback would return (used to prove the fallback path runs).
TEXT_RAW = dict(GOOD, quote_no="QT-TEXT")


def ok(record=None, result="done", **extra):
    """A successful `claude -p --output-format json` reply."""
    body = {"type": "result", "subtype": "success", "is_error": False, "result": result,
            "structured_output": GOOD if record is None else record}
    body.update(extra)
    return RunResult(0, json.dumps(body, ensure_ascii=False), "")


def no_structured(subtype="success"):
    return RunResult(0, json.dumps({"type": "result", "subtype": subtype, "is_error": False,
                                    "result": "I could not read it"}), "")


def failed(result, rc=1, status=None, stderr="", subtype="success"):
    """A failed run: is_error true (observed shape: rc=1, subtype 'success', api_error_status)."""
    body = {"type": "result", "subtype": subtype, "is_error": True, "result": result}
    if status:
        body["api_error_status"] = status
    return RunResult(rc, json.dumps(body, ensure_ascii=False), stderr)


class FakeRunner:
    """Replaces the subprocess runner. Records every extraction call (not the --version probe)."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])    # RunResult or Exception instances, popped in order (last repeats)
        self.calls = []                       # dicts: argv, env, cwd, files (names in cwd), pdf (bytes of input.pdf)
        self.version_calls = 0

    def __call__(self, argv, env, cwd, timeout):
        if list(argv[1:]) == ["--version"]:
            self.version_calls += 1
            return RunResult(0, "2.1.316 (Claude Code)", "")
        snap = {"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout, "files": None, "pdf": None}
        if cwd and os.path.isdir(cwd):
            snap["files"] = sorted(os.listdir(cwd))
            pdf = os.path.join(cwd, "input.pdf")
            if os.path.exists(pdf):
                snap["pdf"] = Path(pdf).read_bytes()
        self.calls.append(snap)
        rep = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(rep, Exception):
            raise rep
        return rep


def make_ex(runner, model="m", **kw):
    kw.setdefault("base_env", {"PATH": "/usr/bin", "HOME": "/nonexistent"})
    return ClaudeCodeExtractor(model, TOKEN, CLAUDE, runner=runner, **kw)


def make_cfg(tmp: Path, **kw) -> Config:
    src = tmp / "src"
    src.mkdir(exist_ok=True)
    return Config(source_folder=src, output_file=tmp / "out.xlsx", cache_file=tmp / "cache" / "c.json",
                  companies=[COMPANY], **kw)


def put(cfg, name, data=b"%PDF-fake"):
    p = cfg.source_folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def write_config(tmp: Path, **extra) -> Path:
    data = {"source_folder": str(tmp / "src"), "cache_file": str(tmp / "c" / "c.json"),
            "companies": [{"sheet_name": "A"}]}
    data.update(extra)
    p = tmp / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class RequestTests(unittest.TestCase):
    def test_argv_flags_and_token_only_in_env(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner, model="claude-sonnet-5-5")
            ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        call = runner.calls[0]
        argv = call["argv"]
        self.assertEqual(argv[0], CLAUDE)
        self.assertEqual(argv[1], "-p")
        self.assertTrue(argv[2].startswith("Read ./input.pdf"))        # prompt before the list-style options
        for flag in ("--output-format", "--json-schema", "--append-system-prompt", "--settings",
                     "--tools", "--max-turns", "--no-session-persistence", "--strict-mcp-config", "--model"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        # a bare `Read` allow rule would pre-approve reading ANY file; reads outside the temp folder are
        # refused instead (documented setting, valid JSON, passed inline).
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--allowed-tools", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings, {"permissions": {"blockReadsOutsideWorkingDirectories": True}})
        self.assertEqual(argv[argv.index("--settings") + 1], SETTINGS_JSON)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], CC_PROMPT)
        # The token never appears in the command line, only in the environment.
        self.assertFalse(any(TOKEN in a or "sk-ant" in a for a in argv))
        self.assertEqual(call["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)

    def test_empty_model_omits_model_flag_and_label_says_default(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner, model="")
            ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertNotIn("--model", runner.calls[0]["argv"])
        self.assertEqual(ex.model_label, "claude-code:default")
        self.assertEqual(make_ex(FakeRunner(), model="m").model_label, "claude-code:m")

    def test_build_env_strips_other_credentials_and_sets_token_and_config_dir(self):
        base = {"PATH": "/usr/bin", "KEEP": "1", "ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y",
                "ANTHROPIC_BASE_URL": "http://z", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
                "CLAUDE_CONFIG_DIR": "/somewhere/else", "CLAUDE_CODE_OAUTH_TOKEN": "old"}
        with tempfile.TemporaryDirectory() as t:
            cfgdir = os.path.join(t, "deep", "claude-code")
            env = build_env(base, TOKEN, cfgdir)
            self.assertEqual(oct(stat.S_IMODE(os.stat(cfgdir).st_mode)), "0o700")
            no_dir = build_env(base, TOKEN, None)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], cfgdir)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertEqual(env["KEEP"], "1")
        self.assertFalse([k for k in env if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_")])
        self.assertNotIn("CLAUDE_CONFIG_DIR", no_dir)               # "" in config = leave it unset
        self.assertIn("ANTHROPIC_API_KEY", base)                    # the caller's dict is not modified

    def test_extractor_env_has_no_api_key_even_when_process_env_has_one(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            ex = ClaudeCodeExtractor("m", TOKEN, CLAUDE, runner=runner, config_dir=os.path.join(t, "cc"),
                                     base_env={"ANTHROPIC_API_KEY": "sk-ant-api03-FAKE", "PATH": "/usr/bin"})
            ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertNotIn("ANTHROPIC_API_KEY", runner.calls[0]["env"])

    def test_cwd_holds_only_input_pdf_and_is_removed_afterwards(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            src = put(cfg, "sub folder/ใบเสนอราคา ฉบับที่ 1.pdf", b"%PDF-thai-name")
            make_ex(runner).extract(src)
            self.assertTrue(src.exists())                           # the original is untouched
        call = runner.calls[0]
        self.assertEqual(call["files"], ["input.pdf"])
        self.assertEqual(call["pdf"], b"%PDF-thai-name")
        self.assertNotIn(str(cfg.source_folder), call["cwd"])       # not the source folder
        self.assertFalse(os.path.exists(call["cwd"]))               # temp folder gone after the call


class SuccessAndCacheTests(unittest.TestCase):
    def test_success_is_cached_as_claude_with_label_and_second_run_makes_no_calls(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            put(cfg, "a.pdf")
            _, issues, _, stats = process(cfg, make_ex(runner), log=lambda *_: None, today=date(2026, 11, 9))
            self.assertEqual(stats["claude"], 1)
            entry = list(Cache(cfg.cache_file).entries.values())[0]
            self.assertEqual((entry["method"], entry["model"]), ("claude", "claude-code:m"))
            self.assertEqual(len(runner.calls), 1)
            _, _, _, stats2 = process(cfg, make_ex(runner), log=lambda *_: None)
            self.assertEqual((stats2["cached"], stats2["claude"]), (1, 0))
            self.assertEqual(len(runner.calls), 1)                  # nothing new sent to Claude

    def test_malformed_structured_output_is_extraction_error_and_not_cached(self):
        bad_records = [{k: v for k, v in GOOD.items() if k != "total"}, dict(GOOD, items="x"), dict(GOOD, quote_no=5)]
        for bad in bad_records:
            runner = FakeRunner([ok(bad)])
            with tempfile.TemporaryDirectory() as t:
                cfg = make_cfg(Path(t))
                p = put(cfg, "a.pdf")
                with self.assertRaises(ExtractionError):
                    make_ex(runner).extract(p)
                process(cfg, make_ex(runner), log=lambda *_: None)
                self.assertEqual(Cache(cfg.cache_file).entries, {})

    def test_non_dict_structured_output_is_extraction_error(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ExtractionError):
                make_ex(FakeRunner([ok("a string")])).extract(put(make_cfg(Path(t)), "a.pdf"))

    def test_usage_words_inside_a_successful_reply_do_not_trip_anything(self):
        reply = ok(result="usage limit reached, 401, oauth expired, model not found, unknown option")
        with tempfile.TemporaryDirectory() as t:
            rec = make_ex(FakeRunner([reply])).extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertEqual(rec["quote_no"], "QT0000000001")


class EmptyReplyTests(unittest.TestCase):
    def test_missing_structured_output_is_tried_twice_per_file(self):
        runner = FakeRunner([no_structured()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner)
            with self.assertRaises(ExtractionError):
                ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(ex.empty_in_a_row, 1)

    def test_max_turns_subtype_counts_as_empty_even_with_is_error(self):
        reply = RunResult(1, json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}), "")
        runner = FakeRunner([reply])
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ExtractionError):
                make_ex(runner).extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertEqual(len(runner.calls), 2)

    def test_retry_recovers_when_second_attempt_has_structured_output(self):
        runner = FakeRunner([no_structured(), ok()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner)
            rec = ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual((rec["quote_no"], ex.empty_in_a_row), ("QT0000000001", 0))

    def test_three_empty_files_trip_the_breaker_and_reason_reaches_summary(self):
        runner = FakeRunner([no_structured()])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            for i in range(4):
                put(cfg, "f%d.pdf" % i, b"%%PDF-%d" % i)
            with mock.patch("qsum.main.extract_text", return_value=TEXT_RAW):
                _, issues, _, stats = process(cfg, make_ex(runner), log=lambda *_: None)
        # files 1-2: 2 calls each; file 3 trips the breaker; file 4 is never sent
        self.assertEqual(len(runner.calls), 6)
        self.assertTrue(any("Claude ใช้ไม่ได้" in m and "โครงสร้าง" in m for m in issues))
        self.assertEqual(stats["text"], 4)

    def test_success_resets_the_empty_counter(self):
        runner = FakeRunner([no_structured(), no_structured(), ok(), no_structured()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner)
            p = put(make_cfg(Path(t)), "a.pdf")
            with self.assertRaises(ExtractionError):
                ex.extract(p)
            ex.extract(p)
            self.assertEqual(ex.empty_in_a_row, 0)


class ErrorClassificationTests(unittest.TestCase):
    def _run(self, reply):
        with tempfile.TemporaryDirectory() as t:
            return make_ex(FakeRunner([reply])).extract(put(make_cfg(Path(t)), "a.pdf"))

    def test_usage_limit_is_unavailable(self):
        for reply in (failed("You've hit your limit · resets 5pm"), failed("5-hour usage limit reached"),
                      failed("You've hit your session limit"), failed("You've hit your weekly limit"),
                      failed("You've hit your Sonnet limit", subtype="success"),
                      failed("x", status=429), RunResult(1, "", "Error: rate_limit_error")):
            with self.assertRaises(ClaudeUnavailable) as cm:
                self._run(reply)
            self.assertIn("โควตา Claude", str(cm.exception))

    def test_auth_failure_mentions_setup_token_and_the_command_file(self):
        reply = failed("Failed to authenticate. API Error: 401 OAuth access token is invalid.", status=401)
        with self.assertRaises(ClaudeUnavailable) as cm:
            self._run(reply)
        self.assertIn("claude setup-token", str(cm.exception))
        self.assertIn("set-claude-token.command", str(cm.exception))

    def test_other_documented_auth_messages(self):
        for text in ("Login expired · Please run /login", "OAuth token revoked or expired",
                     "Not logged in · Please run /login", "API Error: 401 Invalid authentication credentials"):
            with self.assertRaises(ClaudeUnavailable) as cm:
                self._run(failed(text))
            self.assertIn("set-claude-token.command", str(cm.exception), text)

    def test_selected_model_issue_is_model_message(self):
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(FakeRunner([failed("There's an issue with the selected model (x). It may not exist.")]), model="x")
            with self.assertRaises(ClaudeUnavailable) as cm:
                ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertIn("claude_model", str(cm.exception))

    def test_overloaded_or_timed_out_api_error_is_per_file(self):
        for text in ("API Error: Repeated 529 Overloaded errors", "Request timed out"):
            with self.assertRaises(ExtractionError):
                self._run(failed(text))

    def test_unrelated_messages_are_not_mistaken_for_quota_auth_or_model_problems(self):
        # these used to match by substring ("limit reached", "429", "401", "expired", "oauth", "model"
        # + "not available") and stopped Claude for the whole run. Each must stay a per-file error.
        cases = [
            failed("Context limit reached · /compact or /clear to continue"),
            failed("Context limit reached · /clear to continue", status=400),
            failed("Prompt is too long: 214290 tokens > 200000 maximum", status=400),
            failed("prompt is too long: 214290 tokens"),
            failed("API Error: 400 invalid_request_error (request_id: req_011CT401abc429xyz)", status=400),
            failed("Request failed (request_id: req_4290401) model overloaded, page not available"),
            failed("The PDF could not be rendered; the page range 4290-4299 is not available to the model"),
            failed("Request too large: the file expired from the upload cache (oauth not involved)"),
            RunResult(1, "", "Error: something 401429 failed while the model output was not available"),
            failed("Image was too large", status=400),
            failed("API Error: Repeated 529 Overloaded errors", status=529),
        ]
        for reply in cases:
            with self.assertRaises(ExtractionError, msg=reply.stdout or reply.stderr):
                self._run(reply)

    def test_api_error_status_decides_before_the_wording(self):
        cases = [(429, "โควตา Claude"), (401, "set-claude-token.command"), (403, "set-claude-token.command"),
                 (404, "claude_model")]
        for status, expect in cases:
            with self.assertRaises(ClaudeUnavailable, msg=status) as cm:
                self._run(failed("something unrelated", status=status))
            self.assertIn(expect, str(cm.exception))
        # a non-listed status is per-file even if the text mentions a quota phrase
        with self.assertRaises(ExtractionError):
            self._run(failed("usage limit reached", status=400))

    def test_documented_texts_without_a_status_are_still_recognised(self):
        quota = ("You've hit your Opus limit", "Request rejected (429)", "spend limit reached",
                 "Server is temporarily limiting requests", "Credit balance is too low", "API Error: 429 too many")
        for text in quota:
            with self.assertRaises(ClaudeUnavailable, msg=text) as cm:
                self._run(RunResult(1, "", text))
            self.assertIn("โควตา Claude", str(cm.exception), text)
        for text in ("OAuth token revoked", "Invalid API key · Fix external API key", "Login expired · Please run /login",
                     "Failed to authenticate. API Error: 401 OAuth access token is invalid."):
            with self.assertRaises(ClaudeUnavailable, msg=text) as cm:
                self._run(RunResult(1, "", text))
            self.assertIn("set-claude-token.command", str(cm.exception), text)

    def test_unknown_option_says_update_claude(self):
        with self.assertRaises(ClaudeUnavailable) as cm:
            self._run(RunResult(1, "", "error: unknown option '--max-turns'"))
        self.assertIn("claude update", str(cm.exception))

    def test_model_not_found_names_the_model(self):
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(FakeRunner([failed("model: claude-typo not found")]), model="claude-typo")
            with self.assertRaises(ClaudeUnavailable) as cm:
                ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertIn("claude-typo", str(cm.exception))

    def test_generic_failure_is_per_file_with_rc_and_redacted_secret(self):
        secret = "sk-ant-oat01-ABCdef_12-xyz"
        with self.assertRaises(ExtractionError) as cm:
            self._run(RunResult(3, "", "boom while using %s and %s" % (secret, TOKEN)))
        msg = str(cm.exception)
        self.assertIn("rc=3", msg)
        self.assertNotIn(secret, msg)
        self.assertNotIn("oat01", msg)
        self.assertIn("sk-ant-***", msg)

    def test_non_json_stdout_is_per_file_and_redacted(self):
        with self.assertRaises(ExtractionError) as cm:
            self._run(RunResult(0, "hello not json sk-ant-api03-SECRETSECRET", ""))
        self.assertIn("ไม่ใช่ JSON", str(cm.exception))
        self.assertNotIn("SECRETSECRET", str(cm.exception))

    def test_auto_mode_falls_back_to_text_on_usage_limit_and_next_run_upgrades(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            for i in range(3):
                put(cfg, "f%d.pdf" % i, b"%%PDF-%d" % i)
            runner = FakeRunner([ok(), failed("You've hit your limit")])   # 1st file ok, then quota is gone
            with mock.patch("qsum.main.extract_text", return_value=TEXT_RAW):
                _, issues, _, stats = process(cfg, make_ex(runner), log=lambda *_: None)
                self.assertEqual((stats["claude"], stats["text"]), (1, 2))
                self.assertEqual(len(runner.calls), 2)                         # 3rd file never sent
                self.assertTrue(any("Claude ใช้ไม่ได้" in m and "โควตา" in m for m in issues))
                # next run: quota is back -> the two text results are re-read with Claude
                runner2 = FakeRunner([ok()])
                _, _, _, stats2 = process(cfg, make_ex(runner2), log=lambda *_: None)
            self.assertEqual((stats2["claude"], stats2["cached"]), (2, 1))
            methods = sorted(e["method"] for e in Cache(cfg.cache_file).entries.values())
            self.assertEqual(methods, ["claude"] * 3)


class TimeoutAndStartupTests(unittest.TestCase):
    def test_first_timeout_is_per_file_second_in_a_row_is_unavailable(self):
        timeout = subprocess.TimeoutExpired("claude", 300)
        runner = FakeRunner([timeout])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            ex = make_ex(runner, timeout_sec=300)
            with self.assertRaises(ExtractionError):
                ex.extract(put(cfg, "a.pdf", b"1"))
            with self.assertRaises(ClaudeUnavailable):
                ex.extract(put(cfg, "b.pdf", b"2"))
        self.assertEqual(len(runner.calls), 2)                       # a timed-out file is not retried

    def test_timeouts_must_be_consecutive(self):
        runner = FakeRunner([subprocess.TimeoutExpired("claude", 1), ok(), subprocess.TimeoutExpired("claude", 1)])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            ex = make_ex(runner)
            with self.assertRaises(ExtractionError):
                ex.extract(put(cfg, "a.pdf", b"1"))
            ex.extract(put(cfg, "b.pdf", b"2"))
            with self.assertRaises(ExtractionError):                 # only 1 in a row again
                ex.extract(put(cfg, "c.pdf", b"3"))

    def test_oserror_when_starting_claude_at_init_and_during_extract(self):
        class Boom(FakeRunner):
            def __call__(self, argv, env, cwd, timeout):
                raise OSError(2, "No such file")
        with self.assertRaises(ClaudeUnavailable):
            make_ex(Boom())
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(FakeRunner([OSError(2, "gone")]))
            with self.assertRaises(ClaudeUnavailable) as cm:
                ex.extract(put(make_cfg(Path(t)), "a.pdf"))
        self.assertIn("ไม่พบโปรแกรม Claude Code", str(cm.exception))

    def test_version_check_failure_and_timeout_at_init(self):
        class Bad(FakeRunner):
            def __call__(self, argv, env, cwd, timeout):
                return RunResult(1, "", "broken")
        class Slow(FakeRunner):
            def __call__(self, argv, env, cwd, timeout):
                raise subprocess.TimeoutExpired(argv, timeout)
        for r in (Bad(), Slow()):
            with self.assertRaises(ClaudeUnavailable):
                make_ex(r)

    def test_rc_127_at_init_hints_at_npm_install(self):
        class Missing(FakeRunner):
            def __call__(self, argv, env, cwd, timeout):
                return RunResult(127, "", "env: node: No such file or directory")
        with self.assertRaises(ClaudeUnavailable) as cm:
            make_ex(Missing())
        self.assertIn("rc=127", str(cm.exception))
        self.assertIn("npm", str(cm.exception))
        class Other(FakeRunner):
            def __call__(self, argv, env, cwd, timeout):
                return RunResult(1, "", "broken")
        with self.assertRaises(ClaudeUnavailable) as cm:
            make_ex(Other())
        self.assertNotIn("npm", str(cm.exception))

    def test_copy_failure_is_per_file_not_a_crash(self):
        # file moved/removed (or disk full) between hashing and copying -> skip this file, keep the run.
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            p = put(cfg, "a.pdf")
            runner = FakeRunner([ok()])
            with mock.patch("qsum.extract_claude_code.shutil.copyfile", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(ExtractionError) as cm:
                    make_ex(runner).extract(p)
            self.assertIn("คัดลอกไฟล์ไม่ได้", str(cm.exception))
            self.assertEqual(len(runner.calls), 0)
            p.unlink()                                         # vanished before stat()
            with self.assertRaises(ExtractionError):
                make_ex(runner).extract(p)

    def test_no_path_or_empty_token_at_init(self):
        with self.assertRaises(ClaudeUnavailable) as cm:
            ClaudeCodeExtractor("m", TOKEN, None, runner=FakeRunner())
        self.assertIn("ไม่พบโปรแกรม Claude Code", str(cm.exception))
        for tok in ("", "   "):
            with self.assertRaises(ClaudeUnavailable) as cm:
                ClaudeCodeExtractor("m", tok, CLAUDE, runner=FakeRunner())
            self.assertIn("set-claude-token.command", str(cm.exception))

    def test_per_run_file_cap_then_text_fallback(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            for i in range(3):
                put(cfg, "f%d.pdf" % i, b"%%PDF-%d" % i)
            with mock.patch("qsum.main.extract_text", return_value=TEXT_RAW):
                _, issues, _, stats = process(cfg, make_ex(runner, max_files=2), log=lambda *_: None)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual((stats["claude"], stats["text"]), (2, 1))
        self.assertTrue(any("claude_code_max_files_per_run" in m for m in issues))

    def test_max_files_zero_means_unlimited(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            ex = make_ex(runner, max_files=0)
            for i in range(3):
                ex.extract(put(cfg, "f%d.pdf" % i, b"%%PDF-%d" % i))
        self.assertEqual(len(runner.calls), 3)

    def test_oversize_pdf_is_per_file_and_not_counted_against_the_cap(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            ex = make_ex(runner, max_files=1, max_pdf_mb=0.0001)
            with self.assertRaises(ExtractionError):
                ex.extract(put(make_cfg(Path(t)), "big.pdf", b"x" * 5000))
        self.assertEqual((len(runner.calls), ex.files_sent), (0, 0))


class LookupTests(unittest.TestCase):
    def _exe(self, d, name="claude", mode=0o755):
        p = Path(d) / name
        p.write_text("#!/bin/sh\n")
        p.chmod(mode)
        return str(p)

    def test_resolve_claude_path_order(self):
        with tempfile.TemporaryDirectory() as t:
            configured = self._exe(t, "mine")
            fixed = self._exe(t, "fixed")
            plain = self._exe(t, "notexec", 0o644)
            nowhere = lambda _: None
            self.assertEqual(resolve_claude_path(configured, which=lambda _: "/on/path/claude"), configured)
            self.assertEqual(resolve_claude_path("", which=lambda _: "/on/path/claude"), "/on/path/claude")
            self.assertEqual(resolve_claude_path(plain, which=lambda _: "/on/path/claude"), "/on/path/claude")
            self.assertEqual(resolve_claude_path("", which=nowhere, candidates=[os.path.join(t, "none"), fixed]), fixed)
            self.assertIsNone(resolve_claude_path("", which=nowhere, candidates=[os.path.join(t, "none")]))
            self.assertIsNone(resolve_claude_path(os.path.join(t, "missing"), which=nowhere, candidates=[]))
            with mock.patch.dict(os.environ, {"HOME": t}):          # "~" in config / candidates is expanded
                self.assertEqual(resolve_claude_path("~/mine", which=nowhere, candidates=[]), configured)

    def test_get_oauth_token_env_first_then_keychain(self):
        with mock.patch("qsum.extract_claude_code.keychain_lookup", return_value="from-keychain") as kc:
            self.assertEqual(get_oauth_token("svc", {"CLAUDE_CODE_OAUTH_TOKEN": " from-env "}), "from-env")
            kc.assert_not_called()
            self.assertEqual(get_oauth_token("svc", {}), "from-keychain")
            kc.assert_called_once_with("svc")


class RealRunnerTests(unittest.TestCase):
    """default_runner against harmless local commands (no claude involved)."""

    def test_captures_output_and_exit_code(self):
        res = default_runner([sys.executable, "-c", "import sys; print('héllo'); sys.stderr.write('e'); sys.exit(3)"],
                             dict(os.environ), None, 30)
        self.assertEqual((res.returncode, res.stdout.strip(), res.stderr), (3, "héllo", "e"))

    def test_timeout_kills_the_whole_process_group_quickly(self):
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            default_runner(["/bin/sh", "-c", "sleep 30 & wait"], dict(os.environ), None, 1)
        self.assertLess(time.time() - start, 15)


class KillGroupTests(unittest.TestCase):
    """the group gets SIGKILL even when the main process already exited on SIGTERM."""

    def test_sigkill_is_sent_even_if_main_process_exits_on_sigterm(self):
        proc = mock.Mock(pid=4242)
        proc.wait.return_value = 0                            # main process "exits" right after SIGTERM
        sent = []
        with mock.patch("qsum.extract_claude_code.os.killpg", side_effect=lambda pid, sig: sent.append((pid, sig))):
            _kill_group(proc)
        import signal
        self.assertEqual(sent, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])

    def test_missing_group_is_tolerated(self):
        proc = mock.Mock(pid=4242)
        proc.wait.return_value = 0
        with mock.patch("qsum.extract_claude_code.os.killpg", side_effect=ProcessLookupError):
            _kill_group(proc)                                  # must not raise

    def test_stubborn_grandchild_is_killed_after_main_process_exits(self):
        # main shell exits on SIGTERM; its background child ignores SIGTERM and would survive without SIGKILL.
        pidfile = tempfile.NamedTemporaryFile(delete=False)
        pidfile.close()
        try:
            script = "trap '' TERM; sleep 60 & echo $! > %s; trap 'exit 0' TERM; wait" % pidfile.name
            proc = subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(50):
                if Path(pidfile.name).read_text().strip():
                    break
                time.sleep(0.1)
            child = int(Path(pidfile.name).read_text().strip())
            _kill_group(proc)
            time.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)                              # grandchild is gone
        finally:
            os.unlink(pidfile.name)

    def test_keyboard_interrupt_while_waiting_kills_the_group(self):
        with mock.patch("qsum.extract_claude_code._kill_group") as kill, \
                mock.patch("qsum.extract_claude_code.subprocess.Popen") as popen:
            popen.return_value.communicate.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                default_runner(["x"], {}, None, 5)
        kill.assert_called_once_with(popen.return_value)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QS_CLAUDE_BACKEND", None)
            cfg = load_config(str(write_config(Path(t))))
        self.assertEqual(cfg.claude_backend, "claude_code")
        self.assertEqual(cfg.claude_code_path, "")
        self.assertEqual(cfg.claude_code_keychain_service, "quotation-summary-claude-code")
        self.assertEqual(cfg.claude_code_timeout_sec, 300)
        self.assertEqual(cfg.claude_code_max_files_per_run, 50)
        self.assertEqual(cfg.claude_code_config_dir, "~/Library/Application Support/QuotationSummary/claude-code")

    def test_invalid_values_raise_config_error(self):
        bad = [dict(claude_backend="gpt"), dict(claude_code_timeout_sec=0), dict(claude_code_timeout_sec=-5),
               dict(claude_code_max_files_per_run=-1), dict(claude_code_timeout_sec="abc")]
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QS_CLAUDE_BACKEND", None)
            for extra in bad:
                with self.assertRaises(ConfigError, msg=str(extra)):
                    load_config(str(write_config(Path(t), **extra)))

    def test_zero_max_files_and_empty_config_dir_are_allowed(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QS_CLAUDE_BACKEND", None)
            cfg = load_config(str(write_config(Path(t), claude_code_max_files_per_run=0, claude_code_config_dir="")))
        self.assertEqual((cfg.claude_code_max_files_per_run, cfg.claude_code_config_dir), (0, ""))

    def test_env_overrides_backend_and_is_validated(self):
        with tempfile.TemporaryDirectory() as t:
            path = str(write_config(Path(t), claude_backend="claude_code"))
            with mock.patch.dict(os.environ, {"QS_CLAUDE_BACKEND": "api"}):
                self.assertEqual(load_config(path).claude_backend, "api")
            with mock.patch.dict(os.environ, {"QS_CLAUDE_BACKEND": "nope"}):
                with self.assertRaises(ConfigError):
                    load_config(path)


class MainTests(unittest.TestCase):
    def _run_main(self, cfg_path, patches):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = qsum_main(["--config", str(cfg_path)])
        return rc, out.getvalue(), err.getvalue()

    def _summary_text(self, xlsx):
        ws = load_workbook(xlsx)["สรุป"]
        return "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)

    def test_auto_mode_without_claude_code_keeps_going_and_tells_why_in_summary_sheet(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "src").mkdir()
            (tmp / "src" / "a.pdf").write_bytes(b"%PDF-1")
            cfg_path = write_config(tmp, extractor="auto")
            rc, out, _ = self._run_main(cfg_path, [
                mock.patch("qsum.main.get_oauth_token", return_value=TOKEN),
                mock.patch("qsum.main.resolve_claude_path", return_value=None),
                mock.patch("qsum.main.extract_text", return_value=TEXT_RAW)])
            self.assertEqual(rc, 0)
            self.assertIn("ตัวอ่าน: pdftotext", out)
            text = self._summary_text(tmp / "src" / "สรุปใบเสนอราคา.xlsx")
            self.assertIn("ไม่พบโปรแกรม Claude Code", text)
            self.assertIn("เมื่อตั้งค่า Claude ได้", text)

    def test_strict_claude_mode_without_claude_code_exits_1(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "src").mkdir()
            cfg_path = write_config(tmp, extractor="claude")
            rc, _, err = self._run_main(cfg_path, [
                mock.patch("qsum.main.get_oauth_token", return_value=TOKEN),
                mock.patch("qsum.main.resolve_claude_path", return_value=None)])
        self.assertEqual(rc, 1)
        self.assertIn("ไม่พบโปรแกรม Claude Code", err)

    def test_missing_token_is_reported_with_the_command_file(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "src").mkdir()
            rc, _, err = self._run_main(write_config(tmp, extractor="claude"), [
                mock.patch("qsum.main.get_oauth_token", return_value=None),
                mock.patch("qsum.main.resolve_claude_path", return_value=CLAUDE),
                mock.patch("qsum.main.ClaudeCodeExtractor",
                           side_effect=lambda *a, **k: ClaudeCodeExtractor(*a, runner=FakeRunner(), **k))])
        self.assertEqual(rc, 1)
        self.assertIn("set-claude-token.command", err)

    def test_claude_code_backend_end_to_end_with_fake_runner(self):
        runner = FakeRunner([ok()])
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "src").mkdir()
            (tmp / "src" / "a.pdf").write_bytes(b"%PDF-1")
            cfg_path = write_config(tmp, extractor="auto", claude_model="m",
                                    claude_code_config_dir=str(tmp / "cc"))
            rc, out, _ = self._run_main(cfg_path, [
                mock.patch("qsum.main.get_oauth_token", return_value=TOKEN),
                mock.patch("qsum.main.resolve_claude_path", return_value=CLAUDE),
                mock.patch("qsum.main.ClaudeCodeExtractor",
                           side_effect=lambda *a, **k: ClaudeCodeExtractor(*a, runner=runner, **k))])
            self.assertEqual(rc, 0)
            self.assertIn("ตัวอ่าน: Claude Code (แพ็กเกจรายเดือน, รุ่น m)", out)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0]["env"]["CLAUDE_CONFIG_DIR"], str(tmp / "cc"))
            self.assertNotIn(TOKEN, out)                              # the token is never printed

    def test_api_backend_still_used_when_selected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "src").mkdir()
            rc, out, err = self._run_main(write_config(tmp, extractor="claude", claude_backend="api"), [
                mock.patch("qsum.main.get_api_key", return_value=None)])
        self.assertEqual(rc, 1)
        self.assertIn("API key", err)

    def test_print_claude_token_service(self):
        with tempfile.TemporaryDirectory() as t:
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = qsum_main(["--config", str(write_config(Path(t))), "--print-claude-token-service"])
        self.assertEqual((rc, out_buf.getvalue().strip()), (0, "quotation-summary-claude-code"))


if __name__ == "__main__":
    unittest.main()
