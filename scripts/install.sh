#!/bin/bash
# Install: create .venv + dependencies, create config.json, register the weekly launchd job.
#
# Usage:  scripts/install.sh [--no-load]
#   --no-load   write the plist but do not register it with launchd (for testing)
# Env overrides: QS_WEEKDAY (0-7, 1=Monday, default 1), QS_HOUR (default 8), QS_MINUTE (default 0),
#                QS_LAUNCH_AGENTS_DIR (default ~/Library/LaunchAgents)
#                QS_SETUP_ALL=1 (set by setup-all.command: it handles pdftotext / Claude Code / token itself,
#                so the follow-up hints at the end of this script are not printed)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.korakod.quotation-summary"
AGENTS_DIR="${QS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/QuotationSummary"
LOAD=1
[ "${1:-}" = "--no-load" ] && LOAD=0

# Schedule values: validate and force base 10 (bash treats "08"/"09" as invalid octal in $(( ))).
WEEKDAY="${QS_WEEKDAY:-1}"; HOUR="${QS_HOUR:-8}"; MINUTE="${QS_MINUTE:-0}"
for v in "$WEEKDAY" "$HOUR" "$MINUTE"; do
  case "$v" in ''|*[!0-9]*) echo "QS_WEEKDAY/QS_HOUR/QS_MINUTE ต้องเป็นตัวเลข (ตอนนี้: '$v')" >&2; exit 1;; esac
done
WEEKDAY=$((10#$WEEKDAY)); HOUR=$((10#$HOUR)); MINUTE=$((10#$MINUTE))
if [ "$WEEKDAY" -gt 7 ] || [ "$HOUR" -gt 23 ] || [ "$MINUTE" -gt 59 ]; then
  echo "ค่าเวลาเกินช่วง (วัน 0-7, ชั่วโมง 0-23, นาที 0-59)" >&2; exit 1
fi
case "$WEEKDAY" in
  0|7) DAY_NAME="อาทิตย์";; 1) DAY_NAME="จันทร์";; 2) DAY_NAME="อังคาร";; 3) DAY_NAME="พุธ";;
  4) DAY_NAME="พฤหัสบดี";; 5) DAY_NAME="ศุกร์";; 6) DAY_NAME="เสาร์";;
esac

