# Shared helpers for scripts/setup-all.command and scripts/doctor.command (sourced, not run directly).
# Must stay compatible with macOS' stock bash 3.2 (no associative arrays, no ${var,,}).
#
# The caller sets APP_DIR (physical path of the app folder) before using the functions below.
#
# Test hooks (env, all optional; used only by tests/test_setup_scripts.py to avoid touching the real machine):
#   QS_BIN_DIRS            colon-separated folders searched for claude / brew / pdftotext in addition to PATH
#   QS_PYTHON_CANDIDATES   space-separated python commands/paths to try, in order
#   QS_APPLICATIONS_DIR    where the desktop app is installed (default ~/Applications)

QS_LABEL="com.korakod.quotation-summary"
QS_BIN_DIRS="${QS_BIN_DIRS:-$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin}"
QS_PYTHON_CANDIDATES="${QS_PYTHON_CANDIDATES:-python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3}"
QS_PYBIN="${QS_PYBIN:-}"   # cache for qs_find_python

# The desktop app (SwiftUI window): built by scripts/build-app.sh, shipped in the zip under build/, installed by
# setup-all.command into ~/Applications under a Thai name. The bundle id is how every script recognises "our" app.
QS_GUI_BUNDLE_ID="dev.korakod.QuotationSummary"
QS_GUI_BUILT="build/QuotationSummary.app"            # relative to APP_DIR
QS_GUI_INSTALLED_NAME="สรุปใบเสนอราคา.app"

# Physical (symlink-resolved) home, so comparisons with `pwd -P` paths are reliable.
QS_HOME_P="$(cd "$HOME" 2>/dev/null && pwd -P)" || QS_HOME_P="$HOME"

# qs_find_bin NAME -> prints the path of NAME (PATH first, then QS_BIN_DIRS); status 1 if not found.
qs_find_bin() {
  local name="$1" p d old_ifs
  p="$(command -v "$name" 2>/dev/null)"
  if [ -n "$p" ] && [ -f "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
  old_ifs="$IFS"; IFS=:
  for d in $QS_BIN_DIRS; do
    if [ -f "$d/$name" ] && [ -x "$d/$name" ]; then IFS="$old_ifs"; echo "$d/$name"; return 0; fi
  done
  IFS="$old_ifs"
  return 1
}

# qs_find_claude -> prints the claude executable (same places the program itself looks), status 1 if none.
qs_find_claude() {
  qs_find_bin claude && return 0
  if [ -f "$HOME/.claude/local/claude" ] && [ -x "$HOME/.claude/local/claude" ]; then
    echo "$HOME/.claude/local/claude"; return 0
  fi
  return 1
}

# qs_find_python -> sets and prints QS_PYBIN (a Python >= 3.9); status 1 if none.
qs_find_python() {
  local cand p
  if [ -n "$QS_PYBIN" ]; then echo "$QS_PYBIN"; return 0; fi
  for cand in $QS_PYTHON_CANDIDATES; do
    p="$(command -v "$cand" 2>/dev/null)" || continue
    [ -n "$p" ] || continue
    # Without the Command Line Tools /usr/bin/python3 is only a stub that pops up the "install developer
    # tools" dialog when run: do not run it in that case.
    if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      QS_PYBIN="$p"; echo "$p"; return 0
    fi
  done
  return 1
}

# qs_is_protected PATH -> status 0 when PATH is inside a folder that macOS (TCC) hides from launchd jobs.
# Same list as scripts/install.sh.
qs_is_protected() {
  local p="$1" base d
  for base in "$HOME" "$QS_HOME_P"; do
    for d in Documents Desktop Downloads "Library/Mobile Documents"; do
      case "$p" in
        "$base/$d"|"$base/$d"/*) return 0;;
      esac
    done
  done
  return 1
}

# qs_cfg_valid -> status 0 when APP_DIR/config.json exists and is valid JSON (a JSON object).
qs_cfg_valid() {
  local py
  py="$(qs_find_python)" || return 1
  "$py" -I - "$APP_DIR/config.json" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
sys.exit(0 if isinstance(data, dict) else 1)
PY
}

# qs_cfg_get KEY DEFAULT -> prints the string value of KEY in config.json, or DEFAULT if missing/unreadable.
qs_cfg_get() {
  local py
  py="$(qs_find_python)" || { printf '%s\n' "$2"; return 0; }
  "$py" -I - "$APP_DIR/config.json" "$1" "$2" <<'PY'
import json, sys
path, key, default = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as f:
        value = json.load(f).get(key, default)
except Exception:
    value = default
if value is None or value == "":
    value = default
print(value)
PY
}

# qs_source_folder -> prints the PDF source folder from config.json with ~ and $VARS expanded (empty if unknown).
qs_source_folder() {
  local py raw
  py="$(qs_find_python)" || return 1
  raw="$(qs_cfg_get source_folder "")"
  [ -n "$raw" ] || return 1
  "$py" -I -c 'import os, sys; print(os.path.expandvars(os.path.expanduser(sys.argv[1])))' "$raw"
}

# qs_has_keychain_item SERVICE -> status 0 when the item exists. Never asks for or prints the secret:
# without -w / -g `security` only looks up the attributes (output is discarded anyway).
qs_has_keychain_item() {
  security find-generic-password -s "$1" >/dev/null 2>&1
}

# qs_count_pdfs DIR -> number of .pdf files below DIR (case-insensitive); 0 if DIR is unreadable.
qs_count_pdfs() {
  find "$1" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' '
}

# qs_applications_dir -> the folder the desktop app is installed into (QS_APPLICATIONS_DIR, default ~/Applications).
qs_applications_dir() {
  printf '%s\n' "${QS_APPLICATIONS_DIR:-$HOME/Applications}"
}

# qs_gui_installed_path -> full path of the installed desktop app.
qs_gui_installed_path() {
  printf '%s/%s\n' "$(qs_applications_dir)" "$QS_GUI_INSTALLED_NAME"
}

# qs_bundle_id APP_BUNDLE -> prints CFBundleIdentifier of the bundle ("" if unreadable). Uses plutil, with
# `defaults read` as fallback. Never lipo / xcrun / git: on a Mac with bare Command Line Tools those are stubs that
# pop up an install dialog.
qs_bundle_id() {
  local info="$1/Contents/Info.plist" id
  [ -f "$info" ] || return 1
  id="$(plutil -extract CFBundleIdentifier raw -o - "$info" 2>/dev/null)" \
    || id="$(defaults read "$1/Contents/Info" CFBundleIdentifier 2>/dev/null)" || id=""
  printf '%s\n' "$id"
  [ -n "$id" ]
}

# qs_is_our_gui_app APP_BUNDLE -> status 0 when it is a real folder (not a symlink) with our bundle id.
qs_is_our_gui_app() {
  [ -d "$1" ] && [ ! -L "$1" ] && [ "$(qs_bundle_id "$1")" = "$QS_GUI_BUNDLE_ID" ]
}
