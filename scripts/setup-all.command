#!/bin/bash
# One-stop installer for the user's Mac (double-click "ติดตั้ง.command" in the app folder, which runs this file).
#
# What it does, in order (every step says what it is doing, asks before anything that needs the user's
# password or a sign-in, and is safe to run again; finished steps are detected and skipped):
#   1. remove the download quarantine flag, restore +x, move the folder to ~/QuotationSummary/quotation-summary
#   2. check Python 3.9+ (offers xcode-select --install if missing)
#   3. optional: pdftotext via Homebrew poppler (Homebrew itself is never installed)
#   4. scripts/install.sh (venv, packages, config.json, launchd job)
#   5. install the desktop app (build/QuotationSummary.app from the package) into ~/Applications as "สรุปใบเสนอราคา.app"
#   6. create the PDF source folder from config.json and open it in Finder
#   7. install Claude Code if missing (official native installer)
#   8. claude setup-token (browser sign-in) + hidden paste of the token into Keychain
#   9. reminder to turn off "Help improve Claude"
#  10. first test run (Terminal run, then launchd kickstart) so Keychain "Always Allow" happens while present
#  11. scripts/doctor.command checklist, then an offer to open the desktop app
#
# Usage:  scripts/setup-all.command [--dry-run] [--allow-dev-checkout]
#   --dry-run              only read-only checks; every step that would change something prints "(ทดลอง) จะรัน: ..."
#   --allow-dev-checkout   run the real install even from a developer checkout (inside a git work tree). Without it a
#                          real run STOPS after step 1 there: step 4 would re-register the live launchd job to point
#                          at the dev folder (often under Desktop, which macOS blocks for launchd jobs).
#
# The token is never captured by this script: `claude setup-token` prints it on screen, the user copies it and
# pastes it into the hidden prompt of `security add-generic-password ... -w` (same method as
# scripts/set-claude-token.command). No token ever lands in argv, a file or a log by this script's doing.
# The terminal window itself still holds what `claude setup-token` printed (scrollback, and maybe the window state
# macOS saves to disk; not verified on a machine), so after the token is stored the screen AND scrollback are cleared
# (`clear` + ESC[3J) and the user is told to close the window.
#
# Test hooks (env, optional; used by tests/test_setup_scripts.py): QS_TARGET_DIR, QS_RELOCATED, QS_MOVED_FROM,
# plus the ones listed in scripts/_common.sh. Must stay compatible with macOS' stock bash 3.2.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
# shellcheck source=scripts/_common.sh
. "$SCRIPT_DIR/_common.sh"

DRY_RUN=0
ALLOW_DEV=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1;;
    --allow-dev-checkout) ALLOW_DEV=1;;
    *) echo "ไม่รู้จักตัวเลือก: $arg (ใช้ได้: --dry-run, --allow-dev-checkout)" >&2; exit 2;;
  esac
done

TARGET_DIR="${QS_TARGET_DIR:-$QS_HOME_P/QuotationSummary/quotation-summary}"
PRIVACY_URL="https://claude.ai/settings/data-privacy-controls"        # verified in the Claude Code data-usage doc, 2026-11-09
CLAUDE_INSTALL_URL="https://claude.ai/install.sh"                      # native installer, verified in the Claude Code setup doc, 2026-11-09
TOTAL_STEPS=11
STEP_NO=0

# ---- small output / interaction helpers ---------------------------------------------------------------
step()  { STEP_NO=$((STEP_NO + 1)); echo; echo "== [$STEP_NO/$TOTAL_STEPS] $1"; }
info()  { echo "   $1"; }
good()  { echo "   [ผ่าน] $1"; }
warn()  { echo "   [เตือน] $1"; }
bad()   { echo "   [ไม่ผ่าน] $1"; }

# Wait for Enter before the window closes (only when a human is at the terminal), then exit with CODE.
finish() {
  if [ "$DRY_RUN" != 1 ] && [ -t 0 ]; then
    echo
    echo "กด Enter เพื่อปิดหน้าต่าง"
    read -r _
  fi
  exit "$1"
}

# ask_yn "question" y|n -> status 0 = yes. Empty answer / end of input = the default. Dry run: assumes yes.
ask_yn() {
  local prompt="$1" def="${2:-n}" ans hint
  if [ "$def" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "   (ทดลอง) คำถาม: $prompt $hint -> สมมติว่าตอบ ใช่"
    return 0
  fi
  printf '   %s %s ' "$prompt" "$hint"
  if ! IFS= read -r ans; then echo; ans=""; fi
  case "$ans" in
    [yY]*) return 0;;
    [nN]*) return 1;;
  esac
  [ "$def" = y ]
}

# do_cmd CMD ARGS... -> run it, or in dry-run only print it.
do_cmd() {
  if [ "$DRY_RUN" = 1 ]; then echo "   (ทดลอง) จะรัน: $*"; return 0; fi
  "$@"
}

# clear_screen -> wipe the visible screen AND the scrollback (so a token printed by `claude setup-token` is gone).
# Only on a real terminal (QS_FORCE_CLEAR=1 is a test hook so the tests can see the call). Never in dry-run.
clear_screen() {
  [ "$DRY_RUN" = 1 ] && return 0
  if [ -t 1 ] || [ "${QS_FORCE_CLEAR:-}" = 1 ]; then
    clear 2>/dev/null
    printf '\033[3J'
  fi
  return 0
}

