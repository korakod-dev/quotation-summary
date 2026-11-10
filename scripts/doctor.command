#!/bin/bash
# Doctor: double-click (or run) to check that everything is set up, and print a Thai pass/fail checklist.
# Read-only: it changes nothing and never prints the token (it only asks Keychain whether the item exists).
#
# Usage:  scripts/doctor.command [--no-pause]
#   --no-pause   do not wait for Enter at the end (used when setup-all.command calls it)
# Exit code: 0 = no "ไม่ผ่าน" item (warnings are allowed), 1 = at least one "ไม่ผ่าน".
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
# shellcheck source=scripts/_common.sh
. "$SCRIPT_DIR/_common.sh"

PAUSE=1
[ "${1:-}" = "--no-pause" ] && PAUSE=0
[ -t 0 ] || PAUSE=0     # nobody to wait for when not run from a terminal

N_PASS=0; N_WARN=0; N_FAIL=0
pass()  { N_PASS=$((N_PASS + 1)); echo "  [ผ่าน]     $1"; }
warnc() { N_WARN=$((N_WARN + 1)); echo "  [เตือน]    $1"; [ -n "${2:-}" ] && echo "             -> $2"; return 0; }
failc() { N_FAIL=$((N_FAIL + 1)); echo "  [ไม่ผ่าน]  $1"; [ -n "${2:-}" ] && echo "             -> $2"; return 0; }

echo "== ตรวจระบบ quotation-summary"
echo "   โฟลเดอร์โปรแกรม: $APP_DIR"
echo

# --- 1. location -------------------------------------------------------------------------------
if qs_is_protected "$APP_DIR"; then
  failc "โปรแกรมอยู่ในโฟลเดอร์ที่ macOS กันงานตั้งเวลา (Documents/Desktop/Downloads/iCloud)" \
        "ดับเบิลคลิก ติดตั้ง.command เพื่อย้ายไป ~/QuotationSummary/quotation-summary"
else
  pass "ตำแหน่งโปรแกรมใช้ได้ (ไม่อยู่ใน Documents/Desktop/Downloads/iCloud)"
fi

