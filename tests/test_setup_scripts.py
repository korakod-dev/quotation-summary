"""Tests for the one-double-click install path: scripts/make-package.sh, scripts/setup-all.command,
scripts/doctor.command (and the thin launchers).

Everything runs against a throw-away sandbox: a fake $HOME, and PATH stubs for every command that would touch
the real machine (security, launchctl, claude, curl = the Claude installer, open, brew, xcode-select, sleep).
The stubs only append their arguments to a log file, so the tests can assert exactly what would have been run.
No real token, Keychain, launchd, Homebrew or Claude installer is involved (the fake `security` never sees a token:
the real one prompts on the terminal by itself, see scripts/set-claude-token.command).
The real scripts/install.sh is tested separately (InstallScriptTests) with a fake python3 whose pip only logs.

Run:  .venv/bin/python -m unittest tests.test_setup_scripts -v      (macOS only: uses /bin/bash 3.2, xattr, ditto)
"""
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
BASH = "/bin/bash"
IS_MAC = sys.platform == "darwin" and os.path.exists(BASH)

LAUNCHER_SETUP = "ติดตั้ง.command"
LAUNCHER_DOCTOR = "ตรวจระบบ.command"
LAUNCHER_UNINSTALL = "ถอนการติดตั้ง.command"   # tested in tests/test_uninstall_all.py
SERVICE = "quotation-summary-claude-code"
QUARANTINE = "0081;00000000;Safari;"

# ---- stub scripts ----------------------------------------------------------------------------------------------
STUB_SECURITY = """#!/bin/bash
echo "security $*" >> "$STUB_LOG"
case "$1" in
  find-generic-password) [ -f "$STUB_STATE/token" ] && { echo 'keychain: "stub item"'; exit 0; }; exit 44;;
  add-generic-password)  touch "$STUB_STATE/token"; exit 0;;
esac
exit 0
"""
STUB_LAUNCHCTL = """#!/bin/bash
echo "launchctl $*" >> "$STUB_LOG"
case "$1" in
  print)     [ -f "$STUB_STATE/registered" ] && { printf '\\tstate = not running\\n'; exit 0; }; exit 113;;
  kickstart) mkdir -p "$HOME/Library/Logs/QuotationSummary"; echo "stub job output line" >> "$HOME/Library/Logs/QuotationSummary/run.log"; exit 0;;
esac
exit 0
"""
STUB_LOGONLY = """#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
"""
STUB_XCODE = """#!/bin/bash
echo "xcode-select $*" >> "$STUB_LOG"
[ "$1" = "-p" ] && { [ -f "$STUB_STATE/no-clt" ] && exit 2; echo /Library/Developer/CommandLineTools; exit 0; }
exit 0
"""
STUB_CLAUDE = """#!/bin/bash
echo "claude $*" >> "$STUB_LOG"
case "$1" in
  --version)    echo "2.1.0 (Claude Code) [stub]"; exit 0;;
  setup-token)  [ -f "$STUB_STATE/setup-token-fail" ] && { echo "[stub] login failed"; exit 1; }
                echo "[stub] setup-token ran (no real token exists)"; exit 0;;
esac
exit 0
"""
# `curl -fsSL -o FILE https://claude.ai/install.sh` stub: writes a tiny "installer" that drops the fake claude in
# ~/.local/bin into FILE. With state file "curl-fail" it simulates a dropped connection: a half-written FILE
# (which would touch "partial-installer-ran" if somebody ran it) and curl's failure status.
STUB_CURL = """#!/bin/bash
echo "curl $*" >> "$STUB_LOG"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
if [ -f "$STUB_STATE/curl-fail" ]; then
  [ -n "$out" ] && echo 'touch "$STUB_STATE/partial-installer-ran"' > "$out"
  exit 22
fi
cat > "${out:-/dev/stdout}" <<'INSTALLER'
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude" <<'CLAUDE'
%s
CLAUDE
chmod +x "$HOME/.local/bin/claude"
echo "[stub installer] fake claude installed"
INSTALLER
""" % STUB_CLAUDE.rstrip("\n")
# `brew install poppler` stub: also drops a fake pdftotext into the fake bin folder.
STUB_BREW = """#!/bin/bash
echo "brew $*" >> "$STUB_LOG"
if [ "$1" = "install" ]; then
  mkdir -p "$STUB_FAKEBIN"; printf '#!/bin/bash\\nexit 0\\n' > "$STUB_FAKEBIN/pdftotext"; chmod +x "$STUB_FAKEBIN/pdftotext"
fi
exit 0
"""
# Replacement for scripts/install.sh inside the sandbox copy: no pip / launchd, just the visible effects.
STUB_INSTALL = """#!/bin/bash
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "install.sh QS_SETUP_ALL=${QS_SETUP_ALL:-} app=$APP_DIR" >> "$STUB_LOG"
mkdir -p "$APP_DIR/.venv/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/QuotationSummary"
cat > "$APP_DIR/.venv/bin/python" <<'PYSTUB'
#!/bin/bash
[ "${3:-}" = "--version" ] && exit 0
echo "STUB-PYTHON-RUN $*"
exit 0
PYSTUB
chmod +x "$APP_DIR/.venv/bin/python"
cat > "$HOME/Library/LaunchAgents/com.korakod.quotation-summary.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>ProgramArguments</key><array><string>/bin/bash</string><string>$APP_DIR/scripts/run.sh</string></array></dict></plist>
PLIST
touch "$STUB_STATE/registered"
exit 0
"""

# ---- stubs for the desktop-app steps (osascript / codesign / file / sysctl) -------------------------------------
GUI_ID = "dev.korakod.QuotationSummary"
GUI_NAME = "สรุปใบเสนอราคา.app"
# `application id X is running` answers true while $STUB_STATE/app-running exists; `to quit` removes that file.
STUB_OSASCRIPT = """#!/bin/bash
echo "osascript $*" >> "$STUB_LOG"
case "$*" in
  *"is running"*) [ -f "$STUB_STATE/app-running" ] && echo true || echo false;;
  *"to quit"*)    [ -f "$STUB_STATE/app-wont-quit" ] || rm -f "$STUB_STATE/app-running";;
esac
exit 0
"""
# codesign stub: --verify fails while $STUB_STATE/codesign-fail exists (no real signature is checked in the sandbox).
STUB_CODESIGN = """#!/bin/bash
echo "codesign $*" >> "$STUB_LOG"
[ -f "$STUB_STATE/codesign-fail" ] && exit 1
exit 0
"""
# `file -b BINARY`: arm64 by default; state file file-x86 -> x86_64, file-script -> a shell script (wrong kind).
STUB_FILE = """#!/bin/bash
echo "file $*" >> "$STUB_LOG"
if [ -f "$STUB_STATE/file-x86" ]; then echo "Mach-O 64-bit executable x86_64"
elif [ -f "$STUB_STATE/file-script" ]; then echo "Bourne-Again shell script text executable, ASCII text"
else echo "Mach-O 64-bit executable arm64"; fi
exit 0
"""
# `sysctl -n hw.optional.arm64`: 1 (Apple Silicon) by default, 0 with state file intel.
STUB_SYSCTL = """#!/bin/bash
echo "sysctl $*" >> "$STUB_LOG"
[ -f "$STUB_STATE/intel" ] && echo 0 || echo 1
exit 0
"""
# Stand-in for scripts/build-app.sh (hook QS_BUILD_APP_SCRIPT of make-package.sh): writes a fake bundle into --out DIR.
# State file build-fail -> exits 1 (and builds nothing).
STUB_BUILD_APP = """#!/bin/bash
echo "build-app $*" >> "$STUB_LOG"
[ -f "$STUB_STATE/build-fail" ] && { echo "stub build failed" >&2; exit 1; }
out=""; prev=""
for a in "$@"; do [ "$prev" = "--out" ] && out="$a"; prev="$a"; done
b="$out/QuotationSummary.app"
mkdir -p "$b/Contents/MacOS"
cat > "$b/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.korakod.QuotationSummary</string></dict></plist>
PLIST
printf '#!/bin/bash\n# built by stub\n' > "$b/Contents/MacOS/QuotationSummary"
chmod +x "$b/Contents/MacOS/QuotationSummary"
echo "$b"
"""