# is_this_app DIR -> status 0 when DIR looks like this program (so an update never rsyncs into a random folder).
is_this_app() {
  [ -f "$1/scripts/run.sh" ] && [ -f "$1/qsum/main.py" ] && [ -f "$1/scripts/setup-all.command" ]
}

# is_dev_checkout -> status 0 when APP_DIR sits inside a git work tree (a developer's copy: never move it).
# Walks up looking for .git instead of calling git (the git stub of macOS can pop up an install dialog).
is_dev_checkout() {
  local d="$APP_DIR"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    [ -e "$d/.git" ] && return 0
    d="$(dirname "$d")"
  done
  return 1
}

# Make sure the tools installed in the usual user folders are found (appended: the user's own PATH keeps priority).
PATH="$PATH:$QS_BIN_DIRS"
export PATH

echo "ติดตั้ง quotation-summary (สรุปใบเสนอราคาที่ขายงานได้เป็น Excel ทุกสัปดาห์)"
[ "$DRY_RUN" = 1 ] && echo "*** โหมดทดลอง (--dry-run): ไม่เปลี่ยนแปลงอะไรในเครื่อง แค่แสดงว่าจะทำอะไร ***"
echo "รันซ้ำได้ปลอดภัย: ขั้นที่เสร็จแล้วจะถูกข้าม (บางขั้นทำซ้ำแบบไม่เสียหาย เช่น ตั้งเวลา) ถ้าติดปัญหา รันใหม่ได้เลย"
[ -n "${QS_MOVED_FROM:-}" ] && info "(ย้ายโปรแกรมมาจาก ${QS_MOVED_FROM} แล้ว)"