# --- 2. file permissions / quarantine -----------------------------------------------------------
NOT_EXEC=""
for f in "$APP_DIR"/scripts/*.sh "$APP_DIR"/scripts/*.command "$APP_DIR"/*.command; do
  [ -f "$f" ] || continue
  # _common.sh is only sourced, so it does not need the executable bit
  [ "$(basename "$f")" = "_common.sh" ] && continue
  [ -x "$f" ] || NOT_EXEC="$NOT_EXEC $(basename "$f")"
done
if [ -n "$NOT_EXEC" ]; then
  failc "ไฟล์สคริปต์ยังไม่มีสิทธิ์รัน:$NOT_EXEC" "ดับเบิลคลิก ติดตั้ง.command (จะแก้ให้) หรือรัน chmod +x scripts/*.sh scripts/*.command"
else
  pass "ไฟล์สคริปต์มีสิทธิ์รันครบ"
fi
if xattr -p com.apple.quarantine "$APP_DIR/scripts/run.sh" >/dev/null 2>&1; then
  warnc "ไฟล์ยังมีป้ายกักของ macOS (quarantine) จากการดาวน์โหลด" "ดับเบิลคลิก ติดตั้ง.command (จะลบป้ายให้)"
else
  pass "ไม่มีป้ายกัก (quarantine) ค้างอยู่"
fi

# --- 3. python / venv ---------------------------------------------------------------------------
if PY="$(qs_find_python)"; then
  pass "พบ Python 3.9+ ($PY)"
else
  PY=""
  failc "ไม่พบ Python 3.9 ขึ้นไป" "ดับเบิลคลิก ติดตั้ง.command (จะช่วยติดตั้ง) หรือโหลดจาก python.org/downloads/macos"
fi
if [ -x "$APP_DIR/.venv/bin/python" ] && "$APP_DIR/.venv/bin/python" -m qsum --version >/dev/null 2>&1; then
  pass "สภาพแวดล้อม Python ของโปรแกรม (.venv) ใช้งานได้"
else
  failc "สภาพแวดล้อม Python ของโปรแกรม (.venv) ยังไม่พร้อม" "ดับเบิลคลิก ติดตั้ง.command"
fi

# --- 4. config.json -----------------------------------------------------------------------------
CFG_OK=0
if [ ! -f "$APP_DIR/config.json" ]; then
  failc "ไม่พบไฟล์ config.json (ไฟล์ตั้งค่าชื่อบริษัท)" "ขอไฟล์ config.json จากผู้ดูแลแล้ววางในโฟลเดอร์โปรแกรม หรือรัน ติดตั้ง.command เพื่อสร้างจากแม่แบบ"
elif [ -z "$PY" ]; then
  warnc "ตรวจรูปแบบ config.json ไม่ได้ เพราะไม่มี Python" ""
elif qs_cfg_valid; then
  CFG_OK=1
  pass "config.json มีอยู่และอ่านได้ (รูปแบบ JSON ถูกต้อง)"
  if grep -q -e "ตัวอย่าง" -e "Company A" "$APP_DIR/config.json" 2>/dev/null; then
    warnc "config.json ยังมีชื่อบริษัทตัวอย่าง (Company A / ตัวอย่าง)" "เปิด config.json แล้วใส่ชื่อและเลขผู้เสียภาษีของบริษัทจริง"
  fi
else
  failc "config.json อ่านไม่ได้ (รูปแบบ JSON ผิด เช่น ลืมเครื่องหมาย , หรือ \")" "ขอไฟล์ config.json ใหม่จากผู้ดูแล"
fi

# --- 5. source folder ---------------------------------------------------------------------------
if [ "$CFG_OK" = 1 ]; then
  SRC="$(qs_source_folder)"
  if [ -z "$SRC" ]; then
    failc "config.json ไม่ได้ระบุ source_folder (โฟลเดอร์ PDF)" "เติม source_folder ใน config.json"
  elif [ ! -d "$SRC" ]; then
    failc "ไม่พบโฟลเดอร์ PDF: $SRC" "ดับเบิลคลิก ติดตั้ง.command (จะสร้างให้) หรือแก้ source_folder ใน config.json"
  else
    N="$(qs_count_pdfs "$SRC")"
    pass "พบโฟลเดอร์ PDF: $SRC (มี PDF $N ไฟล์)"
    [ "$N" = "0" ] && warnc "โฟลเดอร์ PDF ยังว่าง" "ใส่ไฟล์ใบเสนอราคา PDF ลงไป"
    if qs_is_protected "$SRC"; then
      failc "โฟลเดอร์ PDF อยู่ในโฟลเดอร์ที่ macOS กันงานตั้งเวลา" "ย้ายไปไว้ เช่น ~/QuotationSummary/ แล้วแก้ source_folder"
    fi
  fi
fi

# --- 6. launchd job -----------------------------------------------------------------------------
PLIST="${QS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}/$QS_LABEL.plist"
if [ ! -f "$PLIST" ]; then
  failc "ยังไม่มีไฟล์ตั้งเวลา ($PLIST)" "ดับเบิลคลิก ติดตั้ง.command"
else
  PLIST_SCRIPT=""
  [ -n "$PY" ] && PLIST_SCRIPT="$("$PY" -I -c 'import plistlib, sys
with open(sys.argv[1], "rb") as f:
    print(plistlib.load(f)["ProgramArguments"][1])' "$PLIST" 2>/dev/null)"
  PLIST_APP=""
  [ -n "$PLIST_SCRIPT" ] && PLIST_APP="$(cd "$(dirname "$PLIST_SCRIPT")/.." 2>/dev/null && pwd -P)"
  if [ -n "$PLIST_APP" ] && [ "$PLIST_APP" != "$APP_DIR" ]; then
    failc "ไฟล์ตั้งเวลาชี้ไปโปรแกรมที่อื่น: $PLIST_APP" "ดับเบิลคลิก ติดตั้ง.command จากโฟลเดอร์นี้เพื่อตั้งใหม่"
  elif launchctl print "gui/$(id -u)/$QS_LABEL" >/dev/null 2>&1; then
    pass "งานตั้งเวลา (launchd) ลงทะเบียนแล้ว: $QS_LABEL"
  else
    failc "มีไฟล์ตั้งเวลาแต่ยังไม่ได้ลงทะเบียนกับ launchd" "ดับเบิลคลิก ติดตั้ง.command"
  fi
fi
LOG="$HOME/Library/Logs/QuotationSummary/run.log"
if [ -f "$LOG" ]; then
  pass "มีล็อกการรัน (แก้ไขล่าสุด $(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$LOG" 2>/dev/null)): $LOG"
else
  warnc "ยังไม่มีล็อกการรัน (ยังไม่เคยรันเลย)" "ดับเบิลคลิก ติดตั้ง.command แล้วเลือกทดสอบรัน หรือดับเบิลคลิก scripts/run-now.command"
fi

# --- 6b. desktop app (window) ---------------------------------------------------------------------------------
# Optional: the weekly job and the Terminal tools work without it, so a missing app is only a warning.
GUI_APP="$(qs_gui_installed_path)"
if [ -e "$GUI_APP" ] || [ -L "$GUI_APP" ]; then
  if qs_is_our_gui_app "$GUI_APP"; then
    if codesign --verify --strict "$GUI_APP" >/dev/null 2>&1; then
      pass "แอปหน้าต่าง สรุปใบเสนอราคา ติดตั้งแล้ว ($GUI_APP)"
    else
      warnc "แอปหน้าต่างติดตั้งอยู่ แต่ลายเซ็นไม่ผ่านการตรวจ ($GUI_APP)" "ดับเบิลคลิก ติดตั้ง.command เพื่อติดตั้งแอปใหม่"
    fi
  else
    warnc "มี $GUI_APP อยู่ แต่ไม่ใช่แอปของโปรแกรมนี้ (bundle id ไม่ตรง หรือเป็น symlink)" "ย้ายหรือเปลี่ยนชื่อไฟล์นั้น แล้วดับเบิลคลิก ติดตั้ง.command"
  fi
else
  warnc "ยังไม่ได้ติดตั้งแอปหน้าต่าง สรุปใบเสนอราคา (ไม่บังคับ)" "ดับเบิลคลิก ติดตั้ง.command (ต้องมีแอปในแพ็กเกจ build/QuotationSummary.app)"
fi

# --- 7. claude + token ----------------------------------------------------------------------------
BACKEND="claude_code"
[ "$CFG_OK" = 1 ] && BACKEND="$(qs_cfg_get claude_backend claude_code)"
if [ "$BACKEND" = "api" ]; then
  KC_SERVICE="$(qs_cfg_get keychain_service quotation-summary-anthropic)"
  if qs_has_keychain_item "$KC_SERVICE"; then
    pass "พบ API key ใน Keychain ($KC_SERVICE) (ไม่แสดงค่า)"
  else
    failc "ไม่พบ API key ใน Keychain ($KC_SERVICE)" "ดับเบิลคลิก scripts/set-api-key.command"
  fi
else
  if CLAUDE="$(qs_find_claude)"; then
    VER="$("$CLAUDE" --version 2>/dev/null | head -n 1)"
    if [ -n "$VER" ]; then
      pass "พบ Claude Code ที่ $CLAUDE (เวอร์ชัน: $VER)"
    else
      failc "พบ Claude Code ที่ $CLAUDE แต่สั่ง --version ไม่ได้" "ติดตั้งใหม่: ดับเบิลคลิก ติดตั้ง.command (หรือรัน claude update)"
    fi
  else
    failc "ไม่พบโปรแกรม Claude Code" "ดับเบิลคลิก ติดตั้ง.command (จะช่วยติดตั้ง)"
  fi
  KC_SERVICE="$(qs_cfg_get claude_code_keychain_service quotation-summary-claude-code)"
  if qs_has_keychain_item "$KC_SERVICE"; then
    pass "พบ token ของ Claude ใน Keychain ($KC_SERVICE) (ไม่แสดงค่า)"
  else
    failc "ไม่พบ token ของ Claude ใน Keychain ($KC_SERVICE)" "ดับเบิลคลิก ติดตั้ง.command (ขั้นตอน token) หรือ scripts/set-claude-token.command"
  fi
fi

# --- 8. pdftotext (optional fallback reader) -------------------------------------------------------
if PDFT="$(qs_find_bin pdftotext)"; then
  pass "พบ pdftotext ($PDFT) (ตัวอ่านสำรอง)"
else
  warnc "ไม่พบ pdftotext (ตัวอ่านสำรอง ไม่บังคับ)" "ถ้ามี Homebrew: brew install poppler (ไม่ติดตั้งก็ใช้ Claude อ่านได้ปกติ)"
fi

echo
echo "สรุป: ผ่าน $N_PASS, เตือน $N_WARN, ไม่ผ่าน $N_FAIL"
if [ "$N_FAIL" -eq 0 ]; then
  echo "ผลรวม: พร้อมใช้งาน"
else
  echo "ผลรวม: ยังมีรายการที่ไม่ผ่าน ทำตามข้อความหลังเครื่องหมาย -> ของแต่ละรายการ"
fi

if [ "$PAUSE" = 1 ]; then
  echo
  echo "กด Enter เพื่อปิดหน้าต่าง"
  read -r _
fi
[ "$N_FAIL" -eq 0 ]