# macOS privacy (TCC) blocks launchd jobs from Documents / Desktop / Downloads / iCloud Drive.
case "$APP_DIR" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*|"$HOME/Library/Mobile Documents"/*)
    echo "คำเตือน: โปรแกรมอยู่ใน $APP_DIR ซึ่ง macOS กันงานตั้งเวลาไม่ให้อ่าน (Excel จะไม่ถูกสร้างตอนตั้งเวลา)" >&2
    echo "         ย้ายโฟลเดอร์ไปไว้ที่ ~/QuotationSummary/ แล้วรัน scripts/install.sh ใหม่" >&2
    ;;
esac

echo "== 1/4 หา Python 3.9+"
PYBIN=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PYBIN="$(command -v "$cand")"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "ไม่พบ Python 3.9 ขึ้นไป. ติดตั้งจาก https://www.python.org/downloads/macos/ หรือรัน: xcode-select --install" >&2
  exit 1
fi
echo "   ใช้ $PYBIN ($("$PYBIN" --version))"

echo "== 2/4 สร้าง .venv และติดตั้งแพ็กเกจ"
[ -x "$APP_DIR/.venv/bin/python" ] || "$PYBIN" -m venv "$APP_DIR/.venv"
# Re-runs must not need the internet: pip runs only when requirements.txt changed since the last successful
# install (hash kept in .venv/.requirements.sha256, written only after pip succeeded) or the venv is new.
# If pip fails but an earlier successful install exists, warn and carry on with the packages already there.
REQ_HASH="$(shasum -a 256 "$APP_DIR/requirements.txt" | cut -d' ' -f1)"
HASH_FILE="$APP_DIR/.venv/.requirements.sha256"
venv_usable() { "$APP_DIR/.venv/bin/python" -c 'import sys' >/dev/null 2>&1; }
if venv_usable && [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE")" = "$REQ_HASH" ]; then
  echo "   แพ็กเกจครบแล้ว (requirements.txt ไม่เปลี่ยนจากครั้งก่อน) ข้ามการดาวน์โหลด ไม่ต้องใช้อินเทอร์เน็ต"
elif "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip \
     && "$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"; then
  printf '%s\n' "$REQ_HASH" > "$HASH_FILE"
elif venv_usable && [ -f "$HASH_FILE" ]; then
  echo "   คำเตือน: ติดตั้งแพ็กเกจใหม่ไม่สำเร็จ (น่าจะไม่มีอินเทอร์เน็ต) ใช้ชุดแพ็กเกจเดิมที่ติดตั้งไว้ต่อไป" >&2
else
  echo "ติดตั้งแพ็กเกจไม่สำเร็จ (ต้องต่ออินเทอร์เน็ต) ต่อเน็ตแล้วรันใหม่" >&2
  exit 1
fi

if [ "${QS_SETUP_ALL:-}" != "1" ] && ! command -v pdftotext >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/pdftotext ] && [ ! -x /usr/local/bin/pdftotext ]; then
  echo "   หมายเหตุ: ไม่พบ pdftotext (ใช้เป็นตัวสำรองเมื่อใช้ Claude ไม่ได้). ติดตั้งด้วย: brew install poppler"
fi

# Claude Code (the default reader, runs on the monthly Claude plan). Only a hint: the install still works without it.
if [ "${QS_SETUP_ALL:-}" != "1" ] && ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ] && [ ! -x /opt/homebrew/bin/claude ] \
   && [ ! -x /usr/local/bin/claude ] && [ ! -x "$HOME/.claude/local/claude" ]; then
  echo "   หมายเหตุ: ยังไม่พบโปรแกรม Claude Code (ตัวอ่านหลักที่ใช้แพ็กเกจ Claude รายเดือน)"
  echo "   ทำตามนี้: 1) ติดตั้ง Claude Code (ดูหัวข้อ \"ใช้แพ็กเกจ Claude รายเดือน\" ใน README)"
  echo "            2) รัน 'claude setup-token' ใน Terminal"
  echo "            3) ดับเบิลคลิก scripts/set-claude-token.command แล้ววาง token"
fi

echo "== 3/4 ไฟล์ตั้งค่า"
if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
  echo "   สร้าง $APP_DIR/config.json แล้ว -> เปิดแก้ source_folder และชื่อ/เลขภาษีของบริษัททั้ง 3"
else
  echo "   มี config.json อยู่แล้ว (ไม่เขียนทับ)"
fi
mkdir -p "$LOG_DIR" "$AGENTS_DIR"

echo "== 4/4 ตั้งเวลารันรายสัปดาห์ (launchd)"
# plistlib handles XML escaping, so Thai/space characters in paths are safe.
"$APP_DIR/.venv/bin/python" - "$PLIST" "$LABEL" "$APP_DIR" "$LOG_DIR" \
  "$WEEKDAY" "$HOUR" "$MINUTE" <<'PY'
import plistlib, sys
plist, label, app_dir, log_dir, weekday, hour, minute = sys.argv[1:8]
data = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", app_dir + "/scripts/run.sh"],
    "WorkingDirectory": app_dir,
    # Weekday 1 = Monday. If the Mac is asleep at that time, launchd runs the job on wake.
    "StartCalendarInterval": {"Weekday": int(weekday), "Hour": int(hour), "Minute": int(minute)},
    # Tells qsum who started the run (recorded in state.json, shown in the desktop app as "ตั้งเวลา").
    "EnvironmentVariables": {"QS_TRIGGER": "schedule"},
    "StandardOutPath": log_dir + "/run.log",
    "StandardErrorPath": log_dir + "/run.log",
    "RunAtLoad": False,
    "ProcessType": "Background",
}
with open(plist, "wb") as f:
    plistlib.dump(data, f)
PY
plutil -lint "$PLIST" >/dev/null
echo "   เขียน $PLIST แล้ว"

if [ "$LOAD" = "1" ]; then
  DOMAIN="gui/$(id -u)"
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true   # re-install = replace
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$DOMAIN/$LABEL" || true
  echo "   ลงทะเบียนกับ launchd แล้ว (ทุกวัน${DAY_NAME} $(printf '%02d:%02d' "$HOUR" "$MINUTE"))"
  echo "   ดูสถานะ: launchctl print $DOMAIN/$LABEL | head -20"
else
  echo "   (--no-load: ยังไม่ได้ลงทะเบียนกับ launchd)"
fi
if [ "${QS_SETUP_ALL:-}" = "1" ]; then exit 0; fi   # setup-all.command continues with the next steps itself
echo
echo "ขั้นต่อไป: 1) ดับเบิลคลิก scripts/set-claude-token.command (ใส่ token ของแพ็กเกจ Claude รายเดือน)  2) แก้ config.json  3) ดับเบิลคลิก scripts/run-now.command"
echo "         (ใช้ API key แทนแพ็กเกจรายเดือน: ตั้ง \"claude_backend\": \"api\" ใน config.json แล้วดับเบิลคลิก scripts/set-api-key.command)"