# =====================================================================================================
step "เตรียมไฟล์ และย้ายโฟลเดอร์ไปที่ถูกต้อง"
# =====================================================================================================
info "ลบป้ายกัก (quarantine) ที่ macOS ติดไฟล์ที่ดาวน์โหลดมา และคืนสิทธิ์รันให้สคริปต์"
do_cmd xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null
do_cmd chmod +x "$APP_DIR"/scripts/*.sh "$APP_DIR"/scripts/*.command "$APP_DIR"/*.command 2>/dev/null

# Compare with the target using resolved paths (the parent may not exist yet).
if [ -d "$(dirname "$TARGET_DIR")" ]; then
  TARGET_CMP="$(cd "$(dirname "$TARGET_DIR")" && pwd -P)/$(basename "$TARGET_DIR")"
else
  TARGET_CMP="$TARGET_DIR"
fi

if [ "$APP_DIR" = "$TARGET_CMP" ]; then
  good "โปรแกรมอยู่ที่ที่ถูกต้องแล้ว: $APP_DIR"
elif [ "${QS_RELOCATED:-}" = 1 ]; then
  warn "ย้ายแล้วแต่ตำแหน่งยังไม่ตรงกับ $TARGET_DIR (ใช้ตำแหน่งปัจจุบัน: $APP_DIR)"
elif is_dev_checkout; then
  warn "นี่คือโฟลเดอร์นักพัฒนา (อยู่ใน git) จึงไม่ย้าย ใช้ที่เดิม: $APP_DIR"
  if [ "$DRY_RUN" = 1 ]; then
    echo "   (ทดลอง) ของจริงจะหยุดตรงนี้ (ถ้าไม่ใส่ --allow-dev-checkout): ไม่ติดตั้ง ไม่แตะงานตั้งเวลาที่ใช้อยู่"
  elif [ "$ALLOW_DEV" != 1 ]; then
    bad "หยุดก่อนติดตั้ง: ถ้าติดตั้งจากโฟลเดอร์นี้ งานตั้งเวลาที่ใช้งานอยู่บนเครื่องจะถูกเปลี่ยนไปชี้มาที่นี่ (และ macOS กันงานตั้งเวลาไม่ให้อ่านโฟลเดอร์ Desktop/Documents/Downloads)"
    info "ติดตั้งจริงให้ใช้ zip จาก make-package.sh ที่แตกไว้ที่อื่น หรือถ้าตั้งใจจริง ๆ รันใหม่ด้วย --allow-dev-checkout"
    finish 1
  else
    warn "ใช้ --allow-dev-checkout: ติดตั้งต่อจากโฟลเดอร์นักพัฒนา งานตั้งเวลาจะชี้มาที่ $APP_DIR"
  fi
else
  info "macOS ไม่ให้งานตั้งเวลาอ่านโฟลเดอร์ Documents / Desktop / Downloads / iCloud"
  info "จึงต้องย้ายโปรแกรมไปไว้ที่ $TARGET_DIR (ถ้า macOS ถามสิทธิ์เข้าโฟลเดอร์ Downloads ให้กดอนุญาต)"
  if [ ! -e "$TARGET_DIR" ]; then
    if ask_yn "ย้ายโฟลเดอร์ไปตอนนี้ไหม?" y; then
      do_cmd mkdir -p "$(dirname "$TARGET_DIR")" || { bad "สร้างโฟลเดอร์ปลายทางไม่ได้"; finish 1; }
      if [ "$DRY_RUN" = 1 ]; then
        echo "   (ทดลอง) จะรัน: mv $APP_DIR $TARGET_DIR แล้วรันขั้นตอนนี้ต่อจากที่ใหม่"
      else
        mv "$APP_DIR" "$TARGET_DIR" || { bad "ย้ายโฟลเดอร์ไม่สำเร็จ (ลองลากโฟลเดอร์ไปที่ $TARGET_DIR เอง แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง)"; finish 1; }
        good "ย้ายไป $TARGET_DIR แล้ว"
        # Carry on from the new place (the running script was just moved).
        QS_RELOCATED=1 QS_MOVED_FROM="$APP_DIR" exec /bin/bash "$TARGET_DIR/scripts/setup-all.command" "$@"
      fi
    else
      warn "ไม่ย้าย: ถ้าโปรแกรมอยู่ในโฟลเดอร์ที่ macOS กัน การตั้งเวลาจะไม่สร้าง Excel"
    fi
  elif ! is_this_app "$TARGET_DIR"; then
    bad "มีโฟลเดอร์ $TARGET_DIR อยู่แล้ว แต่ดูไม่ใช่โปรแกรมนี้ (ไม่พบ scripts/run.sh หรือ qsum/main.py) จึงไม่คัดลอกอะไรลงไป"
    info "ย้ายหรือเปลี่ยนชื่อโฟลเดอร์นั้นก่อน แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    finish 1
  else
    info "พบโปรแกรมที่ติดตั้งไว้แล้วที่ $TARGET_DIR จะอัปเดตโค้ดจากโฟลเดอร์นี้ (เก็บ .venv และ config.json เดิมไว้)"
    if ask_yn "อัปเดตตอนนี้ไหม?" y; then
      if [ "$DRY_RUN" = 1 ]; then
        echo "   (ทดลอง) จะรัน: rsync -a (ยกเว้น .venv, config.json) $APP_DIR/ -> $TARGET_DIR/"
      else
        rsync -a --exclude '.venv/' --exclude 'config.json' --exclude '__pycache__/' "$APP_DIR/" "$TARGET_DIR/" \
          || { bad "คัดลอกไม่สำเร็จ"; finish 1; }
        if [ ! -f "$TARGET_DIR/config.json" ] && [ -f "$APP_DIR/config.json" ]; then
          cp "$APP_DIR/config.json" "$TARGET_DIR/config.json" && good "คัดลอก config.json ใหม่ไปแล้ว"
        elif [ -f "$APP_DIR/config.json" ] && ! cmp -s "$APP_DIR/config.json" "$TARGET_DIR/config.json"; then
          info "config.json ในแพ็กเกจใหม่ต่างจากของเดิมที่ใช้อยู่"
          if ask_yn "ใช้ config.json ใหม่แทน? (ของเดิมจะสำรองไว้เป็น config.json.bak-วันที่-เวลา)" n; then
            # Time-stamped backup: an older backup is never overwritten.
            BAK="$TARGET_DIR/config.json.bak-$(date +%Y%m%d-%H%M%S)"
            cp "$TARGET_DIR/config.json" "$BAK" && cp "$APP_DIR/config.json" "$TARGET_DIR/config.json" \
              && good "เปลี่ยนเป็น config.json ใหม่แล้ว (ของเดิมสำรองไว้ที่ $BAK)"
          else
            info "คง config.json เดิมไว้"
          fi
        fi
        good "อัปเดตโค้ดที่ $TARGET_DIR แล้ว"
        if [ -f "$APP_DIR/config.json" ]; then
          warn "โฟลเดอร์เดิมที่ $APP_DIR ยังมี config.json (ชื่อบริษัทและเลขผู้เสียภาษีจริง) ติดตั้งเสร็จแล้วให้ลบโฟลเดอร์นั้นทิ้ง (ลากไปถังขยะ)"
        fi
        QS_RELOCATED=1 QS_MOVED_FROM="$APP_DIR" exec /bin/bash "$TARGET_DIR/scripts/setup-all.command" "$@"
      fi
    else
      warn "ไม่อัปเดต: ใช้โฟลเดอร์ปัจจุบันต่อ (อาจไม่ตรงกับที่ตั้งเวลาไว้)"
    fi
  fi
fi

# =====================================================================================================
step "ตรวจ Python (ต้อง 3.9 ขึ้นไป)"
# =====================================================================================================
if PYBIN="$(qs_find_python)"; then
  QS_PYBIN="$PYBIN"
  good "พบ $PYBIN ($("$PYBIN" --version 2>&1))"
else
  bad "ไม่พบ Python 3.9 ขึ้นไป"
  if ! xcode-select -p >/dev/null 2>&1; then
    info "เครื่องยังไม่มี Command Line Tools (ชุดเครื่องมือของ Apple ซึ่งมี Python มาด้วย)"
    info "จะเปิดหน้าต่างติดตั้งของ Apple (ใช้เวลาหลายนาที อาจถามรหัสผ่านเครื่อง)"
    if ask_yn "เปิดหน้าต่างติดตั้งตอนนี้ไหม?" y; then
      do_cmd xcode-select --install
      info "รอให้ติดตั้งเสร็จก่อน แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    else
      info "เมื่อพร้อม รัน xcode-select --install ใน Terminal แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    fi
  else
    info "ติดตั้ง Python 3.9 ขึ้นไปจาก https://www.python.org/downloads/macos/ แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
  fi
  [ "$DRY_RUN" = 1 ] && echo "   (ทดลอง) ถึงตรงนี้ของจริงจะหยุดและให้รันใหม่"
  finish 1
fi

# =====================================================================================================
step "ตัวอ่านสำรอง pdftotext (ไม่บังคับ)"
# =====================================================================================================
if PDFT="$(qs_find_bin pdftotext)"; then
  good "พบ pdftotext แล้ว ($PDFT)"
elif BREW="$(qs_find_bin brew)"; then
  info "pdftotext (ในชุด poppler) ใช้เป็นตัวอ่านสำรองเมื่อ Claude ใช้ไม่ได้ (เช่น โควตาเต็ม)"
  if ask_yn "ติดตั้ง poppler ด้วย Homebrew ตอนนี้ไหม? (ใช้เวลาไม่กี่นาที)" n; then
    do_cmd "$BREW" install poppler || warn "ติดตั้ง poppler ไม่สำเร็จ (ข้ามได้ ใช้ Claude อ่านเป็นหลักอยู่แล้ว)"
  else
    info "ข้าม (ติดตั้งทีหลังได้: brew install poppler)"
  fi
else
  info "ไม่พบ pdftotext และเครื่องไม่มี Homebrew (โปรแกรมติดตั้งซอฟต์แวร์เสริมของ Mac) สคริปต์นี้ไม่ติดตั้ง Homebrew ให้เอง"
  info "ข้ามได้ ใช้ Claude อ่านเป็นหลักอยู่แล้ว ตัวสำรองมีไว้ใช้ตอน Claude ใช้ไม่ได้ ถ้าต้องการ ให้ผู้ดูแลช่วยติดตั้ง"
fi

# =====================================================================================================
step "ติดตั้งโปรแกรม (สภาพแวดล้อม Python, แพ็กเกจ, ไฟล์ตั้งค่า, ตั้งเวลารันทุกวันจันทร์ 08:00)"
# =====================================================================================================
if [ -f "$APP_DIR/config.json" ]; then
  info "พบ config.json ที่มากับแพ็กเกจ (ใช้ต่อ ไม่เขียนทับ)"
else
  warn "ไม่พบ config.json: จะสร้างจากแม่แบบซึ่งเป็นชื่อบริษัทตัวอย่าง ต้องแก้ชื่อบริษัทจริงเอง (หรือขอไฟล์ config.json จากผู้ดูแล)"
fi
info "ต้องต่ออินเทอร์เน็ตเพื่อโหลดแพ็กเกจ (ใช้เวลาประมาณ 1-2 นาที)"
if do_cmd env QS_SETUP_ALL=1 /bin/bash "$APP_DIR/scripts/install.sh"; then
  [ "$DRY_RUN" = 1 ] || good "ติดตั้งเสร็จ"
else
  bad "ติดตั้งไม่สำเร็จ ดูข้อความด้านบน (สาเหตุที่พบบ่อย: ไม่มีอินเทอร์เน็ต) แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
  finish 1
fi

# =====================================================================================================
step "ติดตั้งแอปหน้าต่าง สรุปใบเสนอราคา (ลากไฟล์ PDF มาวาง กดเริ่มสรุป ดูสถานะรายไฟล์)"
# =====================================================================================================
# The app comes ready-built inside the package (build/QuotationSummary.app, ad-hoc signed, arm64). Nothing is built
# here. A missing bundle is only a warning: the weekly job and the Terminal tools work without the window app.
# Only tools that are always on a Mac are used (plutil, defaults, file, sysctl, ditto, xattr, codesign, osascript):
# never lipo / xcrun / git, whose stubs pop up an install dialog on a Mac with bare Command Line Tools.
GUI_SRC="$APP_DIR/$QS_GUI_BUILT"
GUI_DEST="$(qs_gui_installed_path)"
GUI_INSTALLED=0

# gui_running -> status 0 when our app is running (`is running` does not launch it, unlike `tell ... to quit`).
gui_running() {
  [ "$(osascript -e "application id \"$QS_GUI_BUNDLE_ID\" is running" 2>/dev/null)" = "true" ]
}
# gui_quit -> ask the running app to quit and wait (max ~5 s) so the old binary is not replaced under a live process.
gui_quit() {
  gui_running || return 0
  info "แอปเดิมกำลังเปิดอยู่ จะปิดก่อนแทนที่"
  osascript -e "tell application id \"$QS_GUI_BUNDLE_ID\" to quit" >/dev/null 2>&1
  local n=0
  while gui_running && [ "$n" -lt 25 ]; do sleep 0.2; n=$((n + 1)); done
  ! gui_running
}

if [ ! -d "$GUI_SRC" ]; then
  warn "ไม่พบแอปหน้าต่างในแพ็กเกจ ($GUI_SRC) จึงข้ามขั้นตอนนี้ (ใช้งานผ่านงานตั้งเวลาและ run-now.command ได้ตามปกติ)"
  info "ขอแพ็กเกจใหม่ที่มีแอปจากผู้ดูแล แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
elif [ "$(qs_bundle_id "$GUI_SRC")" != "$QS_GUI_BUNDLE_ID" ]; then
  warn "แอปในแพ็กเกจ ($GUI_SRC) ไม่ใช่ $QS_GUI_BUNDLE_ID จึงไม่ติดตั้ง (ขอแพ็กเกจใหม่จากผู้ดูแล)"
else
  GUI_BIN="$GUI_SRC/Contents/MacOS/QuotationSummary"
  GUI_ARCH_OK=1
  if [ ! -x "$GUI_BIN" ]; then
    warn "แอปในแพ็กเกจไม่มีตัวโปรแกรม ($GUI_BIN) จึงไม่ติดตั้ง (ขอแพ็กเกจใหม่จากผู้ดูแล)"
    GUI_ARCH_OK=0
  elif [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    # Apple Silicon: the binary must be arm64 (or universal). `file` reads the header only.
    case "$(file -b "$GUI_BIN" 2>/dev/null)" in
      *arm64*) good "แอปเป็นชนิด arm64 ตรงกับเครื่องนี้ (Apple Silicon)";;
      *) warn "แอปในแพ็กเกจไม่ใช่ชนิด arm64 อาจเปิดไม่ได้บนเครื่องนี้ (ขอแพ็กเกจใหม่จากผู้ดูแล)";;
    esac
  else
    case "$(file -b "$GUI_BIN" 2>/dev/null)" in
      *x86_64*) good "แอปเป็นชนิด x86_64 ตรงกับเครื่องนี้ (Intel)";;
      *) warn "เครื่องนี้ไม่ใช่ Apple Silicon และแอปในแพ็กเกจไม่ใช่ชนิด x86_64 อาจเปิดไม่ได้ (ใช้ Terminal และงานตั้งเวลาได้ตามปกติ)";;
    esac
  fi
  if [ "$GUI_ARCH_OK" = 1 ]; then
    # Replace an installed copy ONLY if it is ours (same bundle id). Anything else with this name is not touched.
    if [ -e "$GUI_DEST" ] || [ -L "$GUI_DEST" ]; then
      if qs_is_our_gui_app "$GUI_DEST"; then
        info "พบแอปที่ติดตั้งไว้แล้วที่ $GUI_DEST จะแทนที่ด้วยตัวใหม่จากแพ็กเกจ"
        GUI_REPLACE=1
      else
        warn "มี $GUI_DEST อยู่แล้ว แต่ไม่ใช่แอปนี้ (bundle id ไม่ตรง หรือเป็น symlink) จึงไม่ทับ: ย้ายหรือเปลี่ยนชื่อก่อน แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
        GUI_ARCH_OK=0
      fi
    else
      GUI_REPLACE=0
    fi
  fi
  if [ "$GUI_ARCH_OK" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      [ "$GUI_REPLACE" = 1 ] && echo "   (ทดลอง) จะรัน: osascript (ปิดแอปเดิมถ้าเปิดอยู่)"
      echo "   (ทดลอง) จะรัน: ditto $GUI_SRC $GUI_DEST แล้วลบป้ายกัก (xattr -dr com.apple.quarantine) และตรวจลายเซ็น (codesign --verify)"
    else
      GUI_NEW="$GUI_DEST.new-$$"
      if ! gui_quit; then
        bad "ปิดแอปเดิมไม่สำเร็จ ปิดแอป สรุปใบเสนอราคา เองแล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
      elif ! mkdir -p "$(dirname "$GUI_DEST")" || ! rm -rf "$GUI_NEW" || ! ditto "$GUI_SRC" "$GUI_NEW"; then
        rm -rf "$GUI_NEW"
        bad "คัดลอกแอปไม่สำเร็จ (แอปเดิมยังอยู่ครบ)"
      else
        xattr -dr com.apple.quarantine "$GUI_NEW" 2>/dev/null
        if ! codesign --verify --strict "$GUI_NEW" >/dev/null 2>&1; then
          rm -rf "$GUI_NEW"
          bad "ลายเซ็นของแอปไม่ผ่านการตรวจหลังคัดลอก จึงไม่ติดตั้ง (แอปเดิมยังอยู่ครบ) ขอแพ็กเกจใหม่จากผู้ดูแล"
        else
          # Swap in: the old copy goes only now, and only if it is ours (re-checked right before rm).
          if [ "$GUI_REPLACE" = 1 ]; then
            qs_is_our_gui_app "$GUI_DEST" && rm -rf "$GUI_DEST"
          fi
          # The destination must be really gone before mv: mv into an existing folder would put the new app INSIDE the
          # old one and still report success (old copy not fully removed, or the re-check above did not pass).
          if [ -e "$GUI_DEST" ] || [ -L "$GUI_DEST" ]; then
            rm -rf "$GUI_NEW"
            bad "ลบแอปเดิมที่ $GUI_DEST ไม่หมด จึงไม่ติดตั้งทับ: ปิดแอปแล้วลบหรือย้ายออกเอง แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
          elif mv "$GUI_NEW" "$GUI_DEST"; then
            GUI_INSTALLED=1
            good "ติดตั้งแอปแล้ว: $GUI_DEST"
          else
            rm -rf "$GUI_NEW"
            bad "ย้ายแอปเข้าที่ไม่สำเร็จ ($GUI_DEST) รันซ้ำได้"
          fi
        fi
      fi
    fi
    # (not in a dry run: there the folder may still be about to be moved to the right place)
    case "$APP_DIR" in
      "$QS_HOME_P/QuotationSummary/quotation-summary") ;;
      *) [ "$DRY_RUN" = 1 ] || warn "แอปหน้าต่างหาโปรแกรมที่ $QS_HOME_P/QuotationSummary/quotation-summary เท่านั้น (โปรแกรมนี้อยู่ที่ $APP_DIR) ให้ย้ายโฟลเดอร์ quotation-summary ไปไว้ที่ $QS_HOME_P/QuotationSummary/quotation-summary แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง";;
    esac
  fi
fi

# =====================================================================================================
step "โฟลเดอร์เก็บไฟล์ PDF"
# =====================================================================================================
SRC=""
if [ ! -f "$APP_DIR/config.json" ]; then
  warn "ยังไม่มี config.json (โหมดทดลอง) ข้ามขั้นตอนนี้"
elif ! qs_cfg_valid; then
  bad "config.json อ่านไม่ได้ (รูปแบบ JSON ผิด) ขอไฟล์ config.json ใหม่จากผู้ดูแล แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
  finish 1
else
  if grep -q -e "ตัวอย่าง" -e "Company A" "$APP_DIR/config.json" 2>/dev/null; then
    warn "config.json ยังมีชื่อบริษัทตัวอย่าง: เปิดไฟล์แล้วใส่ชื่อและเลขผู้เสียภาษีของบริษัทจริง"
  fi
  SRC="$(qs_source_folder)"
  if [ -z "$SRC" ]; then
    bad "config.json ไม่ได้ระบุ source_folder (โฟลเดอร์ PDF)"
  else
    if qs_is_protected "$SRC"; then
      warn "โฟลเดอร์ PDF อยู่ในโฟลเดอร์ที่ macOS กันงานตั้งเวลา: ควรแก้ source_folder ใน config.json ให้อยู่ที่ ~/QuotationSummary/"
    fi
    if [ -d "$SRC" ]; then
      good "มีโฟลเดอร์ PDF อยู่แล้ว: $SRC"
    else
      info "สร้างโฟลเดอร์ PDF: $SRC"
      if do_cmd mkdir -p "$SRC"; then [ "$DRY_RUN" = 1 ] || good "สร้างแล้ว"; else bad "สร้างโฟลเดอร์ไม่ได้: $SRC"; fi
    fi
    info "เปิดโฟลเดอร์ใน Finder: วางไฟล์ใบเสนอราคา PDF ที่ขายงานได้ลงในนี้"
    do_cmd open "$SRC" 2>/dev/null || true
  fi
fi

# =====================================================================================================
step "Claude Code (โปรแกรมของ Anthropic ที่ใช้อ่าน PDF ผ่านแพ็กเกจ Claude รายเดือน)"
# =====================================================================================================
BACKEND="$(qs_cfg_get claude_backend claude_code)"
CLAUDE=""
if [ "$BACKEND" = "api" ]; then
  info "config.json ตั้งให้ใช้ API key (claude_backend = api) ไม่ต้องใช้ Claude Code: ข้ามขั้นตอนนี้และขั้นตอน token"
  info "ใส่ API key ด้วยการดับเบิลคลิก scripts/set-api-key.command"
elif CLAUDE="$(qs_find_claude)"; then
  good "พบ Claude Code แล้ว: $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -n 1))"
else
  info "ยังไม่พบ Claude Code จะติดตั้งด้วยตัวติดตั้งทางการของ Anthropic ($CLAUDE_INSTALL_URL) โดยโหลดลงไฟล์ชั่วคราวให้ครบก่อน แล้วค่อยรัน"
  info "(ตามคู่มือ https://code.claude.com/docs/en/setup ไม่ต้องใช้รหัสผ่านเครื่อง ต้องต่ออินเทอร์เน็ต)"
  if ask_yn "ติดตั้ง Claude Code ตอนนี้ไหม?" y; then
    if [ "$DRY_RUN" = 1 ]; then
      echo "   (ทดลอง) จะรัน: curl -fsSL -o <ไฟล์ชั่วคราว> $CLAUDE_INSTALL_URL && bash <ไฟล์ชั่วคราว>"
    else
      # Download COMPLETELY first, run only if the download succeeded (`curl | bash` would start executing a
      # half-downloaded script when the connection drops). The temp file is removed on exit.
      (
        tmp="$(mktemp "${TMPDIR:-/tmp}/qs-claude-install.XXXXXX")" || exit 1
        trap 'rm -f "$tmp"' EXIT
        curl -fsSL -o "$tmp" "$CLAUDE_INSTALL_URL" && /bin/bash "$tmp"
      ) || warn "ตัวติดตั้ง Claude Code ทำงานไม่สำเร็จ (ดาวน์โหลดไม่ครบหรือติดตั้งไม่จบ: ตรวจอินเทอร์เน็ตแล้วลองใหม่)"
    fi
    if CLAUDE="$(qs_find_claude)"; then
      good "ติดตั้ง Claude Code แล้ว: $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -n 1))"
    elif [ "$DRY_RUN" != 1 ]; then
      bad "ติดตั้งแล้วแต่ยังหา claude ไม่เจอ: เปิด Terminal หน้าต่างใหม่ รัน claude --version แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    fi
  else
    warn "ข้ามการติดตั้ง Claude Code: โปรแกรมจะใช้ตัวอ่านสำรอง (pdftotext) ซึ่งแม่นยำน้อยกว่า"
  fi
fi

# =====================================================================================================
step "token ของ Claude (เก็บใน Keychain ของ macOS ไม่เขียนลงไฟล์)"
# =====================================================================================================
if [ "$BACKEND" = "api" ]; then
  info "ข้าม (ใช้ API key)"
else
  KC_SERVICE="$(qs_cfg_get claude_code_keychain_service quotation-summary-claude-code)"
  DO_TOKEN=1
  if qs_has_keychain_item "$KC_SERVICE"; then
    good "มี token ใน Keychain อยู่แล้ว ($KC_SERVICE) (ไม่แสดงค่า)"
    ask_yn "ต้องการเปลี่ยนเป็น token ใหม่ไหม? (ปกติไม่ต้อง)" n || DO_TOKEN=0
  fi
  if [ "$DO_TOKEN" = 1 ] && [ -z "$CLAUDE" ]; then
    warn "ยังไม่มี Claude Code จึงสร้าง token ไม่ได้: ติดตั้ง Claude Code ก่อนแล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    DO_TOKEN=0
  fi
  if [ "$DO_TOKEN" = 1 ]; then
    info "ขั้นตอน: (1) เบราว์เซอร์จะเปิดขึ้นมา ให้ล็อกอินด้วยบัญชี Claude ของคุณแล้วกดอนุญาต"
    info "         (2) Terminal จะแสดง token ยาว ๆ (ขึ้นต้น sk-ant-oat01-) ให้คัดลอกไว้ ตัวคำสั่งไม่ได้บันทึก token ไว้ที่ใด"
    info "         (3) จากนั้นสคริปต์นี้จะขอให้วาง token (ตัวอักษรไม่แสดงบนจอ) แล้วเก็บเข้า Keychain"
    if ask_yn "ล็อกอินและสร้าง token ตอนนี้ไหม?" y; then
      if ! do_cmd "$CLAUDE" setup-token; then
        warn "claude setup-token ไม่สำเร็จ (ถ้าล็อกอินไม่ผ่าน ลองใหม่ได้ด้วยการดับเบิลคลิก ติดตั้ง.command อีกครั้ง)"
      fi
      echo
      if ask_yn "คัดลอก token แล้ว พร้อมวางตอนนี้ไหม?" y; then
        info "ช่อง password: วาง token แล้วกด Enter (ไม่แสดงบนจอ) แล้ววางซ้ำอีกครั้งเพื่อยืนยัน"
        # -w as the LAST option makes `security` prompt (hidden) for the password; -U updates an existing item.
        # Same method as scripts/set-claude-token.command: the token never appears in argv, a file or a log.
        if do_cmd security add-generic-password -a "${USER:-$(id -un)}" -s "$KC_SERVICE" -U -w; then
          if [ "$DRY_RUN" = 1 ]; then
            info "(ทดลอง) ของจริงจะตรวจว่า token อยู่ใน Keychain แล้วแจ้งวันที่ควรสร้างใหม่"
          elif qs_has_keychain_item "$KC_SERVICE"; then
            clear_screen
            good "เก็บ token ใน Keychain แล้ว ($KC_SERVICE)"
            info "ล้างหน้าจอแล้ว เพื่อไม่ให้ token ที่แสดงไว้ค้างบนจอ เมื่อทำขั้นตอนทั้งหมดเสร็จ ให้ปิดหน้าต่าง Terminal นี้"
            info "token มีอายุ 1 ปี: ใส่ปฏิทินเตือนวันที่ $(date -v+350d +%Y-%m-%d 2>/dev/null || echo 'ประมาณ 1 ปีข้างหน้าลบ 2 สัปดาห์') เพื่อสร้างใหม่ก่อนหมดอายุ"
          else
            bad "สั่งเก็บแล้วแต่หา token ใน Keychain ไม่เจอ ลองใหม่ด้วยการดับเบิลคลิก scripts/set-claude-token.command"
          fi
        else
          bad "เก็บ token ไม่สำเร็จ (พิมพ์สองครั้งไม่ตรงกัน หรือกดยกเลิก) ลองใหม่ด้วยการดับเบิลคลิก scripts/set-claude-token.command"
        fi
      else
        info "ข้าม: ใส่ทีหลังได้ด้วยการดับเบิลคลิก scripts/set-claude-token.command"
        warn "token ที่แสดงไว้ยังค้างบนจอนี้: ใส่เสร็จแล้วให้ปิดหน้าต่าง Terminal นี้"
      fi
    else
      info "ข้าม: ทำทีหลังได้ด้วยการดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
    fi
  fi
fi

# =====================================================================================================
step "ตั้งค่าความเป็นส่วนตัวของบัญชี Claude (ทำเองในเบราว์เซอร์ 1 ครั้ง)"
# =====================================================================================================
info "เนื้อหา PDF (ชื่อลูกค้า ราคา) จะถูกส่งให้ Anthropic อ่าน ควรปิดการให้นำข้อมูลไปพัฒนาโมเดล:"
info "  เข้า claude.ai > Settings > Privacy แล้วปิด \"Help improve Claude\""
info "  ลิงก์ตรง: $PRIVACY_URL (ตามหน้า data usage ของ Claude Code เช็กเมื่อ 2026-11-09)"
if [ "$BACKEND" = "api" ]; then
  info "(ใช้ API key อยู่: ข้อมูลภายใต้เงื่อนไขธุรกิจ ไม่เกี่ยวกับการตั้งค่านี้)"
else
  # First run: default yes. Later runs (marker exists): default no, so re-running after adding PDFs
  # does not keep opening the page. The marker lives in .venv (not shipped in the zip, not copied by an update).
  PRIV_MARK="$APP_DIR/.venv/.privacy-reminder-shown"
  PRIV_DEF=y
  [ -f "$PRIV_MARK" ] && PRIV_DEF=n
  if ask_yn "เปิดหน้านี้ในเบราว์เซอร์ตอนนี้ไหม?" "$PRIV_DEF"; then
    do_cmd open "$PRIVACY_URL" 2>/dev/null || true
  fi
  if [ "$DRY_RUN" != 1 ] && [ -d "$APP_DIR/.venv" ]; then : > "$PRIV_MARK"; fi
fi

# =====================================================================================================
step "ทดสอบรันครั้งแรก (ทำตอนนี้เพื่อให้ macOS ถามสิทธิ์ Keychain ขณะที่คุณอยู่หน้าจอ)"
# =====================================================================================================
LOG="$HOME/Library/Logs/QuotationSummary/run.log"
N_PDF=0
[ -n "$SRC" ] && [ -d "$SRC" ] && N_PDF="$(qs_count_pdfs "$SRC")"
if [ "$DRY_RUN" != 1 ] && [ "$N_PDF" = "0" ]; then
  warn "โฟลเดอร์ PDF ยังว่าง จึงยังไม่ทดสอบ: ใส่ PDF 1-2 ไฟล์ แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง (ขั้นตอนอื่นจะถูกข้ามเร็ว ๆ)"
  info "ถ้า macOS ถามสิทธิ์ Keychain ให้กด \"Always Allow\" (อนุญาตเสมอ) เพื่อไม่ให้ถูกถามซ้ำตอนตั้งเวลา"
elif ask_yn "ทดสอบรันตอนนี้ไหม? (อ่าน PDF ทั้งหมดในโฟลเดอร์ อาจใช้เวลาหลายนาที)" y; then
  info "ถ้ามีหน้าต่างของ macOS ถามสิทธิ์ใช้ Keychain ให้กด \"Always Allow\" (อนุญาตเสมอ)"
  info "(1/2) รันโปรแกรมครั้งหนึ่งในหน้าต่างนี้"
  do_cmd /bin/bash "$APP_DIR/scripts/run.sh"
  RC=$?
  [ "$DRY_RUN" = 1 ] || info "รหัสออก $RC (0 = ปกติ, 2 = สร้าง Excel แล้วแต่มี PDF อ่านไม่ได้, อื่น ๆ = ดูข้อความด้านบน)"
  info "(2/2) สั่งงานตั้งเวลา (launchd) ให้รันหนึ่งครั้ง เหมือนที่จะเกิดขึ้นทุกวันจันทร์"
  DOMAIN="gui/$(id -u)"
  if [ "$DRY_RUN" = 1 ]; then
    echo "   (ทดลอง) จะรัน: launchctl kickstart $DOMAIN/$QS_LABEL แล้วรอจนเสร็จ และแสดงท้ายล็อก $LOG"
  elif launchctl kickstart "$DOMAIN/$QS_LABEL" >/dev/null 2>&1; then
    # Wait (max ~5 min) until the job is no longer running.
    sleep 2
    i=0
    while [ "$i" -lt 150 ]; do
      JOB_STATE="$(launchctl print "$DOMAIN/$QS_LABEL" 2>/dev/null | sed -n 's/^[[:space:]]*state = //p' | head -n 1)"
      [ "$JOB_STATE" = "running" ] || break
      sleep 2; i=$((i + 1))
    done
    good "งานตั้งเวลารันเสร็จแล้ว"
    if [ -f "$LOG" ]; then
      echo "   --- ท้ายล็อก $LOG ---"
      tail -n 25 "$LOG" | sed 's/^/   | /'
      echo "   ---"
    fi
  else
    bad "สั่ง launchd รันไม่สำเร็จ (ดูสถานะด้วย scripts/doctor.command)"
  fi
  if [ "$DRY_RUN" != 1 ]; then
    OUT="$(/bin/bash "$APP_DIR/scripts/run.sh" --print-output-path 2>/dev/null | tail -n 1)"
    if [ -n "$OUT" ] && [ -f "$OUT" ] && ask_yn "เปิด Excel ที่ได้เพื่อเทียบกับ PDF ไหม?" y; then
      open -a "Microsoft Excel" "$OUT" 2>/dev/null || open "$OUT"
    fi
  fi
  info "สำคัญ: เทียบตัวเลขใน Excel กับ PDF ต้นฉบับสักสองสามใบก่อนใช้งานจริง"
else
  info "ข้าม: ทดสอบทีหลังได้ด้วยการดับเบิลคลิก scripts/run-now.command"
fi

# =====================================================================================================
step "ตรวจรวมทุกอย่าง (doctor)"
# =====================================================================================================
/bin/bash "$SCRIPT_DIR/doctor.command" --no-pause
DOCTOR_RC=$?

echo
if [ "$DRY_RUN" = 1 ]; then
  echo "(ทดลอง) ผลตรวจด้านบนคือสถานะปัจจุบันของเครื่อง ยังไม่ได้ติดตั้งอะไรจริง"
  finish 0
fi
if [ -n "${QS_MOVED_FROM:-}" ] && [ -f "${QS_MOVED_FROM}/config.json" ]; then
  warn "อย่าลืม: โฟลเดอร์เดิม ${QS_MOVED_FROM} ยังมี config.json (ข้อมูลบริษัทจริง) ลบโฟลเดอร์นั้นทิ้ง (ลากไปถังขยะ)"
fi
if [ "$GUI_INSTALLED" = 1 ] && [ -d "$GUI_DEST" ]; then
  if ask_yn "เปิดแอป สรุปใบเสนอราคา ตอนนี้ไหม?" y; then
    open "$GUI_DEST" 2>/dev/null || warn "เปิดแอปไม่ได้: ดับเบิลคลิก $GUI_DEST เอง"
  fi
  info "ถ้าอยากให้ไอคอนอยู่ใน Dock ถาวร: คลิกขวา (หรือกด Control + คลิก) ที่ไอคอนแอปใน Dock > ตัวเลือก (Options) > เก็บไว้ใน Dock (Keep in Dock)"
fi
if [ "$DOCTOR_RC" -eq 0 ]; then
  echo "เสร็จแล้ว: ระบบจะรันเองทุกวันจันทร์ 08:00 (ถ้า Mac หลับอยู่ จะรันตอนตื่น) ตรวจซ้ำได้ทุกเมื่อด้วย ตรวจระบบ.command"
else
  echo "ยังมีรายการที่ไม่ผ่านด้านบน ทำตามข้อความหลังเครื่องหมาย -> แล้วดับเบิลคลิก ติดตั้ง.command อีกครั้ง"
fi
finish "$DOCTOR_RC"