FAKE_CONFIG = """{
  "source_folder": "~/QuotationSummary/ใบเสนอราคาทดสอบ",
  "claude_backend": "claude_code",
  "claude_code_keychain_service": "%s",
  "companies": [{"sheet_name": "T1", "display_name": "บริษัท ทดสอบ จำกัด", "tax_id": "0100000000009", "aliases": ["ทดสอบ"]}]
}
""" % SERVICE


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_gui_bundle(path: Path, bundle_id: str = GUI_ID, payload: str = "v1") -> Path:
    """A minimal fake desktop-app bundle with a REAL Info.plist (the scripts read it with plutil)."""
    (path / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    with open(str(path / "Contents" / "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "QuotationSummary",
                       "CFBundlePackageType": "APPL"}, f)
    _write_exec(path / "Contents" / "MacOS" / "QuotationSummary", "#!/bin/bash\n# payload %s\n" % payload)
    return path


def gui_payload(bundle: Path) -> str:
    """Which payload a fake bundle holds ("v1", "old", ...)."""
    return (bundle / "Contents" / "MacOS" / "QuotationSummary").read_text(encoding="utf-8").split("payload ")[-1].strip()


class Sandbox:
    """A fake machine: fake HOME, stub commands on PATH, a log of every stub call."""

    def __init__(self, root: Path, with_claude=False, with_brew=True):
        self.root = Path(os.path.realpath(str(root)))
        self.home = self.root / "home"
        self.stubs = self.root / "stubs"
        self.state = self.root / "state"
        self.fakebin = self.root / "fakebin"
        self.log = self.root / "stubs.log"
        self.downloads = self.home / "Downloads"
        self.target = self.home / "QuotationSummary" / "quotation-summary"
        self.applications = self.home / "Applications"
        self.gui_dest = self.applications / GUI_NAME
        for d in (self.home, self.stubs, self.state, self.fakebin, self.downloads, self.root / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        self.log.write_text("")
        for name, text in (("security", STUB_SECURITY), ("launchctl", STUB_LAUNCHCTL), ("curl", STUB_CURL),
                           ("open", STUB_LOGONLY), ("sleep", STUB_LOGONLY), ("clear", STUB_LOGONLY),
                           ("xcode-select", STUB_XCODE), ("osascript", STUB_OSASCRIPT), ("codesign", STUB_CODESIGN),
                           ("file", STUB_FILE), ("sysctl", STUB_SYSCTL)):
            _write_exec(self.stubs / name, text)
        self.build_stub = self.root / "stub-build-app.sh"
        _write_exec(self.build_stub, STUB_BUILD_APP)
        if with_brew:
            _write_exec(self.stubs / "brew", STUB_BREW)
        if with_claude:
            _write_exec(self.stubs / "claude", STUB_CLAUDE)
        os.symlink(sys.executable, str(self.stubs / "python3"))   # make-package.sh uses python3 from PATH

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "USER": "tester",
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(self.root / "tmp"),
            # Only the stubs + system tools: the real brew / claude / pdftotext must NOT be visible.
            "PATH": "%s:/usr/bin:/bin:/usr/sbin:/sbin" % self.stubs,
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_FAKEBIN": str(self.fakebin),
            "QS_BIN_DIRS": "%s:%s" % (self.home / ".local" / "bin", self.fakebin),
            "QS_PYTHON_CANDIDATES": sys.executable,
            "QS_FORCE_CLEAR": "1",   # stdout is a pipe here: still run the (stub) `clear` so the tests can see it
            "QS_APPLICATIONS_DIR": str(self.applications),   # where the desktop app is installed (never the real one)
            "QS_BUILD_APP_SCRIPT": str(self.build_stub),     # make-package.sh builds the fake bundle, not the real app
        }
        env.update(extra)
        return env

    def make_app(self, where: Path, config=FAKE_CONFIG) -> Path:
        """Copy the app (without .venv, real config.json, tests) to `where`, install the stub install.sh.
        .git and .gitignore are skipped: this repo root IS the app folder, and the sandbox builds its own git work tree."""
        shutil.copytree(str(APP), str(where), ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "config.json", "samples.local.json", "tests", "*.pyc", ".DS_Store",
            "gui", "build", ".build", ".git", ".gitignore"))
        if config is not None:
            (where / "config.json").write_text(config, encoding="utf-8")
        _write_exec(where / "scripts" / "install.sh", STUB_INSTALL)
        return where

    def run(self, script: Path, args=(), answers="", env_extra=None, timeout=120, cwd=None):
        return subprocess.run([BASH, str(script)] + list(args), input=answers, capture_output=True, text=True,
                              env=self.env(**(env_extra or {})), timeout=timeout, cwd=cwd)

    def add_gui_bundle(self, app: Path, **kw) -> Path:
        """Put a fake built desktop app into the package folder, where setup-all.command looks for it."""
        return make_gui_bundle(app / "build" / "QuotationSummary.app", **kw)

    def log_lines(self):
        return self.log.read_text().splitlines()

    def calls(self, prefix):
        return [l for l in self.log_lines() if l.startswith(prefix)]


def listing(app_dir: Path):
    """All shell scripts of the feature (for bash -n)."""
    names = [LAUNCHER_SETUP, LAUNCHER_DOCTOR, LAUNCHER_UNINSTALL]
    names += ["scripts/" + n for n in sorted(os.listdir(str(app_dir / "scripts"))) if n.endswith((".sh", ".command"))]
    return names


