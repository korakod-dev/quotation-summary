"""Claude Code backend: read each PDF with headless `claude -p` on the user's Claude subscription.

Why: the weekly job then spends the subscription quota instead of a pay-per-use API key.

How it works (one `claude -p` process per PDF):
  * auth = OAuth token from `claude setup-token`, kept in the macOS Keychain (written by
    scripts/set-claude-token.command) and handed to the child process ONLY through the environment
    variable CLAUDE_CODE_OAUTH_TOKEN. It is never put on the command line, never logged, never cached.
  * the child runs in a fresh temporary folder that contains only a copy of the PDF (`input.pdf`), with
    an isolated CLAUDE_CONFIG_DIR, so it cannot see the user's projects, memory, CLAUDE.md or settings.
  * only the Read tool exists (`--tools Read`) and file reads outside the temporary folder are refused in every
    permission mode (`--settings` with permissions.blockReadsOutsideWorkingDirectories = true). We deliberately do
    NOT pass `--allowedTools Read`: per https://code.claude.com/docs/en/permissions a bare `Read` rule "matches all
    file reads" (any file on the machine). Reads inside the working folder need no allow rule.
  * the answer comes back as schema-validated JSON (`--json-schema`), which we validate again with the same
    checks as the API backend before it can be cached.

Flags and env vars used (all checked on 2026-11-09 against `claude --help` of Claude Code 2.1.316 and the
official docs https://code.claude.com/docs/en/cli-reference , /headless , /env-vars , /authentication):
  -p, --output-format json, --json-schema, --append-system-prompt, --settings (JSON string), --tools, --max-turns,
  --no-session-persistence, --strict-mcp-config, --model;
  CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CONFIG_DIR, DISABLE_AUTOUPDATER, CLAUDE_CODE_DISABLE_AUTO_MEMORY.
Not used because not documented / not needed: --bare (docs: it does NOT read CLAUDE_CODE_OAUTH_TOKEN),
--max-budget-usd (meaningless on a subscription), --permission-mode.
Observed locally (Claude Code 2.1.316, fake token, no quota used): an auth failure exits rc=1 with JSON
{"is_error": true, "subtype": "success", "api_error_status": 401, "result": "Failed to authenticate. ..."};
the same call with `--tools Read` + `--settings '{"permissions":{...}}'` is accepted (same 401, no option error).
Errors are therefore classified from `api_error_status` first, then from full phrases in `result` / stderr,
never from `subtype`. NOT verified live: that blockReadsOutsideWorkingDirectories really refuses an outside read
(needs a real token; the documented behaviour is relied on).
NOT verified live with a real subscription token (none is handled during development).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence

from .extract_claude import (EXTRACTION_RULES, MAX_EMPTY_IN_A_ROW, SCHEMA, ClaudeUnavailable,
                             keychain_lookup, validate_tool_input)
from .extract_text import ExtractionError

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
MAX_TURNS = 4                 # Read (1) + StructuredOutput (1) + slack for one retry of the schema check
_ATTEMPTS_PER_FILE = 2        # total tries per file when the reply has no structured output
_VERSION_TIMEOUT_SEC = 30
_CLASSIFY_CHARS = 1000        # how much of the error text is searched for known phrases
_SNIPPET_CHARS = 300          # how much of the error text is shown to the user

# Fixed install locations tried when `claude` is not on PATH (launchd starts jobs with a minimal PATH).
_CANDIDATE_PATHS = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                    "~/.claude/local/claude")

CC_PROMPT = ("You extract structured data from Thai sales quotation PDFs (ใบเสนอราคา).\n"
             "The PDF is the file ./input.pdf in the current folder: open it with the Read tool and use no "
             "other tool. Then return the result as structured output. Rules:\n" + EXTRACTION_RULES)
# Inline settings for the child (documented: permissions.blockReadsOutsideWorkingDirectories).
SETTINGS_JSON = json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}}, separators=(",", ":"))
USER_PROMPT = "Read ./input.pdf with the Read tool and extract the quotation."

# Classification of a FAILED run (rc != 0 or is_error). Never applied to a successful reply.
# 1) `api_error_status` (HTTP status Claude Code reports for API failures) decides when it is present.
#    Any other status (e.g. 400 "Prompt is too long") is a per-file problem and its text is NOT searched, so
#    "Context limit reached" / "prompt is too long: 214290 tokens" can never be mistaken for a quota problem.
_USAGE_STATUS = (429,)
_AUTH_STATUS = (401, 403)
_MODEL_STATUS = (404,)
# 2) Without a status (plain stderr text, old versions) the first _CLASSIFY_CHARS of the text are matched with
#    FULL phrases and word boundaries, taken from https://code.claude.com/docs/en/errors (checked 2026-11-09):
#    "You've hit your session / weekly / Opus limit", "Request rejected (429)", "OAuth token revoked or expired",
#    "Login expired", "Not logged in", "There's an issue with the selected model". Bare words such as "limit
#    reached", "expired", "oauth", "401" or "429" are NOT used (they also occur in unrelated messages).
_UNKNOWN_OPTION_RE = re.compile(r"\bunknown (?:option|argument)\b")
_USAGE_RE = re.compile(
    r"\bhit your (?:[\w' ]{1,40} )?limit\b|\b(?:usage|session|weekly|opus|sonnet|spend) limit\b"
    r"|\btemporarily limiting requests\b|\bcredit balance is too low\b|\brate_limit_error\b"
    r"|\brequest rejected \(429\)|\bapi error:? 429\b")
_AUTH_RE = re.compile(
    r"\binvalid api key\b|\bnot logged in\b|\blogin (?:expired|was rejected|not accepted)\b"
    r"|\boauth (?:access )?token (?:revoked|expired|is invalid|refresh failed)\b|\bfailed to authenticate\b"
    r"|\binvalid authentication credentials\b|\binvalid auth token\b|\bplease run /login\b"
    r"|\bclaude subscription access\b|\bapi error:? 401\b")
_MODEL_RE = re.compile(
    r"\bissue with the selected model\b|\bis not a recognized model\b|\bmodel:? '?[\w.\-/\[\]]+'? not found\b"
    r"|\bnot_found_error\b|\bis not available with the claude\b|\bdoes not support this model\b"
    r"|\bmodel is restricted by\b|\binvalid model\b")
# Result subtypes that mean "ran, but produced no answer" (treated like an empty reply: retry, then breaker).
_EMPTY_SUBTYPES = ("error_max_turns", "error_max_structured_output_retries")

_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


class RunResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# Runner(argv, env, cwd, timeout_sec) -> RunResult. May raise subprocess.TimeoutExpired or OSError.
# Tests inject a fake; production uses default_runner.
Runner = Callable[[Sequence[str], Dict[str, str], Optional[str], float], RunResult]


def get_oauth_token(keychain_service: str, environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Token lookup: env CLAUDE_CODE_OAUTH_TOKEN first (handy by hand), else the Keychain item that
    scripts/set-claude-token.command created (launchd jobs do not inherit shell variables)."""
    env = os.environ if environ is None else environ
    tok = env.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    return keychain_lookup(keychain_service)


