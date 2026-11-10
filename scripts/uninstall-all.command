#!/bin/bash
# Full uninstaller (double-click "ถอนการติดตั้ง.command" in the app folder, which runs this file).
# Meant for wiping an installation completely so the installer can be re-tested from scratch.
#
# Flow: look at every item and say whether it exists -> ask the optional questions (all default N) -> print ONE
# summary of everything that will be deleted -> the user types the confirmation word -> delete in order.
#
# Items, in order:
#   1. launchd job com.korakod.quotation-summary (bootout) + its plist in ~/Library/LaunchAgents
#   2. Keychain items: Claude subscription token + API key (service names from config, same lookups as uninstall.sh).
#      Only `security find/delete-generic-password -s SERVICE` is used: a secret value is never read or printed.
#   3. ~/Library/Application Support/QuotationSummary (cache + isolated Claude Code config dir), and the configured
#      claude_code_config_dir if it is elsewhere AND clearly ours (.../QuotationSummary/claude-code)
#   4. ~/Library/Logs/QuotationSummary
#   5. the installed program folder ~/QuotationSummary/quotation-summary (only if it passes the same is_this_app
#      check as setup-all.command and is NOT a git working tree) and the desktop app "สรุปใบเสนอราคา.app" in
#      ~/Applications (only if it is exactly that path, a real folder, not a symlink, with our bundle id; the
#      running app is quit first; ~/Applications itself is never touched)
#   6. OPTIONAL (default N): ONLY the *.pdf / *.xlsx files (+ .xlsx.tmp, .DS_Store) inside the PDF source folder from
#      config, never the whole folder: any other file is kept, and the folder is removed only if it ends up empty.
#      Also an Excel file outside it. Then ~/QuotationSummary if empty.
#   7. OPTIONAL (default N): Claude Code itself (native install only, steps from the official docs, see below).
#      Only with --include-claude-home AND only after "yes" to removing Claude Code, and only with its own typed
#      phrase: the user's own ~/.claude + ~/.claude.json are MOVED to ~/.claude.bak-<time> (never rm).
#   8. OPTIONAL (default N): leftover copies ~/Downloads/quotation-summary* (they hold real company data)
#
# Claude Code removal steps are taken from https://code.claude.com/docs/en/setup ("Uninstall Claude Code", checked
# 2026-11-09): native install = `rm -f ~/.local/bin/claude` + `rm -rf ~/.local/share/claude`; Homebrew / npm / apt
# have their own commands (we only print them, never run them). The docs' `rm -rf ~/.claude` is deliberately NOT
# done: it holds the user's CLAUDE.md, agents, hooks, plugins, memory and history, so it is only moved aside.
#
# Usage:  scripts/uninstall-all.command [--dry-run] [--yes] [--include-claude-home]
#   --dry-run   print everything that would be removed, delete nothing (questions are still asked so the plan is
#               realistic; no confirmation word needed because nothing is deleted)
#   --yes       TESTS ONLY: skip typing the final confirmation word. Optional questions still default to N, and the
#               separate ~/.claude phrase is still required.
#   --include-claude-home   also offer to move ~/.claude + ~/.claude.json aside (see item 7)
# Exit codes: 0 = done (or nothing to do, or dry run); 1 = cancelled / a deletion failed; 2 = bad option.
#
# Safety rules (all enforced in safe_check, which runs again right before every deletion):
#   - rm only on absolute, symlink-free paths that resolve under the physical $HOME, never $HOME/"/"/empty,
#     and that match what the item expects (an exact path, or the expected name/parent)
#   - never anything that is, or sits inside, a git working tree (checked up to, not including, $HOME), and a user
#     folder is refused when a .git exists ANYWHERE below it (no depth limit)
#   - Keychain items: only names starting with quotation-summary (never Claude Code's own items)
#   - when the script itself runs from a developer checkout it never deletes that checkout
#   - the script first re-runs itself from a temp copy, so deleting the folder it was started from is safe
#
# Test hooks (env): QS_TARGET_DIR (as in setup-all.command) plus the ones in scripts/_common.sh. Must stay
# compatible with macOS' stock bash 3.2 (no associative arrays, no mapfile, no "${arr[@]}" of an empty array).
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"

DRY_RUN=0
ASSUME_YES=0
INCLUDE_CLAUDE_HOME=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1;;
    --yes) ASSUME_YES=1;;
    --include-claude-home) INCLUDE_CLAUDE_HOME=1;;
    *) echo "ไม่รู้จักตัวเลือก: $arg (ใช้ได้: --dry-run, --yes, --include-claude-home)" >&2; exit 2;;
  esac
done

# ---- step 0: re-run from a temp copy ---------------------------------------------------------------------------
# This file lives inside the folder it is about to delete. Run from a copy in the temp folder instead, and tell the
# copy where the real app folder is (QS_UNINSTALL_APP_DIR). The copy removes its own temp folder on exit.
if [ -z "${QS_UNINSTALL_REEXEC:-}" ]; then
  ORIG_APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
  TMP_COPY="$(mktemp -d "${TMPDIR:-/tmp}/qs-uninstall.XXXXXX" 2>/dev/null)"
  if [ -n "$TMP_COPY" ] && cp "$SCRIPT_DIR/uninstall-all.command" "$SCRIPT_DIR/_common.sh" "$TMP_COPY/" 2>/dev/null; then
    QS_UNINSTALL_REEXEC="$TMP_COPY" QS_UNINSTALL_APP_DIR="$ORIG_APP_DIR" \
      exec /bin/bash "$TMP_COPY/uninstall-all.command" "$@"
  fi
  [ -n "$TMP_COPY" ] && rm -rf "$TMP_COPY"
  echo "ผิดพลาด: สร้างสำเนาชั่วคราวของสคริปต์ไม่ได้ (ยังไม่ได้ลบอะไร)" >&2
  exit 1
fi

# From here on we are the temp copy. Remove its folder on exit (name checked first: never rm anything else).
cleanup_tmp() {
  local d="${QS_UNINSTALL_REEXEC:-}"
  case "$(basename "$d")" in
    qs-uninstall.*) [ -f "$d/uninstall-all.command" ] && rm -rf "$d";;
  esac
}
trap cleanup_tmp EXIT
trap 'exit 130' INT TERM

APP_DIR="${QS_UNINSTALL_APP_DIR:-}"
if [ -z "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
  echo "ผิดพลาด: หาโฟลเดอร์โปรแกรมไม่เจอ (ยังไม่ได้ลบอะไร)" >&2; exit 1
fi
APP_DIR="$(cd "$APP_DIR" && pwd -P)"
# shellcheck source=scripts/_common.sh
. "$SCRIPT_DIR/_common.sh"      # QS_LABEL, QS_HOME_P, qs_find_python, qs_cfg_get, qs_source_folder, qs_has_keychain_item ...

# is_this_app DIR -> same check as scripts/setup-all.command (the program's own files are there).
is_this_app() {
  [ -f "$1/scripts/run.sh" ] && [ -f "$1/qsum/main.py" ] && [ -f "$1/scripts/setup-all.command" ]
}
is_this_app "$APP_DIR" || { echo "ผิดพลาด: $APP_DIR ไม่ใช่โฟลเดอร์ของโปรแกรมนี้ (ยังไม่ได้ลบอะไร)" >&2; exit 1; }

# The physical home must be a real folder that is not "/" (every deletion is validated against it).
case "$QS_HOME_P" in
  /?*) ;;
  *) echo "ผิดพลาด: หาโฟลเดอร์ home ไม่เจอ (ยังไม่ได้ลบอะไร)" >&2; exit 1;;