@unittest.skipUnless(IS_MAC, "macOS only (bash 3.2, xattr, ditto)")
class SyntaxTests(unittest.TestCase):
    def test_bash_syntax_of_all_scripts(self):
        for name in listing(APP):
            r = subprocess.run([BASH, "-n", str(APP / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "%s: %s" % (name, r.stderr))

    def test_scripts_are_executable_and_launchers_exist(self):
        for name in (LAUNCHER_SETUP, LAUNCHER_DOCTOR, LAUNCHER_UNINSTALL, "scripts/setup-all.command",
                     "scripts/doctor.command", "scripts/uninstall-all.command", "scripts/make-package.sh"):
            self.assertTrue(os.access(str(APP / name), os.X_OK), name + " must be executable")


HAS_GIT = shutil.which("git") is not None


@unittest.skipUnless(IS_MAC and HAS_GIT, "macOS + git only")
class MakePackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Sandbox(Path(self._tmp.name))
        self.work = self.sb.root / "work"
        self.app = self.sb.make_app(self.work / "quotation-summary")
        # The Swift sources are tracked by git in the real repo but must never travel with the package.
        (self.app / "gui" / "Sources").mkdir(parents=True)
        (self.app / "gui" / "Package.swift").write_text("// swift package\n")
        (self.app / "gui" / "Sources" / "main.swift").write_text("print(1)\n")
        # make-package.sh packs what git tracks: the sandbox is a git work tree with the app files added
        self.git("init", "-q")
        self.git("add", "-A")
        # local files that are NOT tracked and must NOT end up in the zip (allow-list, not deny-list)
        (self.app / ".venv" / "bin").mkdir(parents=True)
        (self.app / ".venv" / "bin" / "python").write_text("x")
        (self.app / "qsum" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (self.app / "qsum" / "__pycache__" / "a.pyc").write_bytes(b"x")
        (self.app / ".DS_Store").write_bytes(b"x")
        (self.app / "tests").mkdir(exist_ok=True)
        (self.app / "tests" / "samples.local.json").write_text("{}")
        self.out = self.sb.root / "out" / "pkg.zip"
        self.script = self.app / "scripts" / "make-package.sh"

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.work)] + list(args), check=True, capture_output=True, env=self.sb.env())

    def tracked(self):
        out = subprocess.run(["git", "-C", str(self.app), "-c", "core.quotepath=off", "ls-files"], check=True,
                             capture_output=True, text=True, env=self.sb.env()).stdout
        return set("quotation-summary/" + n for n in out.splitlines())

    BUNDLE_FILES = {"quotation-summary/build/QuotationSummary.app/Contents/Info.plist",
                    "quotation-summary/build/QuotationSummary.app/Contents/MacOS/QuotationSummary"}

    def zip_names(self, path=None):
        """Names in the zip, Thai ones decoded (Info-ZIP does not set the UTF-8 flag, Python then reads cp437)."""
        names = set()
        for info in zipfile.ZipFile(str(path or self.out)).infolist():
            names.add(info.filename if info.flag_bits & 0x800 else info.filename.encode("cp437").decode("utf-8"))
        return names

    def test_zip_has_config_and_no_junk(self):
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = zipfile.ZipFile(str(self.out)).namelist()
        self.assertIn("quotation-summary/config.json", names)
        self.assertIn("quotation-summary/scripts/setup-all.command", names)
        self.assertFalse([n for n in names if ".venv" in n or "__pycache__" in n or ".DS_Store" in n
                          or n.endswith("samples.local.json") or n.endswith(".pyc")], names)
        self.assertEqual(len([n for n in names if n.count("/") == 1 and n.endswith(".command")]), 3)  # 3 launchers

    def test_zip_is_exactly_tracked_files_plus_config(self):
        """local data files lying in the folder must not travel with the zip."""
        for junk in ("config.json.bak-20261109-101010", "report.xlsx", "customer.pdf", ".env", ".env.local", "run.log",
                     "notes.txt"):
            (self.app / junk).write_text("local data")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Info-ZIP does not set the UTF-8 flag, so Python reads Thai names as cp437: undo that for the comparison
        # (Finder/ditto extraction of the Thai names is covered by the extraction test below).
        names = set()
        for info in zipfile.ZipFile(str(self.out)).infolist():
            names.add(info.filename if info.flag_bits & 0x800 else info.filename.encode("cp437").decode("utf-8"))
        # tracked files minus the Swift sources and the repo-only files (docs/, .gitignore), plus config.json and
        # exactly the built app's files
        no_gui = set(n for n in self.tracked() if not n.startswith("quotation-summary/gui/")
                     and not n.startswith("quotation-summary/docs/") and n != "quotation-summary/.gitignore"
                     and n != "quotation-summary/scripts/build-app.sh")
        self.assertEqual(names, no_gui | {"quotation-summary/config.json"} | self.BUNDLE_FILES)

    def test_tracked_customer_data_file_is_still_dropped(self):
        self.git("add", "quotation-summary/tests/samples.local.json")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("quotation-summary/tests/samples.local.json", zipfile.ZipFile(str(self.out)).namelist())

    def test_required_file_not_in_git_is_refused(self):
        self.git("rm", "--cached", "-q", "quotation-summary/" + LAUNCHER_SETUP)
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("git add", r.stderr)

    def test_uninstaller_files_are_in_the_zip_and_required(self):
        """The uninstaller launcher + script travel with the zip (git ls-files allow-list) and must be tracked."""
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = set()
        for info in zipfile.ZipFile(str(self.out)).infolist():
            names.add(info.filename if info.flag_bits & 0x800 else info.filename.encode("cp437").decode("utf-8"))
        self.assertIn("quotation-summary/" + LAUNCHER_UNINSTALL, names)
        self.assertIn("quotation-summary/scripts/uninstall-all.command", names)
        self.git("rm", "--cached", "-q", "quotation-summary/scripts/uninstall-all.command")
        r = self.sb.run(self.script, [str(self.sb.root / "out" / "again.zip")])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git add", r.stderr)

    def test_app_folder_outside_git_is_refused(self):
        other = self.sb.make_app(self.sb.root / "plain" / "quotation-summary")   # no git here
        r = self.sb.run(other / "scripts" / "make-package.sh", [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_zip_extracts_like_finder_with_thai_names_and_exec_bits(self):
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = self.sb.root / "unz"
        dest.mkdir()
        subprocess.run(["ditto", "-x", "-k", str(self.out), str(dest)], check=True)   # engine behind Finder's unzip
        launcher = dest / "quotation-summary" / LAUNCHER_SETUP
        self.assertTrue(launcher.is_file(), "Thai launcher name lost: %s" % os.listdir(str(dest / "quotation-summary")))
        self.assertTrue(os.access(str(launcher), os.X_OK))
        self.assertTrue(os.access(str(dest / "quotation-summary" / "scripts" / "setup-all.command"), os.X_OK))
        self.assertEqual((dest / "quotation-summary" / "config.json").read_text(encoding="utf-8"), FAKE_CONFIG)

    def test_missing_config_is_refused_unless_allowed(self):
        (self.app / "config.json").unlink()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())
        r = self.sb.run(self.script, [str(self.out), "--allow-no-config"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("quotation-summary/config.json", zipfile.ZipFile(str(self.out)).namelist())

    def test_invalid_config_json_is_refused(self):
        (self.app / "config.json").write_text("{ not json", encoding="utf-8")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)

    def test_output_inside_app_folder_is_refused(self):
        r = self.sb.run(self.script, [str(self.app / "x.zip")])
        self.assertNotEqual(r.returncode, 0)

    # -- never write the zip (it holds the real config.json) anywhere a `git add -A` could pick it up --------
    def test_output_elsewhere_in_git_work_tree_is_refused(self):
        target = self.work / "qs.zip"        # inside the repo, but NOT inside the app folder
        r = self.sb.run(self.script, [str(target)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(target.exists())
        self.assertIn("git", r.stderr)

    def test_relative_output_into_work_tree_is_refused(self):
        r = self.sb.run(self.script, ["../../qs.zip"], cwd=str(self.app / "scripts"))   # = work/qs.zip
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.work / "qs.zip").exists())

    def test_output_through_symlink_into_work_tree_is_refused(self):
        os.symlink(str(self.work), str(self.sb.root / "link"))
        r = self.sb.run(self.script, [str(self.sb.root / "link" / "qs.zip")])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.work / "qs.zip").exists())

    def test_output_in_new_folder_inside_work_tree_is_refused_without_creating_it(self):
        r = self.sb.run(self.script, [str(self.work / "newdir" / "qs.zip")])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.work / "newdir").exists())

    def test_output_that_is_a_symlink_is_refused(self):
        (self.sb.root / "out").mkdir()
        precious = self.sb.root / "precious.txt"
        precious.write_text("keep")
        os.symlink(str(precious), str(self.out))
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(precious.read_text(), "keep")

    # -- only ever overwrite a .zip ---------------------------------------------------------------------------
    def test_output_must_end_with_zip_and_existing_file_survives(self):
        (self.sb.root / "out").mkdir()
        for name in ("important.txt", "pkg.zip.bak", "pkg"):
            victim = self.sb.root / "out" / name
            victim.write_text("do not delete")
            r = self.sb.run(self.script, [str(victim)])
            self.assertNotEqual(r.returncode, 0, name)
            self.assertEqual(victim.read_text(), "do not delete", name)
            self.assertIn(".zip", r.stderr)


    # -- desktop app in the package ----------------------------------------------------------------------------
    def test_app_is_rebuilt_and_only_its_files_are_packed(self):
        stale = self.app / "build" / "QuotationSummary.app" / "stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("from an earlier build")
        (self.app / "build" / "other.txt").write_text("not part of the app")
        (self.app / "build" / "QuotationSummary.app" / ".DS_Store").write_bytes(b"x")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = self.zip_names()
        self.assertTrue(self.BUNDLE_FILES <= names, names)
        self.assertEqual([n for n in names if "/build/" in n and n not in self.BUNDLE_FILES], [])
        self.assertFalse(stale.exists(), "the old bundle must be deleted before the rebuild")
        self.assertEqual(len(self.sb.calls("build-app ")), 1)
        self.assertIn("--out %s" % (self.app / "build"), self.sb.calls("build-app ")[0])

    def test_swift_sources_are_never_packed(self):
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([n for n in self.zip_names() if "/gui" in n or n.endswith(".swift")], [])

    def test_repo_only_files_are_not_packed_but_license_is(self):
        """docs/ (README screenshots) and .gitignore are tracked in the public repo but must not reach the zip."""
        (self.app / "docs" / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.app / "docs" / "screenshots" / "main-idle.png").write_bytes(b"png")
        (self.app / ".gitignore").write_text(".venv/\n")
        (self.app / "LICENSE").write_text("license text\n")
        self.git("add", "-A")
        for n in ("docs/screenshots/main-idle.png", ".gitignore", "LICENSE"):
            self.assertIn("quotation-summary/" + n, self.tracked())
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = self.zip_names()
        self.assertEqual([n for n in names if "/docs/" in n or n.endswith(".gitignore")], [])
        self.assertIn("quotation-summary/LICENSE", names)

    def test_build_app_script_is_an_owner_tool_and_not_packed_or_required(self):
        """build-app.sh needs gui/, which is never packed, so it must not ship (and must not be required)."""
        self.assertIn("quotation-summary/scripts/build-app.sh", self.tracked())      # tracked, yet not packed
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("quotation-summary/scripts/build-app.sh", self.zip_names())
        # an untracked / missing build-app.sh no longer blocks the package
        self.git("rm", "--cached", "-q", "quotation-summary/scripts/build-app.sh")
        self.out.unlink()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_tracked_file_under_build_is_not_packed(self):
        (self.app / "build").mkdir()
        (self.app / "build" / "junk.txt").write_text("tracked by mistake")
        self.git("add", "-f", "quotation-summary/build/junk.txt")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("quotation-summary/build/junk.txt", self.zip_names())

    def test_no_app_flag_skips_the_build_and_leaves_build_out(self):
        old = self.app / "build" / "QuotationSummary.app"
        make_gui_bundle(old)
        r = self.sb.run(self.script, [str(self.out), "--no-app"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.calls("build-app "), [])
        self.assertEqual([n for n in self.zip_names() if "/build/" in n], [], "an old bundle must not slip in")
        self.assertEqual(self.sb.calls("codesign"), [], "no app, nothing to verify")

    def test_failing_build_aborts_without_a_zip(self):
        self.sb.state.joinpath("build-fail").touch()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("สร้างแอปหน้าต่างไม่สำเร็จ", r.stderr)

    def test_failing_build_does_not_fall_back_to_an_old_bundle(self):
        make_gui_bundle(self.app / "build" / "QuotationSummary.app", payload="old")
        self.sb.state.joinpath("build-fail").touch()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_build_that_leaves_no_binary_is_refused(self):
        _write_exec(self.sb.build_stub, "#!/bin/bash\nexit 0\n")      # claims success, builds nothing
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_app_survives_extraction_with_exec_bit_and_reports_signature_and_arch(self):
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ลายเซ็นของแอป", r.stdout)
        self.assertIn("ชนิดของแอป: Mach-O 64-bit executable arm64", r.stdout)
        # the post-extract check ran codesign --verify --strict on the EXTRACTED bundle
        verifies = self.sb.calls("codesign --verify --strict")
        self.assertEqual(len(verifies), 1, verifies)
        self.assertIn("QuotationSummary.app", verifies[0])
        dest = self.sb.root / "unz"
        dest.mkdir()
        subprocess.run(["ditto", "-x", "-k", str(self.out), str(dest)], check=True)
        binary = dest / "quotation-summary" / "build" / "QuotationSummary.app" / "Contents" / "MacOS" / "QuotationSummary"
        self.assertTrue(os.access(str(binary), os.X_OK))

    def test_bad_signature_after_extraction_refuses_the_package(self):
        self.sb.state.joinpath("codesign-fail").touch()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ลายเซ็นของแอป", r.stdout + r.stderr)
        self.assertIn("อย่าส่งไฟล์นี้", r.stderr)

    def test_app_that_is_not_a_mach_o_binary_is_refused(self):
        self.sb.state.joinpath("file-script").touch()
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("อย่าส่งไฟล์นี้", r.stderr)

    def test_status_sh_must_be_tracked(self):
        self.git("rm", "--cached", "-q", "quotation-summary/scripts/status.sh")
        r = self.sb.run(self.script, [str(self.out)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("git add", r.stderr)

    def test_status_sh_ships_executable(self):
        r = self.sb.run(self.script, [str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dest = self.sb.root / "unz2"
        dest.mkdir()
        subprocess.run(["ditto", "-x", "-k", str(self.out), str(dest)], check=True)
        self.assertTrue(os.access(str(dest / "quotation-summary" / "scripts" / "status.sh"), os.X_OK))


@unittest.skipUnless(IS_MAC, "macOS only")
class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def sandbox(self, **kw):
        return Sandbox(Path(self._tmp.name), **kw)

    # -- dry run -------------------------------------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        sb = self.sandbox()
        app = sb.make_app(sb.downloads / "quotation-summary")
        # the quarantine flag must survive a dry run
        subprocess.run(["xattr", "-w", "com.apple.quarantine", QUARANTINE, str(app / "scripts" / "run.sh")], check=True)
        before = (app / "config.json").read_text(encoding="utf-8")
        r = sb.run(app / "scripts" / "setup-all.command", ["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ทดลอง", r.stdout)
        self.assertTrue(app.is_dir(), "dry run must not move the folder")
        self.assertFalse(sb.target.exists())
        self.assertEqual((app / "config.json").read_text(encoding="utf-8"), before)
        self.assertFalse((app / ".venv").exists())
        xa = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(app / "scripts" / "run.sh")],
                            capture_output=True, text=True)
        self.assertEqual(xa.returncode, 0, "dry run must not remove the quarantine flag")
        # only read-only stub calls are allowed
        for bad in ("add-generic-password", "install.sh", "curl", "setup-token", "kickstart", "open ", "brew install",
                    "xcode-select --install"):
            self.assertFalse([l for l in sb.log_lines() if bad in l], "dry run executed: %s" % bad)
        self.assertFalse((sb.home / "QuotationSummary").exists())

    def test_dry_run_reports_missing_python_without_installing(self):
        sb = self.sandbox()
        sb.state.joinpath("no-clt").touch()
        app = sb.make_app(sb.home / "QuotationSummary" / "quotation-summary")
        r = sb.run(app / "scripts" / "setup-all.command", ["--dry-run"],
                   env_extra={"QS_PYTHON_CANDIDATES": "/nonexistent/python3"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ไม่พบ Python", r.stdout)
        self.assertFalse(sb.calls("xcode-select --install"))

    def test_developer_checkout_is_not_moved(self):
        sb = self.sandbox()
        (sb.root / "devrepo" / ".git").mkdir(parents=True)
        app = sb.make_app(sb.root / "devrepo" / "apps" / "quotation-summary")
        r = sb.run(app / "scripts" / "setup-all.command", ["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("โฟลเดอร์นักพัฒนา", r.stdout)
        self.assertTrue(app.is_dir())

    def test_developer_checkout_real_run_stops_before_touching_launchd(self):
        """a double-click inside the dev repo must not re-register the live job to point at the repo."""
        sb = self.sandbox(with_claude=True)
        (sb.root / "devrepo" / ".git").mkdir(parents=True)
        app = sb.make_app(sb.root / "devrepo" / "apps" / "quotation-summary")
        r = sb.run(app / "scripts" / "setup-all.command")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("หยุดก่อนติดตั้ง", r.stdout)
        self.assertFalse(sb.calls("install.sh"))
        self.assertFalse(sb.calls("launchctl"))
        self.assertFalse(sb.calls("security"))
        self.assertFalse((sb.home / "Library" / "LaunchAgents").exists())

    def test_developer_checkout_continues_only_with_explicit_flag(self):
        sb = self.sandbox(with_brew=False, with_claude=True)
        sb.state.joinpath("token").touch()
        (sb.root / "devrepo" / ".git").mkdir(parents=True)
        app = sb.make_app(sb.root / "devrepo" / "apps" / "quotation-summary")
        r = sb.run(app / "scripts" / "setup-all.command", ["--allow-dev-checkout"], answers="n\nn\n")
        self.assertIn("--allow-dev-checkout", r.stdout)
        self.assertEqual(sb.calls("install.sh"), ["install.sh QS_SETUP_ALL=1 app=%s" % app])

    # -- full flow with stubs ------------------------------------------------------------------------------
    def test_full_flow_from_downloads_then_idempotent_rerun(self):
        sb = self.sandbox()
        app = sb.make_app(sb.downloads / "quotation-summary")
        # as downloaded: quarantine flag on the folder and a script, and the exec bit lost on the scripts
        subprocess.run(["xattr", "-w", "com.apple.quarantine", QUARANTINE, str(app)], check=True)
        subprocess.run(["xattr", "-w", "com.apple.quarantine", QUARANTINE, str(app / "scripts" / "run.sh")], check=True)
        for f in list((app / "scripts").iterdir()) + [app / LAUNCHER_SETUP]:
            f.chmod(0o644)
        # answers: move=y, poppler=y, install claude=y, sign in=y, copied token=y, open privacy page=y
        r = sb.run(app / "scripts" / "setup-all.command", answers="y\ny\ny\ny\ny\ny\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # 1. moved out of Downloads, flags fixed
        self.assertFalse(app.exists())
        self.assertTrue((sb.target / "scripts" / "setup-all.command").is_file())
        for f in (sb.target / LAUNCHER_SETUP, sb.target / "scripts" / "run.sh", sb.target / "scripts" / "doctor.command"):
            self.assertTrue(os.access(str(f), os.X_OK), "%s lost +x" % f.name)
        q = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(sb.target / "scripts" / "run.sh")],
                           capture_output=True, text=True)
        self.assertNotEqual(q.returncode, 0, "quarantine flag must be removed")
        # 2-6. each external action happened once, in the right form
        self.assertEqual(len(sb.calls("brew install poppler")), 1)
        self.assertEqual(sb.calls("install.sh"), ["install.sh QS_SETUP_ALL=1 app=%s" % sb.target])
        # the installer is downloaded to a temp file first (never piped into bash), and the temp file is cleaned up
        curl = sb.calls("curl")
        self.assertEqual(len(curl), 1, curl)
        self.assertTrue(curl[0].startswith("curl -fsSL -o ") and curl[0].endswith(" https://claude.ai/install.sh"), curl)
        self.assertEqual(list((sb.root / "tmp").glob("qs-claude-install.*")), [], "temp installer must be removed")
        self.assertEqual(len(sb.calls("claude setup-token")), 1)
        src = sb.home / "QuotationSummary" / "ใบเสนอราคาทดสอบ"
        self.assertTrue(src.is_dir(), "PDF source folder from config.json must be created")
        self.assertIn("open %s" % src, sb.log_lines())
        self.assertIn("open https://claude.ai/settings/data-privacy-controls", sb.log_lines())
        # token: same method as set-claude-token.command, `-w` last, nothing secret on the command line
        adds = sb.calls("security add-generic-password")
        self.assertEqual(adds, ["security add-generic-password -a tester -s %s -U -w" % SERVICE])
        self.assertNotIn("sk-ant", sb.log.read_text())
        self.assertNotIn("sk-ant", r.stdout.replace("sk-ant-oat01-)", ""))   # only the hint prefix may be printed
        # after the token is stored the screen + scrollback are cleared, and the user is told to close the window
        self.assertEqual(len(sb.calls("clear")), 1)
        self.assertIn("\033[3J", r.stdout)
        self.assertIn("ปิดหน้าต่าง Terminal", r.stdout)
        self.assertLess(r.stdout.index("setup-token ran"), r.stdout.index("\033[3J"))   # cleared AFTER the token was shown
        # no PDFs yet: the first test run is skipped with an explanation
        self.assertFalse(sb.calls("launchctl kickstart"))
        # doctor summary at the end
        self.assertIn("ไม่ผ่าน 0", r.stdout)
        self.assertIn("พร้อมใช้งาน", r.stdout)

        # ---- rerun from the installed location with a PDF present: everything is skipped, first test runs ----
        (src / "a.pdf").write_bytes(b"%PDF-1.4 stub")
        before_log = len(sb.log_lines())
        # keep token, privacy page: empty answer = the default, which must be "no" on a re-run, run test
        r2 = sb.run(sb.target / "scripts" / "setup-all.command", answers="n\n\ny\n")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        new_calls = sb.log_lines()[before_log:]
        self.assertFalse([l for l in new_calls if l.startswith("curl") or l.startswith("brew install")
                          or "setup-token" in l or "add-generic-password" in l], new_calls)
        self.assertNotIn("open https://claude.ai/settings/data-privacy-controls", new_calls)   # default is n now
        self.assertFalse([l for l in new_calls if l.startswith("clear")], "nothing secret was shown: no clear")
        self.assertRegex(r2.stdout, r"เปิดหน้านี้ในเบราว์เซอร์ตอนนี้ไหม\? \[y/N\]")
        self.assertIn("STUB-PYTHON-RUN -m qsum", r2.stdout)                       # run.sh executed once
        self.assertEqual(len([l for l in new_calls if l.startswith("launchctl kickstart")]), 1)
        self.assertIn("stub job output line", r2.stdout)                           # tail of run.log shown
        self.assertIn("ไม่ผ่าน 0", r2.stdout)

    def test_missing_python_offers_xcode_select_and_stops(self):
        sb = self.sandbox()
        sb.state.joinpath("no-clt").touch()
        app = sb.make_app(sb.target)
        r = sb.run(app / "scripts" / "setup-all.command", answers="y\n",
                   env_extra={"QS_PYTHON_CANDIDATES": "/nonexistent/python3"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(len(sb.calls("xcode-select --install")), 1)
        self.assertFalse(sb.calls("install.sh"), "must stop before installing")

    def test_no_brew_prints_note_and_does_not_install_homebrew(self):
        sb = self.sandbox(with_brew=False, with_claude=True)
        sb.state.joinpath("token").touch()      # token already stored: the only questions left are "replace token?" and "open privacy page?"
        app = sb.make_app(sb.target)
        r = sb.run(app / "scripts" / "setup-all.command", answers="n\nn\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่มี Homebrew", r.stdout)
        self.assertFalse(sb.calls("brew"))
        self.assertFalse(sb.calls("curl"), "claude exists: installer must not run")

    def test_update_keeps_existing_config_and_venv(self):
        sb = self.sandbox(with_claude=True)
        sb.state.joinpath("token").touch()
        installed = sb.make_app(sb.target, config=FAKE_CONFIG)
        (installed / ".venv").mkdir()
        (installed / ".venv" / "marker").write_text("keep")
        new_config = FAKE_CONFIG.replace("T1", "T9")
        app = sb.make_app(sb.downloads / "quotation-summary", config=new_config)
        (app / "qsum" / "NEWFILE.py").write_text("# new code")
        # answers: update=y, replace config=n, install poppler=n, replace token=n, open privacy page=n
        r = sb.run(app / "scripts" / "setup-all.command", answers="y\nn\nn\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((sb.target / "config.json").read_text(encoding="utf-8"), FAKE_CONFIG)
        self.assertFalse((sb.target / "config.json.bak").exists())
        self.assertTrue((sb.target / "qsum" / "NEWFILE.py").is_file())
        self.assertTrue(app.is_dir(), "the old download folder is left for the user to delete")

    def test_update_with_new_config_makes_timestamped_backup(self):
        """choosing the new config keeps the old one as config.json.bak-DATE-TIME (never overwritten)."""
        sb = self.sandbox(with_claude=True)
        sb.state.joinpath("token").touch()
        installed = sb.make_app(sb.target, config=FAKE_CONFIG)
        old_bak = installed / "config.json.bak-20200101-000000"
        old_bak.write_text("an older backup")
        new_config = FAKE_CONFIG.replace("T1", "T9")
        app = sb.make_app(sb.downloads / "quotation-summary", config=new_config)
        # answers: update=y, use new config=y, install poppler=n, replace token=n, open privacy page=n
        r = sb.run(app / "scripts" / "setup-all.command", answers="y\ny\nn\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((sb.target / "config.json").read_text(encoding="utf-8"), new_config)
        baks = sorted(p.name for p in sb.target.glob("config.json.bak-*"))
        self.assertEqual(len(baks), 2, baks)                       # the older one survived, a new one was added
        self.assertEqual(old_bak.read_text(), "an older backup")
        new_bak = [p for p in sb.target.glob("config.json.bak-*") if p != old_bak][0]
        self.assertEqual(new_bak.read_text(encoding="utf-8"), FAKE_CONFIG)
        # the Downloads copy still holds the real company data: the user is told to delete it
        self.assertIn("ยังมี config.json", r.stdout)
        self.assertTrue(app.is_dir())

    def test_update_refuses_a_target_folder_that_is_not_this_program(self):
        """never rsync into a random folder that merely has the target name."""
        sb = self.sandbox(with_claude=True)
        sb.target.mkdir(parents=True)
        (sb.target / "somebody-elses-file.txt").write_text("mine")
        app = sb.make_app(sb.downloads / "quotation-summary")
        r = sb.run(app / "scripts" / "setup-all.command", answers="y\ny\ny\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ดูไม่ใช่โปรแกรมนี้", r.stdout)
        self.assertEqual(sorted(os.listdir(str(sb.target))), ["somebody-elses-file.txt"])
        self.assertFalse(sb.calls("install.sh"))

    def test_claude_installer_download_failure_does_not_run_partial_script(self):
        """curl fails midway -> the half-downloaded file is not executed, the step reports it, and it
        carries on without Claude Code (no token step, no setup-token)."""
        sb = self.sandbox()                          # no claude installed, brew present
        sb.state.joinpath("curl-fail").touch()
        app = sb.make_app(sb.target)
        # answers: install poppler=n, install Claude Code=y, open privacy page=n
        r = sb.run(app / "scripts" / "setup-all.command", answers="n\ny\nn\n")
        self.assertEqual(len(sb.calls("curl")), 1)
        self.assertFalse((sb.state / "partial-installer-ran").exists(), "a partial installer must never be executed")
        self.assertIn("ตัวติดตั้ง Claude Code ทำงานไม่สำเร็จ", r.stdout)
        self.assertIn("ยังหา claude ไม่เจอ", r.stdout)
        self.assertFalse(sb.calls("claude"))
        self.assertFalse(sb.calls("security add-generic-password"))
        self.assertEqual(list((sb.root / "tmp").glob("qs-claude-install.*")), [], "temp file must be removed")
        self.assertEqual(r.returncode, 1, "doctor must report the missing Claude Code")

    def test_setup_token_failure_still_reaches_the_paste_step(self):
        """`claude setup-token` fails -> warning, but the user may already hold a token: the paste step is
        still offered, and the screen is cleared after the token is stored."""
        sb = self.sandbox(with_claude=True)
        sb.state.joinpath("setup-token-fail").touch()
        app = sb.make_app(sb.target)
        # answers: install poppler=n, sign in=y, ready to paste=y, open privacy page=n
        r = sb.run(app / "scripts" / "setup-all.command", answers="n\ny\ny\nn\n")
        self.assertEqual(len(sb.calls("claude setup-token")), 1)
        self.assertIn("claude setup-token ไม่สำเร็จ", r.stdout)
        self.assertEqual(sb.calls("security add-generic-password"),
                         ["security add-generic-password -a tester -s %s -U -w" % SERVICE])
        self.assertEqual(len(sb.calls("clear")), 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_skipping_the_paste_step_warns_that_the_token_is_still_on_screen(self):
        sb = self.sandbox(with_claude=True)
        app = sb.make_app(sb.target)
        # answers: install poppler=n, sign in=y, ready to paste=n, open privacy page=n
        r = sb.run(app / "scripts" / "setup-all.command", answers="n\ny\nn\nn\n")
        self.assertFalse(sb.calls("security add-generic-password"))
        self.assertFalse(sb.calls("clear"))
        self.assertIn("ยังค้างบนจอ", r.stdout)

    def test_api_backend_skips_claude_and_token(self):
        sb = self.sandbox()
        app = sb.make_app(sb.target, config=FAKE_CONFIG.replace('"claude_code"', '"api"'))
        r = sb.run(app / "scripts" / "setup-all.command", answers="n\n")
        self.assertIn("API key", r.stdout)
        self.assertFalse(sb.calls("curl"))
        self.assertFalse(sb.calls("claude"))
        self.assertFalse(sb.calls("security add-generic-password"))

    def test_launcher_runs_setup(self):
        sb = self.sandbox(with_claude=True)
        app = sb.make_app(sb.target)
        (app / LAUNCHER_SETUP).chmod(0o755)
        r = subprocess.run([BASH, str(app / LAUNCHER_SETUP), "--dry-run"], input="", capture_output=True, text=True,
                           env=sb.env(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ติดตั้ง quotation-summary", r.stdout)


@unittest.skipUnless(IS_MAC, "macOS only")
class SetupGuiStepTests(unittest.TestCase):
    """Step 5 of setup-all.command: install the desktop app from build/QuotationSummary.app into ~/Applications.
    The fake machine has a token and no pdftotext/brew, so the prompts are: change token? [n], open privacy page? [n],
    then (only when the app was installed) open the app now? [y by default]."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Sandbox(Path(self._tmp.name), with_brew=False, with_claude=True)
        self.sb.state.joinpath("token").touch()
        self.app = self.sb.make_app(self.sb.target)          # already at the right place: no move step
        self.dest = self.sb.gui_dest

    def tearDown(self):
        self._tmp.cleanup()

    def setup(self, answers="n\nn\nn\n", args=()):
        return self.sb.run(self.app / "scripts" / "setup-all.command", list(args), answers=answers)

    def test_total_is_11_steps_and_the_app_step_comes_after_the_install_step(self):
        self.sb.add_gui_bundle(self.app)
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("== [4/11] ติดตั้งโปรแกรม", r.stdout)
        self.assertIn("== [5/11] ติดตั้งแอปหน้าต่าง สรุปใบเสนอราคา", r.stdout)
        self.assertIn("== [11/11] ตรวจรวมทุกอย่าง (doctor)", r.stdout)
        self.assertNotIn("/10]", r.stdout)

    def test_installs_the_app_and_cleans_up(self):
        self.sb.add_gui_bundle(self.app)
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(gui_payload(self.dest), "v1")
        self.assertEqual([p.name for p in self.sb.applications.iterdir()], [GUI_NAME])      # no .new-* left
        self.assertTrue(os.access(str(self.dest / "Contents" / "MacOS" / "QuotationSummary"), os.X_OK))
        self.assertIn("ติดตั้งแอปแล้ว", r.stdout)
        self.assertEqual(len(self.sb.calls("codesign --verify --strict")) >= 1, True)
        # the doctor at the end sees the installed app
        self.assertIn("แอปหน้าต่าง สรุปใบเสนอราคา ติดตั้งแล้ว", r.stdout)
        # nothing is launched when the answer is no, and the Dock hint is printed
        self.assertFalse([l for l in self.sb.log_lines() if l.startswith("open ") and GUI_NAME in l])
        self.assertIn("เก็บไว้ใน Dock (Keep in Dock)", r.stdout)

    def test_quarantine_flag_is_removed_from_the_installed_copy(self):
        bundle = self.sb.add_gui_bundle(self.app)
        subprocess.run(["xattr", "-w", "com.apple.quarantine", QUARANTINE, str(bundle / "Contents" / "MacOS" / "QuotationSummary")],
                       check=True)
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        q = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(self.dest / "Contents" / "MacOS" / "QuotationSummary")],
                           capture_output=True, text=True)
        self.assertNotEqual(q.returncode, 0)

    def test_open_offer_defaults_to_yes_and_opens_the_installed_app(self):
        self.sb.add_gui_bundle(self.app)
        r = self.setup(answers="n\nn\n\n")                   # empty answer to the last question = default y
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"เปิดแอป สรุปใบเสนอราคา ตอนนี้ไหม\? \[Y/n\]")
        self.assertIn("open %s" % self.dest, self.sb.log_lines())

    def test_replaces_an_older_copy_with_the_same_bundle_id(self):
        make_gui_bundle(self.dest, payload="old")
        self.sb.add_gui_bundle(self.app, payload="new")
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(gui_payload(self.dest), "new")
        self.assertEqual([p.name for p in self.sb.applications.iterdir()], [GUI_NAME])

    def test_running_copy_is_asked_to_quit_before_it_is_replaced(self):
        make_gui_bundle(self.dest, payload="old")
        self.sb.add_gui_bundle(self.app, payload="new")
        self.sb.state.joinpath("app-running").touch()
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        quits = self.sb.calls('osascript -e tell application id "%s" to quit' % GUI_ID)
        self.assertEqual(len(quits), 1, self.sb.log_lines())
        self.assertEqual(gui_payload(self.dest), "new")

    def test_app_that_will_not_quit_is_not_replaced(self):
        make_gui_bundle(self.dest, payload="old")
        self.sb.add_gui_bundle(self.app, payload="new")
        self.sb.state.joinpath("app-running").touch()
        self.sb.state.joinpath("app-wont-quit").touch()
        r = self.setup()
        self.assertEqual(gui_payload(self.dest), "old")
        self.assertIn("ปิดแอปเดิมไม่สำเร็จ", r.stdout)

    def test_app_that_is_not_running_is_never_launched_by_the_quit_request(self):
        """`tell application ... to quit` would LAUNCH a closed app: only `is running` may be asked first."""
        self.sb.add_gui_bundle(self.app)
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.calls('osascript -e tell application id'), [])

    def test_another_app_with_the_same_name_is_not_replaced(self):
        make_gui_bundle(self.dest, bundle_id="com.example.other", payload="theirs")
        self.sb.add_gui_bundle(self.app, payload="new")
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(gui_payload(self.dest), "theirs")
        self.assertIn("ไม่ใช่แอปนี้", r.stdout)
        self.assertEqual([p.name for p in self.sb.applications.iterdir()], [GUI_NAME])
        self.assertNotIn("เปิดแอป สรุปใบเสนอราคา ตอนนี้ไหม", r.stdout)        # nothing was installed: no offer

    def test_a_symlink_with_the_same_name_is_not_replaced_or_followed(self):
        real = make_gui_bundle(self.sb.root / "elsewhere" / "Real.app", payload="theirs")
        self.sb.applications.mkdir(parents=True)
        os.symlink(str(real), str(self.dest))
        self.sb.add_gui_bundle(self.app, payload="new")
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(gui_payload(real), "theirs")

    def test_missing_bundle_warns_and_the_rest_continues(self):
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่พบแอปหน้าต่างในแพ็กเกจ", r.stdout)
        self.assertFalse(self.sb.applications.exists())
        self.assertEqual(len(self.sb.calls("install.sh")), 1)                  # step 4 ran
        self.assertIn("== [11/11]", r.stdout)                                  # and the flow reached the doctor

    def test_bundle_with_another_id_in_the_package_is_not_installed(self):
        self.sb.add_gui_bundle(self.app, bundle_id="com.example.other")
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.dest.exists())
        self.assertIn("ไม่ใช่ %s" % GUI_ID, r.stdout)

    def test_signature_failure_after_copy_keeps_the_old_copy(self):
        make_gui_bundle(self.dest, payload="old")
        self.sb.add_gui_bundle(self.app, payload="new")
        self.sb.state.joinpath("codesign-fail").touch()
        r = self.setup()
        self.assertEqual(gui_payload(self.dest), "old")
        self.assertEqual([p.name for p in self.sb.applications.iterdir()], [GUI_NAME], "no half-copied leftovers")
        self.assertIn("ลายเซ็นของแอปไม่ผ่านการตรวจ", r.stdout)

    def test_architecture_mismatch_is_only_a_warning(self):
        self.sb.add_gui_bundle(self.app)
        self.sb.state.joinpath("file-x86").touch()                             # Apple Silicon (sysctl stub says 1)
        r = self.setup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่ใช่ชนิด arm64", r.stdout)
        self.assertTrue(self.dest.is_dir())

    def test_intel_mac_accepts_an_x86_64_app(self):
        self.sb.add_gui_bundle(self.app)
        self.sb.state.joinpath("intel").touch()
        self.sb.state.joinpath("file-x86").touch()
        r = self.setup()
        self.assertIn("ตรงกับเครื่องนี้ (Intel)", r.stdout)

    def test_arch_check_uses_file_and_sysctl_never_lipo_or_xcrun(self):
        self.sb.add_gui_bundle(self.app)
        self.setup()
        log = "\n".join(self.sb.log_lines())
        self.assertIn("sysctl -n hw.optional.arm64", log)
        self.assertIn("file -b ", log)
        for banned in ("lipo", "xcrun", "git "):
            self.assertNotIn(banned, log)

    def test_dry_run_changes_nothing_and_launches_nothing(self):
        self.sb.add_gui_bundle(self.app)
        make_gui_bundle(self.dest, payload="old")
        self.sb.state.joinpath("app-running").touch()
        r = self.setup(args=["--dry-run"], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("== [5/11]", r.stdout)
        self.assertIn("(ทดลอง) จะรัน: ditto", r.stdout)
        self.assertEqual(gui_payload(self.dest), "old")
        self.assertEqual(self.sb.calls("osascript"), [], "dry run must not even ask whether the app is running")
        # only the doctor's read-only look at the already installed app; nothing is copied, so nothing new to verify
        self.assertEqual([l for l in self.sb.calls("codesign") if ".new-" in l], [])
        self.assertEqual([p.name for p in self.sb.applications.iterdir()], [GUI_NAME])
        self.assertTrue(self.sb.state.joinpath("app-running").exists())

    def test_dry_run_does_not_create_the_applications_folder(self):
        self.sb.add_gui_bundle(self.app)
        r = self.setup(args=["--dry-run"], answers="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.sb.applications.exists())

    def test_app_outside_the_default_location_gets_a_hint(self):
        """The window app only looks in ~/QuotationSummary/quotation-summary (or QS_APP_DIR)."""
        self.sb.add_gui_bundle(self.app)
        elsewhere = self.sb.make_app(self.sb.root / "devrepo" / "apps" / "quotation-summary")
        (self.sb.root / "devrepo" / ".git").mkdir(parents=True)
        self.sb.add_gui_bundle(elsewhere)
        r = self.sb.run(elsewhere / "scripts" / "setup-all.command", ["--allow-dev-checkout"], answers="n\nn\nn\n")
        # tell the user something they can do (move the folder), not an env var a Finder-launched app never sees
        self.assertIn("ย้ายโฟลเดอร์ quotation-summary ไปไว้ที่", r.stdout)
        self.assertIn("QuotationSummary/quotation-summary", r.stdout)
        self.assertNotIn("QS_APP_DIR", r.stdout)

    def test_old_copy_that_cannot_be_fully_removed_is_never_installed_inside(self):
        """if rm leaves the old bundle behind, mv would drop the new app INSIDE it and still say "installed"."""
        old = make_gui_bundle(self.dest, payload="old")
        self.sb.add_gui_bundle(self.app, payload="new")
        locked = old / "Contents" / "Info.plist"
        subprocess.run(["chflags", "uchg", str(locked)], check=True)          # rm -rf cannot delete an immutable file
        try:
            r = self.setup()
        finally:
            subprocess.run(["chflags", "-R", "nouchg", str(self.sb.applications)], check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("ติดตั้งแอปแล้ว", r.stdout)
        self.assertIn("ลบแอปเดิมที่", r.stdout)
        self.assertFalse(list(self.dest.rglob("*.app")), "the new app must not sit inside the old one")
        self.assertNotIn(self.dest.name + ".new", "".join(p.name for p in self.sb.applications.iterdir()))
        self.assertNotIn("เปิดแอป สรุปใบเสนอราคา ตอนนี้ไหม", r.stdout)        # nothing installed: no offer


# A fake "system python3": passes the version probe and `-m venv`, creating a venv whose python logs every pip call
# (and fails them when state file "pip-fail" exists) but otherwise runs the real interpreter (plistlib etc.).
STUB_PYTHON3 = """#!/bin/bash
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<VENVPY
#!/bin/bash
if [ "\\${1:-}" = "-m" ] && [ "\\${2:-}" = "pip" ]; then
  echo "pip \\$*" >> "\\$STUB_LOG"
  [ -f "\\$STUB_STATE/pip-fail" ] && exit 1
  exit 0
fi
exec "%(real)s" "\\$@"
VENVPY
  chmod +x "$3/bin/python"
  exit 0
fi
exec "%(real)s" "$@"
"""


@unittest.skipUnless(IS_MAC, "macOS only")
class InstallScriptTests(unittest.TestCase):
    """The REAL scripts/install.sh: pip must not run again when nothing changed, and a failing pip must
    not stop a re-run that already has a working install. Uses --no-load: launchd is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Sandbox(Path(self._tmp.name))
        self.app = self.sb.make_app(self.sb.root / "work" / "quotation-summary")
        shutil.copy(str(APP / "scripts" / "install.sh"), str(self.app / "scripts" / "install.sh"))   # the real one
        os.unlink(str(self.sb.stubs / "python3"))
        _write_exec(self.sb.stubs / "python3", STUB_PYTHON3 % {"real": sys.executable})
        self.script = self.app / "scripts" / "install.sh"
        self.hash_file = self.app / ".venv" / ".requirements.sha256"

    def tearDown(self):
        self._tmp.cleanup()

    def install(self):
        return self.sb.run(self.script, ["--no-load"])

    def pips(self):
        return self.sb.calls("pip ")

    def test_plist_marks_scheduled_runs_with_qs_trigger(self):
        """The weekly job tells qsum it is the schedule (shown as "ตั้งเวลา" in the desktop app)."""
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plist = self.sb.home / "Library" / "LaunchAgents" / "com.korakod.quotation-summary.plist"
        lint = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        with open(str(plist), "rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["EnvironmentVariables"], {"QS_TRIGGER": "schedule"})
        self.assertEqual(data["ProgramArguments"][0], "/bin/bash")       # unchanged: still run.sh via bash

    def test_first_install_runs_pip_and_records_the_requirements_hash(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.pips()), 2, self.pips())          # upgrade pip + install requirements
        self.assertTrue(self.hash_file.is_file())
        self.assertTrue((self.sb.home / "Library" / "LaunchAgents" / "com.korakod.quotation-summary.plist").is_file())

    def test_rerun_with_same_requirements_skips_pip_and_needs_no_internet(self):
        self.assertEqual(self.install().returncode, 0)
        self.sb.state.joinpath("pip-fail").touch()                  # "no internet": any pip call would fail
        before = len(self.pips())
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.pips()), before, "pip must not run again")
        self.assertIn("ข้ามการดาวน์โหลด", r.stdout)

    def test_changed_requirements_run_pip_again(self):
        self.assertEqual(self.install().returncode, 0)
        with open(str(self.app / "requirements.txt"), "a", encoding="utf-8") as f:
            f.write("# changed\n")
        old_hash = self.hash_file.read_text()
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.pips()), 4)
        self.assertNotEqual(self.hash_file.read_text(), old_hash)

    def test_pip_failure_with_an_earlier_good_install_only_warns(self):
        self.assertEqual(self.install().returncode, 0)
        old_hash = self.hash_file.read_text()
        with open(str(self.app / "requirements.txt"), "a", encoding="utf-8") as f:
            f.write("# changed\n")
        self.sb.state.joinpath("pip-fail").touch()
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("คำเตือน", r.stderr)
        self.assertEqual(self.hash_file.read_text(), old_hash, "hash is only updated after a successful pip")

    def test_pip_failure_on_a_fresh_install_stops(self):
        self.sb.state.joinpath("pip-fail").touch()
        r = self.install()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.hash_file.exists())
        self.assertIn("ต่ออินเทอร์เน็ต", r.stderr)


@unittest.skipUnless(IS_MAC, "macOS only")
class DoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sb = Sandbox(Path(self._tmp.name), with_claude=True)
        self.app = self.sb.make_app(self.sb.target)
        # a healthy installation: venv, plist, launchd registration, token, source folder, log, pdftotext
        self.sb.run(self.app / "scripts" / "install.sh")                    # stub install.sh
        (self.sb.home / "QuotationSummary" / "ใบเสนอราคาทดสอบ").mkdir(parents=True)
        (self.sb.home / "QuotationSummary" / "ใบเสนอราคาทดสอบ" / "a.pdf").write_bytes(b"%PDF")
        (self.sb.home / "Library" / "Logs" / "QuotationSummary" / "run.log").write_text("ok\n")
        self.sb.state.joinpath("token").touch()
        _write_exec(self.sb.fakebin / "pdftotext", "#!/bin/bash\nexit 0\n")
        make_gui_bundle(self.sb.gui_dest)                                   # the installed desktop app
        self.script = self.app / "scripts" / "doctor.command"

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_pass(self):
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ไม่ผ่าน 0", r.stdout)
        self.assertIn("เตือน 0", r.stdout)
        self.assertNotIn("[ไม่ผ่าน]", r.stdout)
        self.assertIn("พบ token ของ Claude ใน Keychain", r.stdout)
        self.assertIn("(ไม่แสดงค่า)", r.stdout)

    def test_missing_token_fails_without_printing_secrets(self):
        self.sb.state.joinpath("token").unlink()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ไม่พบ token ของ Claude", r.stdout)
        self.assertNotIn("-w", " ".join(self.sb.calls("security")), "doctor must never ask Keychain for the secret")

    def test_invalid_config_json_fails(self):
        (self.app / "config.json").write_text("{ broken", encoding="utf-8")
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("รูปแบบ JSON ผิด", r.stdout)

    def test_missing_config_fails(self):
        (self.app / "config.json").unlink()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ไม่พบไฟล์ config.json", r.stdout)

    def test_missing_source_folder_fails(self):
        shutil.rmtree(str(self.sb.home / "QuotationSummary" / "ใบเสนอราคาทดสอบ"))
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ไม่พบโฟลเดอร์ PDF", r.stdout)

    def test_unregistered_launchd_job_fails(self):
        self.sb.state.joinpath("registered").unlink()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ยังไม่ได้ลงทะเบียนกับ launchd", r.stdout)

    def test_claude_not_found_fails(self):
        (self.sb.stubs / "claude").unlink()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ไม่พบโปรแกรม Claude Code", r.stdout)

    def test_missing_pdftotext_is_only_a_warning(self):
        (self.sb.fakebin / "pdftotext").unlink()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[เตือน]    ไม่พบ pdftotext", r.stdout)

    def test_desktop_app_installed_passes_and_is_verified(self):
        r = self.sb.run(self.script)
        self.assertIn("[ผ่าน]     แอปหน้าต่าง สรุปใบเสนอราคา ติดตั้งแล้ว", r.stdout)
        self.assertEqual(len(self.sb.calls("codesign --verify --strict")), 1)

    def test_missing_desktop_app_is_only_a_warning(self):
        shutil.rmtree(str(self.sb.gui_dest))
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[เตือน]    ยังไม่ได้ติดตั้งแอปหน้าต่าง", r.stdout)
        self.assertIn("เตือน 1", r.stdout)

    def test_desktop_app_with_another_bundle_id_is_a_warning(self):
        shutil.rmtree(str(self.sb.gui_dest))
        make_gui_bundle(self.sb.gui_dest, bundle_id="com.example.other")
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ไม่ใช่แอปของโปรแกรมนี้", r.stdout)

    def test_desktop_app_that_is_a_symlink_is_a_warning(self):
        shutil.rmtree(str(self.sb.gui_dest))
        real = make_gui_bundle(self.sb.root / "elsewhere" / "Real.app")
        os.symlink(str(real), str(self.sb.gui_dest))
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ไม่ใช่แอปของโปรแกรมนี้", r.stdout)

    def test_desktop_app_with_a_broken_signature_is_a_warning(self):
        self.sb.state.joinpath("codesign-fail").touch()
        r = self.sb.run(self.script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ลายเซ็นไม่ผ่านการตรวจ", r.stdout)

    def test_protected_location_fails(self):
        moved = self.sb.make_app(self.sb.downloads / "quotation-summary")
        r = self.sb.run(moved / "scripts" / "doctor.command")
        self.assertEqual(r.returncode, 1)
        self.assertIn("macOS กันงานตั้งเวลา", r.stdout)

    def test_plist_pointing_elsewhere_fails(self):
        other = self.sb.make_app(self.sb.root / "other" / "quotation-summary")
        r = self.sb.run(other / "scripts" / "doctor.command")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ไฟล์ตั้งเวลาชี้ไปโปรแกรมที่อื่น", r.stdout)

    def test_launcher_runs_doctor(self):
        (self.app / LAUNCHER_DOCTOR).chmod(0o755)
        r = subprocess.run([BASH, str(self.app / LAUNCHER_DOCTOR)], input="", capture_output=True, text=True,
                           env=self.sb.env(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ตรวจระบบ quotation-summary", r.stdout)


if __name__ == "__main__":
    unittest.main()