def resolve_claude_path(configured: str = "", which: Callable[[str], Optional[str]] = shutil.which,
                        candidates: Optional[Sequence[str]] = None) -> Optional[str]:
    """Find the `claude` executable: config value -> PATH -> well-known install folders. None if not found."""
    def is_exec(p: str) -> bool:
        return os.path.isfile(p) and os.access(p, os.X_OK)

    if configured and configured.strip():
        c = os.path.expanduser(configured.strip())
        if is_exec(c):
            return c
    found = which("claude")
    if found:
        return found
    for cand in (_CANDIDATE_PATHS if candidates is None else candidates):
        c = os.path.expanduser(cand)
        if is_exec(c):
            return c
    return None


def build_env(base: Dict[str, str], token: str, config_dir: Optional[str] = None) -> Dict[str, str]:
    """Environment for the child `claude`: nothing that could redirect it to another account/provider."""
    env = dict(base)
    # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN would override the subscription token (docs: authentication
    # precedence), CLAUDE_CODE_USE_* would switch to Bedrock/Vertex/Foundry, ANTHROPIC_PROFILE / federation
    # variables rank above it as well: drop everything with these prefixes.
    for k in list(env):
        if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_"):
            del env[k]
    env.pop("CLAUDE_CONFIG_DIR", None)
    if config_dir:
        d = Path(os.path.expanduser(config_dir))
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
        env["CLAUDE_CONFIG_DIR"] = str(d)
    env[TOKEN_ENV] = token
    env["DISABLE_AUTOUPDATER"] = "1"              # no background self-update in the middle of a weekly run
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"  # do not create/load auto-memory files for these runs
    return env