esac
[ -d "$QS_HOME_P" ] || { echo "ผิดพลาด: หาโฟลเดอร์ home ไม่เจอ (ยังไม่ได้ลบอะไร)" >&2; exit 1; }
cd "$QS_HOME_P" || exit 1       # never stay inside a folder we are about to delete

TARGET_DIR="${QS_TARGET_DIR:-$QS_HOME_P/QuotationSummary/quotation-summary}"
CONFIRM_WORD="ลบ"
CONFIRM_WORD_EN="DELETE"          # same meaning, for a keyboard that is not on the Thai layout
CLAUDE_HOME_WORD="ย้าย .claude"
CLAUDE_HOME_WORD_EN="MOVE .claude"
DOCS_UNINSTALL_URL="https://code.claude.com/docs/en/setup#uninstall-claude-code"

# ---- output helpers ---------------------------------------------------------------------------------------------
info() { echo "   $1"; }
item_header() { echo; echo "== [$1/8] $2"; }

# Wait for Enter before the window closes (only when a human is at the terminal), then exit with CODE.
finish() {
  if [ "$DRY_RUN" != 1 ] && [ -t 0 ]; then
    echo
    echo "กด Enter เพื่อปิดหน้าต่าง"
    read -r _
  fi
  exit "$1"
}

# ask_yn "question" -> status 0 = yes. Default is always N (empty answer / end of input = no).
ask_yn() {
  local ans
  printf '   %s [y/N] ' "$1"
  if ! IFS= read -r ans; then echo; ans=""; fi
  case "$ans" in
    [yY]*) return 0;;
    *) return 1;;
  esac
}

# ask_word WORD... -> prints the "> " prompt, reads one line; status 0 only when the trimmed line equals one of the
# words (ASCII letters are compared case-insensitively; Thai is compared as is). Empty/EOF/other = no.
ask_word() {
  local ans up w
  printf '> '
  if ! IFS= read -r ans; then echo; ans=""; fi
  ans="${ans#"${ans%%[![:space:]]*}"}"; ans="${ans%"${ans##*[![:space:]]}"}"     # trim spaces
  up="$(printf '%s' "$ans" | LC_ALL=C tr 'a-z' 'A-Z')"
  for w in "$@"; do
    w="$(printf '%s' "$w" | LC_ALL=C tr 'a-z' 'A-Z')"
    [ -n "$up" ] && [ "$up" = "$w" ] && return 0
  done
  return 1
}

# ---- path safety ------------------------------------------------------------------------------------------------
# canon_of PATH -> physical absolute path (parent symlinks resolved, the last component kept as is); status 1 if the
# parent folder does not exist.
canon_of() {
  local p="$1" d b
  d="$(dirname "$p")"; b="$(basename "$p")"
  d="$(cd "$d" 2>/dev/null && pwd -P)" || return 1
  if [ "$d" = "/" ]; then printf '/%s\n' "$b"; else printf '%s/%s\n' "$d" "$b"; fi
}

# in_git_tree DIR -> status 0 when DIR or one of its parents (up to, NOT including, $HOME) holds a .git entry.
# Walks up instead of calling git (the git stub of macOS can pop up an install dialog).
in_git_tree() {
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "/" ] && [ "$d" != "$QS_HOME_P" ]; do
    [ -e "$d/.git" ] && return 0
    d="$(dirname "$d")"
  done
  return 1
}

# Is the app we were started from a developer checkout? Then it must never be deleted.
DEV_CHECKOUT=0
in_git_tree "$APP_DIR" && DEV_CHECKOUT=1

SAFE_C=""     # canonical path of the last successful safe_check
SAFE_WHY=""   # Thai reason of the last failed safe_check