def _kill_group(proc: "subprocess.Popen[str]") -> None:
    """Stop the child and everything it started (it runs in its own session/process group).
    SIGTERM first, then ALWAYS SIGKILL the group: the main process may exit on SIGTERM while a grandchild
    ignores it, so "main process gone" does not mean "group gone"."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)    # no-op (ProcessLookupError) when the whole group is already gone
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
        proc.communicate(timeout=2)            # close the pipes
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def default_runner(argv: Sequence[str], env: Dict[str, str], cwd: Optional[str], timeout: float) -> RunResult:
    """Run the command in its own process group with stdin closed; kill the whole group on timeout or on any
    other interruption (KeyboardInterrupt, SystemExit, ...) while waiting."""
    proc = subprocess.Popen(list(argv), env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        _kill_group(proc)
        raise
    return RunResult(proc.returncode, out or "", err or "")


class ClaudeCodeExtractor:
    """Same interface as ClaudeExtractor: extract(path) -> raw record dict, or raises
    ClaudeUnavailable (stop using Claude for this run) / ExtractionError (this file only)."""

    def __init__(self, model: str, token: str, claude_path: Optional[str], timeout_sec: float = 300,
                 max_files: int = 50, config_dir: Optional[str] = None, max_pdf_mb: float = 30.0,
                 runner: Optional[Runner] = None, base_env: Optional[Dict[str, str]] = None):
        self.model = model or ""
        self.model_label = "claude-code:%s" % (self.model or "default")   # recorded in the cache entry
        self.timeout_sec = timeout_sec
        self.max_files = max_files
        self.max_bytes = int(max_pdf_mb * 1024 * 1024)
        self.runner: Runner = runner or default_runner
        self.empty_in_a_row = 0       # files in a row that got no structured answer (run-wide breaker)
        self.timeouts_in_a_row = 0    # files in a row that timed out
        self.files_sent = 0           # files sent to Claude this run (for the per-run cap)
        if not claude_path:
            raise ClaudeUnavailable("ไม่พบโปรแกรม Claude Code (claude) ติดตั้งตาม README แล้วรันใหม่ "
                                    "หรือใส่ claude_code_path ใน config.json")
        if not token or not token.strip():
            raise ClaudeUnavailable("ไม่พบ token ของ Claude รัน 'claude setup-token' แล้วดับเบิลคลิก "
                                    "scripts/set-claude-token.command เพื่อเก็บ token")
        self.claude_path = claude_path
        self._token = token.strip()
        try:
            self._env = build_env(dict(os.environ if base_env is None else base_env), self._token, config_dir)
        except OSError as e:
            raise ClaudeUnavailable("สร้างโฟลเดอร์เก็บค่าของ Claude Code ไม่ได้ (%s): %s"
                                    % (config_dir, e.strerror or e))
        # Fail early (and clearly) if the binary cannot even start.
        try:
            res = self.runner([self.claude_path, "--version"], self._env, tempfile.gettempdir(), _VERSION_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClaudeUnavailable("เรียก Claude Code (%s) ไม่ได้: %s ลองรัน 'claude --version' ใน Terminal"
                                    % (claude_path, type(e).__name__))
        if res.returncode != 0:
            hint = ""
            if res.returncode == 127:    # shell "command not found": typically a `#!/usr/bin/env node` npm install
                hint = (" (ถ้าติดตั้ง Claude Code ผ่าน npm งานตั้งเวลาจะหา node ไม่เจอ "
                        "ให้ติดตั้งใหม่ด้วยตัวติดตั้งตาม README)")
            raise ClaudeUnavailable("เรียก Claude Code (%s) ไม่ได้ (rc=%d)%s ลองรัน 'claude --version' ใน Terminal"
                                    % (claude_path, res.returncode, hint))

    # ------------------------------------------------------------------ helpers
    def _argv(self) -> List[str]:
        argv = [self.claude_path, "-p", USER_PROMPT,     # prompt first: --tools takes a list
                "--output-format", "json",
                "--json-schema", json.dumps(SCHEMA, ensure_ascii=False),
                "--append-system-prompt", CC_PROMPT,
                "--tools", "Read",                        # only the Read tool exists for this run
                # Refuse reads outside the temp folder in every permission mode. No --allowedTools: a bare
                # `Read` allow rule would approve reading ANY file (docs: "Matches all file reads").
                "--settings", SETTINGS_JSON,
                "--max-turns", str(MAX_TURNS),
                "--no-session-persistence",               # do not keep transcripts of customer PDFs on disk
                "--strict-mcp-config"]                    # no MCP servers (none configured: --mcp-config unused)
        if self.model:
            argv += ["--model", self.model]
        return argv                                       # the token is NOT here: it travels in the env

    def _redact(self, text: str) -> str:
        text = _SECRET_RE.sub("sk-ant-***", text or "")
        if self._token:
            text = text.replace(self._token, "***")
        return text

    def _fail(self, res: RunResult, parsed: Optional[Dict[str, Any]]) -> Exception:
        """Map a failed run (rc != 0 or is_error) to ClaudeUnavailable (whole run) or ExtractionError (file)."""
        result_txt = str(parsed.get("result") or "") if parsed else ""
        stdout_txt = "" if parsed else res.stdout     # unparsable stdout may still carry the message
        status = parsed.get("api_error_status") if parsed else None
        text = self._redact(" ".join(p for p in (result_txt, res.stderr or "", stdout_txt) if p.strip()).strip())
        hay = text[:_CLASSIFY_CHARS].lower()
        snippet = text[:_SNIPPET_CHARS].replace("\n", " ")
        if isinstance(status, bool) or not isinstance(status, int):
            status = None
        usage = lambda: ClaudeUnavailable(               # noqa: E731
            "โควตา Claude ของแพ็กเกจรายเดือนเต็มแล้ว (จำกัดราย 5 ชม./7 วัน) "
            "ไฟล์ที่เหลือใช้ pdftotext และจะอ่านใหม่รอบหน้า: %s" % snippet)
        auth = lambda: ClaudeUnavailable(                # noqa: E731
            "token ของ Claude ใช้ไม่ได้หรือหมดอายุ รัน 'claude setup-token' (ล็อกอินด้วยบัญชีของคุณ) "
            "แล้วดับเบิลคลิก scripts/set-claude-token.command เพื่อใส่ token ใหม่: %s" % snippet)
        model = lambda: ClaudeUnavailable(               # noqa: E731
            "ไม่พบรุ่น Claude '%s' หรือแพ็กเกจนี้ใช้รุ่นนี้ไม่ได้ (แก้ claude_model ใน config.json "
            "หรือเว้นว่างให้ใช้รุ่นตั้งต้นของ Claude Code): %s" % (self.model or "(ค่าตั้งต้น)", snippet))
        if status is not None:                           # the API said why: trust the status, not the wording
            if status in _USAGE_STATUS:
                return usage()
            if status in _AUTH_STATUS:
                return auth()
            if status in _MODEL_STATUS:
                return model()
        else:                                            # no status (stderr / old versions): full phrases only
            if _UNKNOWN_OPTION_RE.search(hay):
                return ClaudeUnavailable("Claude Code เวอร์ชันเก่าเกินไป รัน 'claude update' ใน Terminal แล้วรันใหม่: %s" % snippet)
            if _USAGE_RE.search(hay):
                return usage()
            if _AUTH_RE.search(hay):
                return auth()
            if _MODEL_RE.search(hay):
                return model()
        return ExtractionError("เรียก Claude Code ไม่สำเร็จ (rc=%d): %s" % (res.returncode, snippet))

    def _run_once(self, cwd: str) -> RunResult:
        try:
            res = self.runner(self._argv(), self._env, cwd, self.timeout_sec)
        except subprocess.TimeoutExpired:
            self.timeouts_in_a_row += 1
            if self.timeouts_in_a_row >= 2:
                raise ClaudeUnavailable("Claude Code ไม่ตอบภายใน %d วินาที 2 ไฟล์ติดกัน (อินเทอร์เน็ตช้าหรือ Claude ติดขัด) "
                                        "ไฟล์ที่เหลือใช้ pdftotext และจะอ่านใหม่รอบหน้า" % self.timeout_sec)
            raise ExtractionError("Claude Code ไม่ตอบภายใน %d วินาที (claude_code_timeout_sec)" % self.timeout_sec)
        except OSError:
            raise ClaudeUnavailable("ไม่พบโปรแกรม Claude Code (claude) ติดตั้งตาม README แล้วรันใหม่ "
                                    "หรือใส่ claude_code_path ใน config.json")
        self.timeouts_in_a_row = 0
        return res

    @staticmethod
    def _parse(stdout: str) -> Optional[Dict[str, Any]]:
        """The `result` object from --output-format json (a single object; a list is tolerated)."""
        try:
            obj = json.loads(stdout)
        except (ValueError, TypeError):
            return None
        if isinstance(obj, list):
            obj = next((o for o in reversed(obj) if isinstance(o, dict) and o.get("type") == "result"), None)
        return obj if isinstance(obj, dict) else None

    # ------------------------------------------------------------------ main entry
    def extract(self, path: Path) -> Dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError as e:     # file vanished since the scan: this file only
            raise ExtractionError("อ่านไฟล์ไม่ได้: %s" % (e.strerror or e))
        if size > self.max_bytes:
            raise ExtractionError("ไฟล์ใหญ่เกิน %.0f MB ส่งให้ Claude ไม่ได้" % (self.max_bytes / 1048576))
        if self.max_files and self.files_sent >= self.max_files:
            raise ClaudeUnavailable(
                "อ่านด้วย Claude ครบ %d ไฟล์ต่อรอบแล้ว (claude_code_max_files_per_run) "
                "ไฟล์ที่เหลือใช้ pdftotext ก่อน และจะอ่านด้วย Claude รอบหน้า" % self.max_files)
        self.files_sent += 1

        # Fresh folder holding ONLY the PDF copy (neutral name: the real name may contain odd characters or
        # hints); removed afterwards. Claude Code never sees the source folder.
        with tempfile.TemporaryDirectory(prefix="qsum-cc-") as work:
            try:
                shutil.copyfile(str(path), os.path.join(work, "input.pdf"))
            except OSError as e:     # file moved/removed since hashing, temp disk full, ...: this file only
                raise ExtractionError("คัดลอกไฟล์ไม่ได้: %s" % (e.strerror or e))
            for _ in range(_ATTEMPTS_PER_FILE):
                res = self._run_once(work)
                parsed = self._parse(res.stdout)
                subtype = parsed.get("subtype") if parsed else None
                if subtype in _EMPTY_SUBTYPES:
                    continue                                   # ran out of turns/retries: no answer, try again
                if res.returncode != 0 or (parsed and parsed.get("is_error")):
                    raise self._fail(res, parsed)
                if parsed is None:
                    raise ExtractionError("คำตอบจาก Claude Code ไม่ใช่ JSON: %s"
                                          % self._redact(res.stdout)[:_SNIPPET_CHARS].replace("\n", " "))
                found = parsed.get("structured_output")
                if found is None:
                    continue                                   # no structured output (e.g. declined): try again
                if not isinstance(found, dict):
                    raise ExtractionError("ผลจาก Claude ผิดรูปแบบ: structured_output ต้องเป็นอ็อบเจกต์")
                validate_tool_input(found)                     # malformed output raises ExtractionError, never cached
                self.empty_in_a_row = 0
                return found
        # No structured answer after the retry. Several files in a row means the setup does not work.
        self.empty_in_a_row += 1
        if self.empty_in_a_row >= MAX_EMPTY_IN_A_ROW:
            raise ClaudeUnavailable(
                "Claude Code ไม่ส่งผลลัพธ์แบบโครงสร้างกลับมา %d ไฟล์ติดกัน (ลองเปลี่ยนหรือเว้นว่าง claude_model "
                "ใน config.json และรัน 'claude update')" % self.empty_in_a_row)
        raise ExtractionError("Claude Code ไม่ได้ส่งผลลัพธ์แบบโครงสร้างกลับมา (ลองแล้ว %d ครั้ง)" % _ATTEMPTS_PER_FILE)