# safe_check MODE PATH [ARG] -> status 0 when PATH may be deleted with rm. On success SAFE_C = the path to delete.
#   exact      PATH must be exactly ARG (and not a symlink)
#   exactlink  like exact, but PATH may be a symlink (rm removes only the link)
#   app        PATH must be exactly ARG, named quotation-summary, and pass is_this_app
#   guiapp     PATH must be exactly ARG (the installed desktop app), a real folder (not a symlink) with our bundle id
#   cfgdir     PATH must be named claude-code inside a folder named QuotationSummary
#   dl         PATH must be a direct child of ~/Downloads named quotation-summary*
#   user       a folder chosen by the user (config): at least 2 levels below $HOME, not under Library/.ssh/.claude/...,
#              and no .git anywhere inside (unlimited depth)
#   userfile   like user, for ONE regular file named *.xlsx (the Excel output outside the PDF folder)
safe_check() {
  local mode="$1" p="${2:-}" arg="${3:-}" c rel top ea parent
  SAFE_C=""; SAFE_WHY=""
  p="${p%/}"
  [ -n "$p" ] || { SAFE_WHY="path ว่าง"; return 1; }
  case "$p" in /*) ;; *) SAFE_WHY="ไม่ใช่ path เต็ม"; return 1;; esac
  case "$p" in */..|*/../*|*/.|*/./*|*//*) SAFE_WHY="path มี .. หรือ //"; return 1;; esac
  if [ -L "$p" ] && [ "$mode" != exactlink ]; then SAFE_WHY="เป็น symlink (ไม่ลบ)"; return 1; fi
  { [ -e "$p" ] || [ -L "$p" ]; } || { SAFE_WHY="ไม่มีอยู่"; return 1; }
  c="$(canon_of "$p")" || { SAFE_WHY="อ่าน path ไม่ได้"; return 1; }
  case "$c" in
    "$QS_HOME_P"/?*) ;;
    *) SAFE_WHY="อยู่นอกโฟลเดอร์ home"; return 1;;
  esac
  rel="${c#"$QS_HOME_P"/}"
  top="${rel%%/*}"

  case "$mode" in
    exact|exactlink|app)
      ea="$(canon_of "$arg" 2>/dev/null)" || ea="$arg"
      [ "$c" = "$ea" ] || { SAFE_WHY="ตำแหน่งไม่ตรงที่คาดไว้"; return 1; }
      if [ "$mode" = app ]; then
        [ "$(basename "$c")" = "quotation-summary" ] || { SAFE_WHY="ชื่อโฟลเดอร์ไม่ใช่ quotation-summary"; return 1; }
        is_this_app "$c" || { SAFE_WHY="ไม่ใช่โฟลเดอร์ของโปรแกรมนี้"; return 1; }
      fi;;
    guiapp)
      ea="$(canon_of "$arg" 2>/dev/null)" || ea="$arg"
      [ "$c" = "$ea" ] || { SAFE_WHY="ตำแหน่งไม่ตรงที่คาดไว้"; return 1; }
      [ "$(basename "$c")" = "$QS_GUI_INSTALLED_NAME" ] || { SAFE_WHY="ชื่อแอปไม่ใช่ $QS_GUI_INSTALLED_NAME"; return 1; }
      [ -d "$c" ] || { SAFE_WHY="ไม่ใช่โฟลเดอร์แอป"; return 1; }
      [ "$(qs_bundle_id "$c")" = "$QS_GUI_BUNDLE_ID" ] || { SAFE_WHY="bundle id ไม่ใช่ $QS_GUI_BUNDLE_ID (ไม่ใช่แอปของโปรแกรมนี้)"; return 1; };;
    cfgdir)
      parent="$(basename "$(dirname "$c")")"
      { [ "$(basename "$c")" = "claude-code" ] && [ "$parent" = "QuotationSummary" ]; } \
        || { SAFE_WHY="ไม่ใช่ของโปรแกรมนี้โดยชัดเจน (ต้องชื่อ .../QuotationSummary/claude-code)"; return 1; };;
    dl)
      [ "$(dirname "$c")" = "$QS_HOME_P/Downloads" ] || { SAFE_WHY="ไม่ได้อยู่ใน ~/Downloads โดยตรง"; return 1; }
      case "$(basename "$c")" in quotation-summary*) ;; *) SAFE_WHY="ชื่อไม่ขึ้นต้นด้วย quotation-summary"; return 1;; esac;;
    user|userfile)
      case "$rel" in
        */*) ;;
        *) SAFE_WHY="อยู่ชั้นแรกของ home (ลบเองใน Finder ถ้าต้องการ)"; return 1;;
      esac
      case "$top" in
        Library|Applications|.ssh|.gnupg|.claude|.local|.config|.Trash)
          SAFE_WHY="อยู่ในโฟลเดอร์ระบบ/ของเครื่องมืออื่น ($top)"; return 1;;
      esac
      if [ "$mode" = userfile ]; then
        [ -f "$c" ] || { SAFE_WHY="ไม่ใช่ไฟล์ธรรมดา"; return 1; }
        case "$(basename "$c" | LC_ALL=C tr 'A-Z' 'a-z')" in
          *.xlsx) ;;
          *) SAFE_WHY="ไม่ใช่ไฟล์ .xlsx"; return 1;;
        esac
      elif [ -d "$c" ] && [ -n "$(find "$c" -name .git -print -quit 2>/dev/null)" ]; then
        # no -maxdepth: a repository nested at any depth makes the whole folder off limits
        SAFE_WHY="มีโฟลเดอร์ git อยู่ข้างใน (ไม่ลบ)"; return 1
      fi;;
    *) SAFE_WHY="โหมดตรวจไม่รู้จัก"; return 1;;
  esac

  # Never a git working tree (or anything inside one), for every mode.
  if [ -d "$c" ]; then
    in_git_tree "$c" && { SAFE_WHY="เป็นโฟลเดอร์ git (โฟลเดอร์พัฒนา) ไม่ลบ"; return 1; }
  else
    in_git_tree "$(dirname "$c")" && { SAFE_WHY="อยู่ในโฟลเดอร์ git (โฟลเดอร์พัฒนา) ไม่ลบ"; return 1; }
  fi
  # Started from a developer checkout: never delete it, nor a folder that contains it.
  if [ "$DEV_CHECKOUT" = 1 ]; then
    case "$APP_DIR/" in
      "$c"/*) SAFE_WHY="เป็นโฟลเดอร์พัฒนาที่กำลังรันสคริปต์นี้อยู่ (หรือครอบมันไว้) ไม่ลบ"; return 1;;
    esac
  fi
  SAFE_C="$c"
  return 0
}

# ---- the plan: what will be done, filled while looking at the items --------------------------------------------
# Parallel arrays (index = plan position). KIND: launchd | keychain | rm | rmempty | rmfiles | mv
N_PLAN=0
P_KIND=(); P_A=(); P_MODE=(); P_ARG=(); P_TEXT=()
add_plan() {
  P_KIND[$N_PLAN]="$1"; P_A[$N_PLAN]="$2"; P_MODE[$N_PLAN]="${3:-}"; P_ARG[$N_PLAN]="${4:-}"; P_TEXT[$N_PLAN]="$5"
  N_PLAN=$((N_PLAN + 1))
}

# probe_path LABEL PATH MODE ARG -> prints the state; sets PROBE=ok (exists and may be deleted) | missing | refused
PROBE=""
probe_path() {
  local label="$1" p="$2" mode="$3" arg="${4:-}"
  if [ -z "$p" ] || { [ ! -e "$p" ] && [ ! -L "$p" ]; }; then
    PROBE=missing; info "$label: ไม่มีอยู่แล้ว"; return 0
  fi
  if safe_check "$mode" "$p" "$arg"; then
    PROBE=ok; info "$label: พบ ($SAFE_C)"
  else
    PROBE=refused; info "$label: พบ แต่ข้ามไม่ลบ เพราะ $SAFE_WHY ($p)"
  fi
}

# plan_path LABEL PATH MODE ARG -> probe, and put it in the plan when it exists and is safe.
plan_path() {
  probe_path "$1" "$2" "$3" "${4:-}"
  [ "$PROBE" = ok ] && add_plan rm "$2" "$3" "${4:-}" "$1: $SAFE_C"
  return 0
}

# ---- config lookups (same sources as scripts/uninstall.sh: ask the program, fall back to config.json, then defaults)
# The config of the INSTALLED app (what the installation really used) comes first, as long as that folder is this
# program; only if it has none, the config of the folder we were started from (e.g. a developer checkout).
CFG_DIR=""
if [ -f "$TARGET_DIR/config.json" ] && is_this_app "$TARGET_DIR"; then CFG_DIR="$TARGET_DIR"
elif [ -f "$APP_DIR/config.json" ]; then CFG_DIR="$APP_DIR"; fi

# run_lookup FLAG -> last stdout line of `run.sh FLAG` ("" if it cannot run, e.g. no .venv). stdin is cut off.
run_lookup() {
  [ -n "$CFG_DIR" ] && [ -f "$CFG_DIR/scripts/run.sh" ] || return 0
  /bin/bash "$CFG_DIR/scripts/run.sh" "$1" 2>/dev/null </dev/null | tail -n 1
}
# cfg_get KEY DEFAULT -> config.json value from CFG_DIR (qs_cfg_get reads $APP_DIR/config.json; subshell keeps it local)
cfg_get() { ( APP_DIR="$CFG_DIR"; qs_cfg_get "$1" "$2" ); }
# expand_path STR -> STR with ~ and $VARS expanded
expand_path() {
  local py
  py="$(qs_find_python)" || { printf '%s\n' "$1"; return 0; }
  "$py" -I -c 'import os, sys; print(os.path.expandvars(os.path.expanduser(sys.argv[1])))' "$1"
}

TOKEN_SERVICE="$(run_lookup --print-claude-token-service)"
[ -n "$TOKEN_SERVICE" ] || TOKEN_SERVICE="$(cfg_get claude_code_keychain_service quotation-summary-claude-code)"
API_SERVICE="$(run_lookup --print-keychain-service)"
[ -n "$API_SERVICE" ] || API_SERVICE="$(cfg_get keychain_service quotation-summary-anthropic)"
CLAUDE_DIR="$(run_lookup --print-claude-config-dir)"
[ -n "$CLAUDE_DIR" ] || CLAUDE_DIR="$(expand_path "$(cfg_get claude_code_config_dir "$QS_HOME_P/Library/Application Support/QuotationSummary/claude-code")")"
SOURCE_DIR=""
[ -n "$CFG_DIR" ] && SOURCE_DIR="$( ( APP_DIR="$CFG_DIR"; qs_source_folder ) 2>/dev/null )"
OUTPUT_FILE="$(run_lookup --print-output-path)"
# No .venv (so run_lookup is silent): fall back to output_file in config.json, like the other values above.
if [ -z "$OUTPUT_FILE" ] && [ -n "$CFG_DIR" ]; then
  OUTPUT_RAW="$(cfg_get output_file "")"
  [ -n "$OUTPUT_RAW" ] && OUTPUT_FILE="$(expand_path "$OUTPUT_RAW")"
fi

APPSUP="$QS_HOME_P/Library/Application Support/QuotationSummary"
LOGS="$QS_HOME_P/Library/Logs/QuotationSummary"
PLIST="$QS_HOME_P/Library/LaunchAgents/$QS_LABEL.plist"
CONTAINER="$QS_HOME_P/QuotationSummary"
NATIVE_BIN="$QS_HOME_P/.local/bin/claude"
NATIVE_DATA="$QS_HOME_P/.local/share/claude"

echo "ถอนการติดตั้ง quotation-summary ทั้งหมด"
if [ "$DRY_RUN" = 1 ]; then echo "(โหมดทดลอง --dry-run: ดูเฉยๆ ไม่ลบอะไรเลย)"; fi
echo "ตอนนี้ยังไม่ได้ลบอะไร ขั้นแรกคือดูก่อนว่ามีอะไรอยู่บ้าง แล้วจะสรุปให้ดูอีกครั้งก่อนลบจริง"
[ "$DEV_CHECKOUT" = 1 ] && echo "หมายเหตุ: สคริปต์นี้รันจากโฟลเดอร์พัฒนา (git) โฟลเดอร์นี้จะไม่ถูกลบ"

# ---- 1. launchd -------------------------------------------------------------------------------------------------
item_header 1 "งานตั้งเวลา (launchd) และไฟล์ตั้งเวลา"
if command -v launchctl >/dev/null 2>&1 && launchctl print "gui/$(id -u)/$QS_LABEL" >/dev/null 2>&1; then
  info "งาน $QS_LABEL: กำลังทำงานอยู่ในระบบ -> จะหยุดและถอดออก"
  add_plan launchd "$QS_LABEL" "" "" "หยุดงานตั้งเวลา launchd: $QS_LABEL"
else
  info "งาน $QS_LABEL: ไม่มีอยู่แล้ว"
fi
plan_path "ไฟล์ตั้งเวลา" "$PLIST" exact "$PLIST"

# ---- 2. Keychain ------------------------------------------------------------------------------------------------
item_header 2 "รหัสผ่านใน Keychain (ไม่แสดงค่าของรหัสผ่านเด็ดขาด)"
# kc_name_ok SERVICE -> status 0 when the name may be deleted: it must start with quotation-summary. A wrong config
# (e.g. "Claude Code-credentials") must never make us delete another program's login.
kc_name_ok() {
  case "$1" in
    quotation-summary*) return 0;;
    *) return 1;;
  esac
}
kc_item() {   # kc_item LABEL SERVICE
  if ! kc_name_ok "$2"; then
    info "$1 (ชื่อรายการ $2): ข้ามไม่ลบ เพราะชื่อไม่ขึ้นต้นด้วย quotation-summary (กันลบรหัสของโปรแกรมอื่น เช่น Claude Code เอง ถ้าตั้งใจจริงให้ลบเองใน Keychain Access)"
  elif qs_has_keychain_item "$2"; then
    info "$1 (ชื่อรายการ $2): พบ -> จะลบ"
    add_plan keychain "$2" "" "" "Keychain: $1 (ชื่อรายการ $2)"
  else
    info "$1 (ชื่อรายการ $2): ไม่มีอยู่แล้ว"
  fi
}
kc_item "token แพ็กเกจ Claude รายเดือน" "$TOKEN_SERVICE"
if [ "$API_SERVICE" != "$TOKEN_SERVICE" ]; then kc_item "API key" "$API_SERVICE"; fi

# ---- 3. Application Support (+ the configured Claude Code config dir) -------------------------------------------
item_header 3 "ข้อมูล cache และโฟลเดอร์ค่าของ Claude Code ที่โปรแกรมใช้"
plan_path "โฟลเดอร์ Application Support/QuotationSummary" "$APPSUP" exact "$APPSUP"
case "$CLAUDE_DIR/" in
  "$APPSUP"/*) info "โฟลเดอร์ค่าของ Claude Code ($CLAUDE_DIR): อยู่ในโฟลเดอร์ข้างบนแล้ว";;
  *) plan_path "โฟลเดอร์ค่าของ Claude Code ตาม config" "$CLAUDE_DIR" cfgdir;;
esac

# ---- 4. logs ----------------------------------------------------------------------------------------------------
item_header 4 "ไฟล์ล็อก"
plan_path "โฟลเดอร์ล็อก" "$LOGS" exact "$LOGS"

# ---- 5. installed program folder --------------------------------------------------------------------------------
item_header 5 "โฟลเดอร์โปรแกรมที่ติดตั้งไว้ (รวม .venv และ config.json) และแอปหน้าต่าง สรุปใบเสนอราคา"
plan_path "โฟลเดอร์โปรแกรม" "$TARGET_DIR" app "$TARGET_DIR"
if [ "$APP_DIR" != "$TARGET_DIR" ] && [ "$DEV_CHECKOUT" != 1 ]; then
  info "โฟลเดอร์ที่เปิดตัวถอนอยู่ ($APP_DIR) ไม่ใช่ตัวที่ติดตั้ง จึงไม่ลบให้ (ถ้าเป็นสำเนาใน Downloads ดูข้อ 8)"
fi
# The window app. Only the exact installed path with our bundle id; ~/Applications itself is never in the plan.
GUI_APP="$(qs_gui_installed_path)"
plan_path "แอปหน้าต่าง สรุปใบเสนอราคา" "$GUI_APP" guiapp "$GUI_APP"

# ---- 6. optional: source folder ---------------------------------------------------------------------------------
# Only what the PROGRAM made (plus Finder's .DS_Store) is ever removed from the owner's folder:
#   *.pdf (the input), the Excel output file (output_file, resolved), its temp files, and .DS_Store.
# Everything else is "other" and stays, and so does its folder. That includes every OTHER .xlsx: the program makes
# exactly one Excel file, so an .xlsx that is not output_file is the owner's own work.
# The output file the program writes (default: inside the source folder, like qsum/config.py).
OUT_FILE="$OUTPUT_FILE"
if [ -z "$OUT_FILE" ] && [ -n "$SOURCE_DIR" ]; then OUT_FILE="$SOURCE_DIR/สรุปใบเสนอราคา.xlsx"; fi
OUT_BASE=""; OUT_DIR_C=""
if [ -n "$OUT_FILE" ]; then
  OUT_BASE="${OUT_FILE##*/}"
  OUT_DIR_C="$(canon_of "${OUT_FILE%/*}" 2>/dev/null)" || OUT_DIR_C=""
fi
NL=$'\n'
# src_kind FILE -> sets SK to pdf | out | tmp | ds | other. Symlinks and non-regular files are always "other".
#   out = the program's Excel output file (same file as OUT_FILE, which must not be a symlink)
#   tmp = its temp file: <output_file>.tmp or tmp*.xlsx.tmp (tempfile.mkstemp in qsum/excel_writer.py), only in the
#         folder of the output file
src_kind() {
  local f="$1" b="${1##*/}" d="${1%/*}"
  SK=other
  if [ -L "$f" ] || [ ! -f "$f" ]; then return 0; fi
  case "$b" in
    *.[pP][dD][fF]) SK=pdf; return 0;;
    .DS_Store) SK=ds; return 0;;
  esac
  if [ -n "$OUT_BASE" ] && [ -n "$OUT_DIR_C" ] && [ "$d" = "$OUT_DIR_C" ]; then
    case "$b" in
      tmp*.xlsx.tmp|"$OUT_BASE.tmp") SK=tmp; return 0;;
    esac
  fi
  if [ -n "$OUT_FILE" ] && [ ! -L "$OUT_FILE" ] && [ -f "$OUT_FILE" ] && [ "$f" -ef "$OUT_FILE" ]; then SK=out; fi
  return 0
}
# scan_src DIR -> SRC_PDF, SRC_XLS (output file + its temp files), SRC_DS (.DS_Store, not counted as a file of the
# owner's), SRC_OTHER, SRC_OTHER_XLS (the .xlsx part of "other"), SRC_EX (up to 3 example names of "other", one per line)
scan_src() {
  local f
  SRC_PDF=0; SRC_XLS=0; SRC_DS=0; SRC_OTHER=0; SRC_OTHER_XLS=0; SRC_EX=""
  while IFS= read -r -d '' f; do
    src_kind "$f"
    case "$SK" in
      pdf) SRC_PDF=$((SRC_PDF + 1));;
      out|tmp) SRC_XLS=$((SRC_XLS + 1));;
      ds) SRC_DS=$((SRC_DS + 1));;
      *)
        SRC_OTHER=$((SRC_OTHER + 1))
        case "${f##*/}" in *.[xX][lL][sS][xX]) SRC_OTHER_XLS=$((SRC_OTHER_XLS + 1));; esac
        if [ "$SRC_OTHER" -le 3 ]; then SRC_EX="${SRC_EX}${f}${NL}"; fi;;
    esac
  done < <(find "$1" -mindepth 1 ! -type d -print0 2>/dev/null)
}
item_header 6 "ไฟล์ PDF ใบเสนอราคาและไฟล์ Excel (ถามแยก ค่าเริ่มต้น = ไม่ลบ)"
if [ -z "$SOURCE_DIR" ]; then
  info "ไม่พบ source_folder ใน config.json จึงไม่รู้ว่าโฟลเดอร์ PDF อยู่ที่ไหน (ข้าม)"
else
  probe_path "โฟลเดอร์ PDF" "$SOURCE_DIR" user
  if [ "$PROBE" = ok ]; then
    SRC_PATH="$SAFE_C"
    scan_src "$SRC_PATH"
    SRC_OURS=$((SRC_PDF + SRC_XLS))      # exactly what the question offers to delete (.DS_Store is not counted)
    SRC_SIZE="$(du -sh "$SRC_PATH" 2>/dev/null | cut -f1)"
    info "ข้างในมี PDF $SRC_PDF ไฟล์ และ Excel สรุปของโปรแกรม $SRC_XLS ไฟล์ และไฟล์อื่นอีก $SRC_OTHER ไฟล์ (ในนี้เป็น Excel ที่โปรแกรมไม่ได้สร้าง $SRC_OTHER_XLS ไฟล์) (ขนาดรวมทั้งโฟลเดอร์ ${SRC_SIZE:-?})"
    if [ "$SRC_OTHER" -gt 0 ]; then
      info "โฟลเดอร์นี้มีไฟล์อื่นปนอยู่ จะไม่ลบทั้งโฟลเดอร์ จะลบเฉพาะ PDF กับ Excel สรุปของโปรแกรมเท่านั้น ไฟล์อื่น (รวมไฟล์ Excel ที่โปรแกรมไม่ได้สร้าง) และตัวโฟลเดอร์จะอยู่ต่อ ตัวอย่างไฟล์อื่นที่จะไม่ถูกแตะ:"
      printf '%s' "$SRC_EX" | while IFS= read -r ex; do info "  - $ex"; done
    fi
    if [ "$SRC_OURS" -gt 0 ]; then
      info "คำเตือน: นี่คือ PDF และ Excel สรุปของคุณเอง การลบนี้ถาวร ไม่ผ่านถังขยะ และกู้คืนไม่ได้"
      if ask_yn "ลบเฉพาะไฟล์ PDF/Excel ของโปรแกรมใน $SRC_PATH (รวม $SRC_OURS ไฟล์)?"; then
        add_plan rmfiles "$SOURCE_DIR" user "" "ไฟล์ PDF/Excel ในโฟลเดอร์ (ถาวร ไม่ผ่านถังขยะ: PDF $SRC_PDF ไฟล์, Excel สรุปของโปรแกรม $SRC_XLS ไฟล์; ไฟล์อื่น $SRC_OTHER ไฟล์ รวม Excel ที่โปรแกรมไม่ได้สร้าง $SRC_OTHER_XLS ไฟล์ จะไม่ถูกแตะ; โฟลเดอร์ย่อยที่ว่างอยู่ก่อนแล้วจะอยู่ต่อ; ลบโฟลเดอร์เฉพาะเมื่อว่าง): $SRC_PATH"
      else
        info "ไม่ลบไฟล์ใน PDF โฟลเดอร์"
      fi
    else
      info "ไม่มีไฟล์ PDF/Excel ให้ลบ"
      if [ "$SRC_OTHER" -eq 0 ]; then
        # a folder with nothing in it (maybe only empty sub-folders and a Finder .DS_Store): offer to remove it with
        # rmdir only. Arg "emptyok" = the owner agreed to remove this empty tree, so empty sub-folders go too.
        if ask_yn "ลบโฟลเดอร์ว่าง $SRC_PATH ?"; then
          add_plan rmfiles "$SOURCE_DIR" user "emptyok" "โฟลเดอร์ PDF ที่ไม่มีไฟล์ (ลบเฉพาะเมื่อว่าง): $SRC_PATH"
        fi
      fi
    fi
  fi
fi
# An Excel output file configured OUTSIDE the source folder (the default sits inside it).
if [ -n "$OUTPUT_FILE" ]; then
  case "$OUTPUT_FILE" in
    "${SOURCE_DIR:-/nonexistent}"/*) ;;   # inside the source folder: handled above (as an .xlsx file)
    *)
      if [ ! -e "$OUTPUT_FILE" ] && [ ! -L "$OUTPUT_FILE" ]; then
        info "ไฟล์ Excel ผลลัพธ์ตาม config ($OUTPUT_FILE): ไม่มีอยู่แล้ว"
      else
        probe_path "ไฟล์ Excel ผลลัพธ์ (อยู่นอกโฟลเดอร์ PDF)" "$OUTPUT_FILE" userfile
        if [ "$PROBE" = ok ]; then
          info "คำเตือน: ไฟล์นี้เป็นสรุปของคุณ การลบถาวร กู้คืนไม่ได้"
          if ask_yn "ลบไฟล์ Excel $SAFE_C ?"; then
            add_plan rm "$OUTPUT_FILE" userfile "" "ไฟล์ Excel (ถาวร ไม่ผ่านถังขยะ): $SAFE_C"
          else
            info "ไม่ลบไฟล์ Excel"
          fi
        fi
      fi;;
  esac
fi
# ~/QuotationSummary itself: removed at the end only if nothing is left in it (never rm -rf).
if [ -d "$CONTAINER" ]; then
  info "โฟลเดอร์ $CONTAINER: จะลบให้เฉพาะเมื่อว่างเปล่าหลังลบทุกอย่างเสร็จ (ถ้ายังมีไฟล์อื่นอยู่จะเก็บไว้)"
  add_plan rmempty "$CONTAINER" exact "$CONTAINER" "โฟลเดอร์ $CONTAINER (ลบเฉพาะถ้าว่างเปล่าหลังลบทุกอย่างแล้ว)"
else
  info "โฟลเดอร์ $CONTAINER: ไม่มีอยู่แล้ว"
fi

# ---- 7. optional: Claude Code ------------------------------------------------------------------------------------
item_header 7 "Claude Code (ถามแยก ค่าเริ่มต้น = ไม่ลบ)"
info "วิธีถอนตามคู่มือทางการ: $DOCS_UNINSTALL_URL"
CLAUDE_RUNNING=0
if pgrep -x claude >/dev/null 2>&1; then
  CLAUDE_RUNNING=1
  info "คำเตือน: ตอนนี้มี claude เปิดอยู่ (เช่น หน้าต่างทีม AI) ถ้าลบไฟล์โปรแกรมตอนนี้ งานที่เปิดค้างอยู่จะดับกลางทาง ควรปิด Claude Code ทุกหน้าต่างก่อน แล้วค่อยรันตัวถอนใหม่ (ข้อนี้ตอบ N ไว้ก่อนได้)"
fi
# 7a. the program itself. Only the "native" install is removed by us: its launcher is a symlink into
# ~/.local/share/claude/versions/ (docs). Anything else (Homebrew, npm, a custom launcher) is only reported.
NATIVE_OK=0
CLAUDE_REMOVE_CHOSEN=0
if [ -L "$NATIVE_BIN" ]; then
  case "$(readlink "$NATIVE_BIN")" in
    */.local/share/claude/*) NATIVE_OK=1;;
  esac
elif [ -f "$NATIVE_BIN" ] && [ -d "$NATIVE_DATA" ]; then
  NATIVE_OK=1
fi
FOUND_CLAUDE="$(qs_find_claude 2>/dev/null)"
if [ "$NATIVE_OK" = 1 ]; then
  info "Claude Code (แบบติดตั้งทางการ native): พบที่ $NATIVE_BIN"
  info "ถ้าลบ คุณจะใช้คำสั่ง claude ไม่ได้จนกว่าจะติดตั้งใหม่ (อาจใช้กับงานอื่นอยู่ด้วย) และควรปิด claude ทุกหน้าต่างก่อน"
  if ask_yn "ถอน Claude Code ออกจากเครื่อง (ลบ ~/.local/bin/claude และ ~/.local/share/claude ตามคู่มือ)?"; then
    CLAUDE_REMOVE_CHOSEN=1
    add_plan rm "$NATIVE_BIN" exactlink "$NATIVE_BIN" "Claude Code: ตัวเรียก $NATIVE_BIN"
    if [ -d "$NATIVE_DATA" ] && safe_check exact "$NATIVE_DATA" "$NATIVE_DATA"; then
      add_plan rm "$NATIVE_DATA" exact "$NATIVE_DATA" "Claude Code: ไฟล์โปรแกรม $SAFE_C"
    fi
  else
    info "ไม่ถอน Claude Code"
  fi
elif [ -n "$FOUND_CLAUDE" ]; then
  info "พบ claude ที่ $FOUND_CLAUDE แต่ไม่ใช่แบบ native ที่ตัวติดตั้งของเรา/คู่มือระบุ จึงไม่ลบให้"
  info "ถ้าติดตั้งด้วย Homebrew: brew uninstall --cask claude-code (หรือถ้าลงแบบ latest: brew uninstall --cask claude-code@latest)"
  info "ถ้าติดตั้งด้วย npm: npm uninstall -g @anthropic-ai/claude-code"
  info "รายละเอียด: $DOCS_UNINSTALL_URL"
else
  info "Claude Code: ไม่มีอยู่แล้ว"
fi
# 7b. the user's own settings/history. Never deleted: at most MOVED aside to ~/.claude.bak-<time>. Offered only with
# --include-claude-home, only after "yes" to 7a, and only with its own typed phrase (a stray "y" cannot trigger it).
CLAUDE_HOME="$QS_HOME_P/.claude"
CLAUDE_JSON="$QS_HOME_P/.claude.json"
BAK_TS="$(date +%Y%m%d-%H%M%S)"
# bak_name BASE -> BASE.bak-<time>, with -N added while that name is taken
bak_name() {
  local c="$1.bak-$BAK_TS" n=0
  while [ -e "$c" ] || [ -L "$c" ]; do n=$((n + 1)); c="$1.bak-$BAK_TS-$n"; done
  printf '%s\n' "$c"
}
if [ -e "$CLAUDE_HOME" ] || [ -L "$CLAUDE_HOME" ] || [ -e "$CLAUDE_JSON" ] || [ -L "$CLAUDE_JSON" ]; then
  if [ "$INCLUDE_CLAUDE_HOME" != 1 ]; then
    info "พบค่าของ Claude Code ของคุณเอง (~/.claude และ ~/.claude.json) จะไม่แตะต้อง (ถ้าต้องการย้ายไปสำรองจริงๆ ต้องรันด้วย --include-claude-home)"
  elif [ "$CLAUDE_REMOVE_CHOSEN" != 1 ]; then
    info "พบ ~/.claude และ ~/.claude.json แต่ข้อนี้จะถามเฉพาะเมื่อเลือกถอน Claude Code ข้างบน (ตอนนี้ไม่ได้เลือก) จึงไม่แตะต้อง"
  else
    info "พบค่าของ Claude Code ของคุณเอง: ~/.claude และ ~/.claude.json"
    info "คำเตือน: ข้างในมี CLAUDE.md ส่วนตัว, agents/skills, hooks, plugins, การตั้งค่า, MCP, ความจำ และประวัติการคุยของทุกโปรเจกต์ รวมถึงข้อมูลบัญชีที่ login ไว้ (ใน ~/.claude.json)"
    info "ถ้ายังติดตั้ง Claude Desktop / VS Code / JetBrains อยู่ โปรแกรมเหล่านั้นจะสร้าง ~/.claude ใหม่ให้เอง และต้องปิด Claude Code ทุกตัวก่อน"
    info "โปรแกรมนี้จะไม่ลบ แต่จะย้ายไปเป็น ~/.claude.bak-<วันเวลา> (ยังกู้คืนได้ด้วยการย้ายชื่อกลับ ต้องลบโฟลเดอร์สำรองเองถ้าไม่ต้องการ)"
    echo "   ถ้าต้องการย้าย พิมพ์ \"$CLAUDE_HOME_WORD\" (หรือ $CLAUDE_HOME_WORD_EN ถ้าพิมพ์ไทยไม่ได้) แล้วกด Enter อย่างอื่น = ไม่ย้าย"
    if printf '   ' && ask_word "$CLAUDE_HOME_WORD" "$CLAUDE_HOME_WORD_EN"; then
      for pair in "$CLAUDE_HOME|~/.claude" "$CLAUDE_JSON|~/.claude.json"; do
        cp_path="${pair%%|*}"; cp_label="${pair#*|}"
        probe_path "$cp_label" "$cp_path" exact "$cp_path"
        if [ "$PROBE" = ok ]; then
          cp_dst="$(bak_name "$cp_path")"
          add_plan mv "$cp_path" exact "$cp_dst" "ย้าย $SAFE_C ไปสำรองที่ $cp_dst (ไม่ลบ)"
        fi
      done
    else
      info "ไม่ย้าย ~/.claude และ ~/.claude.json"
    fi
  fi
else
  info "ค่าของ Claude Code (~/.claude): ไม่มีอยู่แล้ว"
fi

# ---- 8. optional: leftovers in Downloads -------------------------------------------------------------------------
item_header 8 "สำเนาค้างใน ~/Downloads ที่ชื่อขึ้นต้นด้วย quotation-summary (ถามแยก ค่าเริ่มต้น = ไม่ลบ)"
DL_DIR="$QS_HOME_P/Downloads"
DL_N=0
DL_LIST=()
if [ -d "$DL_DIR" ]; then
  for f in "$DL_DIR"/quotation-summary*; do
    { [ -e "$f" ] || [ -L "$f" ]; } || continue
    if safe_check dl "$f"; then
      DL_LIST[$DL_N]="$SAFE_C"; DL_N=$((DL_N + 1))
      info "พบ: $SAFE_C ($(du -sh "$SAFE_C" 2>/dev/null | cut -f1))"
    else
      info "พบ $f แต่ข้ามไม่ลบ เพราะ $SAFE_WHY"
    fi
  done
fi
if [ "$DL_N" -eq 0 ]; then
  info "ไม่มีสำเนาค้างอยู่แล้ว"
else
  info "คำเตือน: ในสำเนาเหล่านี้อาจมี config.json (ชื่อบริษัท เลขผู้เสียภาษีจริง) การลบถาวร ไม่ผ่านถังขยะ"
  if ask_yn "ลบสำเนาทั้ง $DL_N รายการข้างบน?"; then
    i=0
    while [ "$i" -lt "$DL_N" ]; do
      add_plan rm "${DL_LIST[$i]}" dl "" "สำเนาใน Downloads (ถาวร): ${DL_LIST[$i]}"
      i=$((i + 1))
    done
  else
    info "ไม่ลบสำเนาใน Downloads"
  fi
fi

# ---- summary + confirmation -------------------------------------------------------------------------------------
echo
echo "================ สรุป: สิ่งที่จะถูกลบ ================"
if [ "$N_PLAN" -eq 0 ]; then
  echo "ไม่มีอะไรต้องลบ (ไม่มีอยู่แล้วทั้งหมด หรือถูกข้ามเพื่อความปลอดภัย)"
  echo
  echo "ถ้าต้องการติดตั้งใหม่: ดับเบิลคลิก ติดตั้ง.command จากโฟลเดอร์ที่แตกจาก zip (ถ้าไม่มีแล้ว ให้แตก zip ใหม่)"
  echo "หลังติดตั้งเช็กด้วย ตรวจระบบ.command"
  finish 0
fi
i=0
while [ "$i" -lt "$N_PLAN" ]; do
  echo "  $((i + 1)). ${P_TEXT[$i]}"
  i=$((i + 1))
done
echo "======================================================"

if [ "$DRY_RUN" = 1 ]; then
  echo
  echo "(ทดลอง) ยังไม่ได้ลบอะไรเลย รันใหม่โดยไม่ใส่ --dry-run เพื่อลบจริง"
  finish 0
fi

if [ "$ASSUME_YES" = 1 ]; then
  echo "(--yes) ข้ามการพิมพ์ยืนยัน"
else
  echo
  echo "การลบนี้กู้คืนไม่ได้ ถ้าแน่ใจ พิมพ์คำว่า $CONFIRM_WORD แล้วกด Enter"
  echo "(ถ้าแป้นพิมพ์ยังเป็นภาษาอังกฤษ พิมพ์ $CONFIRM_WORD_EN แทนได้ พิมพ์อย่างอื่นหรือกด Enter เฉยๆ = ยกเลิก)"
  if ! ask_word "$CONFIRM_WORD" "$CONFIRM_WORD_EN"; then
    echo "ยกเลิก: ยังไม่ได้ลบอะไรเลย"
    finish 1
  fi
fi

# ---- execute ----------------------------------------------------------------------------------------------------
echo
echo "กำลังลบ ..."
FAILS=0
ok_line()   { echo "   [เสร็จ] $1"; }
fail_line() { echo "   [ไม่สำเร็จ] $1"; FAILS=$((FAILS + 1)); }

# quit_gui_app -> ask the running desktop app to quit (and wait max ~5 s) so we do not delete it under a live process.
# `is running` first: `tell application ... to quit` would LAUNCH an app that is not running.
quit_gui_app() {
  local n=0
  [ "$(osascript -e "application id \"$QS_GUI_BUNDLE_ID\" is running" 2>/dev/null)" = "true" ] || return 0
  osascript -e "tell application id \"$QS_GUI_BUNDLE_ID\" to quit" >/dev/null 2>&1
  while [ "$(osascript -e "application id \"$QS_GUI_BUNDLE_ID\" is running" 2>/dev/null)" = "true" ] && [ "$n" -lt 25 ]; do
    sleep 0.2; n=$((n + 1))
  done
  return 0
}

# do_rm MODE PATH ARG KIND(rm|rmempty|rmfiles) TEXT -> re-validates (safe_check) right before deleting.
do_rm() {
  local mode="$1" p="$2" arg="$3" kind="$4" text="$5" left keep_dirs d f
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then ok_line "$text (ไม่มีอยู่แล้ว)"; return 0; fi
  if ! safe_check "$mode" "$p" "$arg"; then
    # a container folder we keep on purpose (e.g. it holds a developer checkout) is not an error
    if [ "$kind" = rmempty ]; then ok_line "$text (เก็บไว้ เพราะ $SAFE_WHY)"; return 0; fi
    fail_line "$text (ไม่ลบ เพราะ $SAFE_WHY)"; return 1
  fi
  if [ "$kind" = rmfiles ]; then
    # 1. remember the sub-folders that hold nothing (but a .DS_Store) BEFORE we delete: they are the owner's, keep them
    #    (arg "emptyok" = the owner agreed to remove an empty tree: nothing is kept, and only .DS_Store files are deleted)
    keep_dirs=""
    if [ "$arg" != emptyok ]; then
      while IFS= read -r -d '' d; do
        if [ -z "$(find "$d" -mindepth 1 -maxdepth 1 ! -name .DS_Store -print -quit 2>/dev/null)" ]; then keep_dirs="${keep_dirs}${d}${NL}"; fi
      done < <(find "$SAFE_C" -mindepth 1 -type d -print0 2>/dev/null)
    fi
    # 2. delete only our kinds of regular files (src_kind never says "ours" for a symlink or another .xlsx)
    while IFS= read -r -d '' f; do
      src_kind "$f"
      case "$SK" in
        ds) rm -f -- "$f" 2>/dev/null;;
        pdf|out|tmp) [ "$arg" = emptyok ] || rm -f -- "$f" 2>/dev/null;;
      esac
    done < <(find "$SAFE_C" -mindepth 1 ! -type d -print0 2>/dev/null)
    # 3. rmdir (never rm -rf) the sub-folders this run emptied, deepest first; rmdir itself refuses a non-empty one
    while IFS= read -r -d '' d; do
      case "$NL$keep_dirs" in *"$NL$d$NL"*) continue;; esac
      rmdir -- "$d" 2>/dev/null
    done < <(find "$SAFE_C" -depth -mindepth 1 -type d -print0 2>/dev/null)
    # 4. did all of our files go?
    left=0
    if [ "$arg" != emptyok ]; then
      scan_src "$SAFE_C"
      left=$((SRC_PDF + SRC_XLS))
    fi
    if [ "$left" -gt 0 ]; then
      fail_line "$text (ลบ PDF/Excel ไม่หมด ถ้าอยู่ใน Desktop/Documents ให้เช็กสิทธิ์ของ Terminal ใน การตั้งค่าระบบ > ความเป็นส่วนตัวและความปลอดภัย)"
      return 1
    fi
    scan_src "$SAFE_C"
    if rmdir -- "$SAFE_C" 2>/dev/null; then
      ok_line "$text (โฟลเดอร์ว่างแล้ว จึงลบโฟลเดอร์ด้วย)"
    elif [ "$SRC_OTHER" -gt 0 ]; then
      ok_line "$text (ลบ PDF/Excel แล้ว เก็บโฟลเดอร์ไว้เพราะยังมีไฟล์อื่น $SRC_OTHER ไฟล์)"
    else
      ok_line "$text (ลบ PDF/Excel แล้ว เก็บโฟลเดอร์ไว้เพราะยังมีโฟลเดอร์ย่อยว่างที่มีอยู่เดิม)"
    fi
    return 0
  fi
  if [ "$kind" = rmempty ]; then
    # only an empty folder (a Finder .DS_Store does not count) and only with rmdir, never rm -rf
    left="$(find "$SAFE_C" -mindepth 1 -maxdepth 1 ! -name .DS_Store -print -quit 2>/dev/null)"
    if [ -n "$left" ]; then ok_line "$text (ยังมีไฟล์อื่นอยู่ จึงเก็บไว้)"; return 0; fi
    rm -f -- "$SAFE_C/.DS_Store" 2>/dev/null
    if rmdir -- "$SAFE_C" 2>/dev/null; then ok_line "$text"; else fail_line "$text"; return 1; fi
    return 0
  fi
  if [ "$mode" = guiapp ]; then quit_gui_app; fi
  if [ -d "$SAFE_C" ] && [ ! -L "$SAFE_C" ]; then rm -rf -- "$SAFE_C"; else rm -f -- "$SAFE_C"; fi
  if [ -e "$SAFE_C" ] || [ -L "$SAFE_C" ]; then
    fail_line "$text (ลบไม่หมด ถ้าอยู่ใน Desktop/Documents ให้เช็กสิทธิ์ของ Terminal ใน การตั้งค่าระบบ > ความเป็นส่วนตัวและความปลอดภัย)"
    return 1
  fi
  ok_line "$text"
}

# do_mv SRC DST TEXT -> re-validates SRC, then moves it aside (mv -n: never overwrites). Nothing is deleted.
do_mv() {
  local src="$1" dst="$2" text="$3"
  if [ ! -e "$src" ] && [ ! -L "$src" ]; then ok_line "$text (ไม่มีอยู่แล้ว)"; return 0; fi
  if ! safe_check exact "$src" "$src"; then fail_line "$text (ไม่ย้าย เพราะ $SAFE_WHY)"; return 1; fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then fail_line "$text (ไม่ย้าย เพราะมีชื่อปลายทางอยู่แล้ว)"; return 1; fi
  mv -n -- "$SAFE_C" "$dst" 2>/dev/null
  if [ -e "$dst" ] && [ ! -e "$SAFE_C" ] && [ ! -L "$SAFE_C" ]; then ok_line "$text"; else fail_line "$text"; return 1; fi
}

i=0
while [ "$i" -lt "$N_PLAN" ]; do
  case "${P_KIND[$i]}" in
    launchd)
      if launchctl bootout "gui/$(id -u)/${P_A[$i]}" >/dev/null 2>&1 || ! launchctl print "gui/$(id -u)/${P_A[$i]}" >/dev/null 2>&1; then
        ok_line "${P_TEXT[$i]}"
      else
        fail_line "${P_TEXT[$i]}"
      fi;;
    keychain)
      if ! kc_name_ok "${P_A[$i]}"; then fail_line "${P_TEXT[$i]} (ไม่ลบ เพราะชื่อไม่ขึ้นต้นด้วย quotation-summary)"; i=$((i + 1)); continue; fi
      n=0
      # delete every item with that service name (normally one); the secret itself is never read
      while qs_has_keychain_item "${P_A[$i]}" && [ "$n" -lt 5 ]; do
        security delete-generic-password -s "${P_A[$i]}" >/dev/null 2>&1 || break
        n=$((n + 1))
      done
      if qs_has_keychain_item "${P_A[$i]}"; then fail_line "${P_TEXT[$i]}"; else ok_line "${P_TEXT[$i]}"; fi;;
    rm|rmempty|rmfiles)
      do_rm "${P_MODE[$i]}" "${P_A[$i]}" "${P_ARG[$i]}" "${P_KIND[$i]}" "${P_TEXT[$i]}";;
    mv)
      do_mv "${P_A[$i]}" "${P_ARG[$i]}" "${P_TEXT[$i]}";;
  esac
  i=$((i + 1))
done

echo
if [ "$FAILS" -gt 0 ]; then
  echo "ถอนไม่ครบ: มี $FAILS รายการที่ไม่สำเร็จ (ดูบรรทัด [ไม่สำเร็จ] ข้างบน) รันซ้ำได้ ส่วนที่ลบแล้วจะแสดงว่าไม่มีอยู่แล้ว"
else
  echo "ถอนการติดตั้งเสร็จแล้ว"
fi
echo "ถ้าต้องการติดตั้งใหม่: ดับเบิลคลิก ติดตั้ง.command จากโฟลเดอร์ที่แตกจาก zip (โฟลเดอร์โปรแกรมที่ติดตั้งไว้ถูกลบไปแล้ว ถ้าไม่มี zip ให้แตกใหม่)"
echo "หลังติดตั้งเช็กด้วย ตรวจระบบ.command"
if [ "$FAILS" -gt 0 ]; then finish 1; fi
finish 0
