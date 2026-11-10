#!/bin/bash
# Maintainer side: build the zip to send to the user.
#
# Usage:  scripts/make-package.sh [OUTPUT.zip] [--allow-no-config] [--no-app]
#   OUTPUT.zip         default: ~/Desktop/quotation-summary-YYYYMMDD.zip. Must end with ".zip" and must NOT be
#                      inside any git work tree (the zip holds the real config.json: it must never be committable).
#   --allow-no-config  build even if config.json is missing (the user then gets the example company names)
#   --no-app           do not build or pack the desktop app (the user then gets the Terminal tools only)
#
# Desktop app: unless --no-app, build/QuotationSummary.app is deleted and rebuilt first with
# ${QS_BUILD_APP_SCRIPT:-scripts/build-app.sh} --out build (QS_BUILD_APP_SCRIPT is a test hook). A failing build
# aborts the whole package. gui/ (the Swift sources) is never packed: the user gets the built app only.
#
# What goes in (ALLOW-LIST, not a deny-list): exactly the files git knows for this app folder (`git ls-files`,
# so the files must be `git add`-ed; commit is not needed) PLUS the git-ignored config.json (real company data:
# send the zip privately) PLUS exactly the files under build/QuotationSummary.app/ (the freshly built app; nothing else
# from build/). Anything else lying in the folder (config.json.bak-*, *.xlsx, *.pdf, .env*, *.log,
# .venv, __pycache__, .DS_Store ...) is NOT packed. A few tracked-by-mistake files are still dropped
# (tests/samples.local.json = customer data; gui/* and build/* = built or sources of the app, handled separately;
# scripts/build-app.sh = maintainer-side tool that needs gui/).
# After building, the zip is extracted (like Finder does) into a
# temporary folder and its file list must equal the allow-list exactly.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
NAME="$(basename "$APP_DIR")"
PARENT="$(dirname "$APP_DIR")"

die() { echo "$1" >&2; exit 1; }

OUT=""
ALLOW_NO_CONFIG=0
WITH_APP=1
for arg in "$@"; do
  case "$arg" in
    --allow-no-config) ALLOW_NO_CONFIG=1;;
    --no-app) WITH_APP=0;;
    -*) echo "ไม่รู้จักตัวเลือก: $arg" >&2; exit 2;;
    *) OUT="$arg";;
  esac
done
[ -n "$OUT" ] || OUT="$HOME/Desktop/quotation-summary-$(date +%Y%m%d).zip"

# ---- validate the output path BEFORE touching anything -------------------------------------------------
# only ever overwrite a ".zip": a typo must not delete some other file via `rm -f`.
case "$OUT" in
  *.zip) ;;
  *) die "ไฟล์ปลายทางต้องลงท้ายด้วย .zip (ตอนนี้: $OUT) สคริปต์จะลบไฟล์เดิมที่ชื่อเดียวกันก่อนแพ็ก จึงไม่รับชื่ออื่น";;
esac
case "$OUT" in
  /*) ;;
  *) OUT="$(pwd -P)/$OUT";;
esac
# Resolve symlinks in the folder part: walk up to the deepest existing folder, `cd`+`pwd -P` there, then
# re-append the not-yet-existing part. A ".." inside the not-yet-existing part cannot be resolved safely.
out_dir="$(dirname "$OUT")"
out_base="$(basename "$OUT")"
out_rest=""
while [ ! -d "$out_dir" ]; do
  part="$(basename "$out_dir")"
  case "$part" in
    ..) die "ที่เก็บไฟล์ zip มี .. ในส่วนที่ยังไม่มีโฟลเดอร์: ระบุเป็นพาธเต็มแทน ($OUT)";;
    .) ;;
    *) out_rest="/$part$out_rest";;
  esac
  out_dir="$(dirname "$out_dir")"
done
OUT_DIR="$(cd "$out_dir" && pwd -P)$out_rest"
OUT="$OUT_DIR/$out_base"
[ ! -L "$OUT" ] || die "ปลายทางเป็น symlink: ไม่รับ ($OUT)"
[ ! -d "$OUT" ] || die "ปลายทางเป็นโฟลเดอร์: ระบุเป็นชื่อไฟล์ .zip ($OUT)"
case "$OUT" in
  "$APP_DIR"/*) die "ไฟล์ zip ต้องไม่อยู่ในโฟลเดอร์โปรแกรมเอง: $OUT";;
esac
# refuse any place inside a git work tree (a `.git` folder or file in OUT_DIR or any parent), not only
# the app folder: otherwise `git add -A` would stage a zip that contains the real config.json.
walk="$OUT_DIR"
while :; do
  if [ -e "$walk/.git" ]; then
    die "ไฟล์ zip ต้องไม่อยู่ในโฟลเดอร์ที่ git ดูแลอยู่ ($walk) เพราะข้างในมี config.json จริง อาจถูก commit และ push ขึ้น GitHub โดยไม่ตั้งใจ ให้เก็บนอก repo เช่น ~/Desktop"
  fi
  [ "$walk" != "/" ] || break
  walk="$(dirname "$walk")"
done

# ---- desktop app: always rebuilt, never reused --------------------------------------------------------------
APP_BUNDLE_REL="build/QuotationSummary.app"
if [ "$WITH_APP" = 1 ]; then
  echo "== 1/4 สร้างแอปหน้าต่าง (build/QuotationSummary.app)"
  rm -rf "$APP_DIR/$APP_BUNDLE_REL"      # fixed path under the app folder: a stale bundle must never be packed
  BUILD_SCRIPT="${QS_BUILD_APP_SCRIPT:-$APP_DIR/scripts/build-app.sh}"
  # build-app.sh prints progress on stderr and the bundle path as the LAST stdout line.
  if ! /bin/bash "$BUILD_SCRIPT" --out "$APP_DIR/build" >/dev/null; then
    die "สร้างแอปหน้าต่างไม่สำเร็จ จึงไม่สร้าง zip (ดูข้อความด้านบน; ถ้าไม่ต้องการแอป ใช้ --no-app)"
  fi
  [ -x "$APP_DIR/$APP_BUNDLE_REL/Contents/MacOS/QuotationSummary" ] && [ -f "$APP_DIR/$APP_BUNDLE_REL/Contents/Info.plist" ] \
    || die "สร้างแอปเสร็จแต่ไม่พบ $APP_BUNDLE_REL/Contents/MacOS/QuotationSummary หรือ Info.plist จึงไม่สร้าง zip"
  echo "   สร้างแอปแล้ว"
else
  echo "== 1/4 ข้ามการสร้างแอปหน้าต่าง (--no-app)"
fi

# ---- allow-list: tracked files of the app folder -----------------------------------------------------------
echo "== 2/4 ตรวจไฟล์ก่อนแพ็ก"
command -v git >/dev/null 2>&1 || die "ไม่พบคำสั่ง git: สคริปต์แพ็กใช้รายชื่อไฟล์จาก git"
LIST_RAW="$(mktemp)"
LIST="$(mktemp)"
CHECK_DIR="$(mktemp -d)"
trap 'rm -rf "$CHECK_DIR" "$LIST_RAW" "$LIST" "$LIST.sorted" "$LIST.found" "$LIST.diff"' EXIT
# quotepath=off: Thai file names come out as plain UTF-8, one per line.
git -C "$APP_DIR" -c core.quotepath=off ls-files > "$LIST_RAW" 2>/dev/null \
  || die "อ่านรายชื่อไฟล์จาก git ไม่ได้: โฟลเดอร์ $APP_DIR ต้องอยู่ใน git repo"
[ -s "$LIST_RAW" ] || die "git ไม่พบไฟล์ของโปรแกรมในโฟลเดอร์นี้ (ยังไม่ได้ git add?)"
# Drop files that must never ship even if somebody tracked them by mistake.
: > "$LIST"
while IFS= read -r f; do
  [ -e "$APP_DIR/$f" ] || continue          # listed in the index but deleted from disk
  case "$f" in
    .venv/*|*/.venv/*|.git/*|*/__pycache__/*|*.pyc|*.DS_Store|tests/samples.local.json|config.json)
      echo "   ข้ามไฟล์ที่ไม่ควรแพ็ก (git ติดตามไว้): $f" >&2; continue;;
    gui/*|build/*|scripts/build-app.sh|docs/*|.gitignore)
      # Swift sources stay with the developer; build/ only ever holds the app, added below; build-app.sh is a
      # maintainer-side tool (it needs gui/, which is not packed), so it is not shipped either. docs/ (README
      # screenshots, ~900 KB) and .gitignore are repo-only files; the package carries README.md and LICENSE.
      continue;;
  esac
  printf '%s\n' "$f" >> "$LIST"
done < "$LIST_RAW"
if [ -f "$APP_DIR/config.json" ]; then printf '%s\n' "config.json" >> "$LIST"; fi
# The built app: exactly the regular files under the bundle (Finder metadata excluded), nothing else from build/.
if [ "$WITH_APP" = 1 ]; then
  ( cd "$APP_DIR" && find "$APP_BUNDLE_REL" -type f ! -name .DS_Store | LC_ALL=C sort ) >> "$LIST"
fi

for f in "ติดตั้ง.command" "ตรวจระบบ.command" "ถอนการติดตั้ง.command" scripts/setup-all.command scripts/doctor.command \
         scripts/uninstall-all.command scripts/_common.sh \
         scripts/install.sh scripts/run.sh scripts/status.sh qsum/main.py qsum/state.py qsum/status.py \
         requirements.txt config.example.json README.md; do
  [ -e "$APP_DIR/$f" ] || die "ไม่พบไฟล์ที่ต้องมี: $f"
  grep -qxF -- "$f" "$LIST" || die "ไฟล์ $f ยังไม่ได้อยู่ใน git (ยังไม่ได้ git add) จึงไม่ถูกแพ็ก: รัน git add แล้วลองใหม่"
done
if [ -f "$APP_DIR/config.json" ]; then
  # Valid JSON?  (python3 from PATH is enough here: the maintainer's Mac has one)
  python3 -I -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$APP_DIR/config.json" \
    || die "config.json ไม่ใช่ JSON ที่ถูกต้อง: แก้ก่อนแพ็ก"
  echo "   config.json: พบ และอ่านได้"
  if grep -q -e "ตัวอย่าง" -e "Company A" "$APP_DIR/config.json"; then
    echo "   คำเตือน: config.json ยังมีชื่อบริษัทตัวอย่าง (Company A / ตัวอย่าง) ตรวจว่าใส่ข้อมูลจริงแล้ว" >&2
  fi
elif [ "$ALLOW_NO_CONFIG" = 1 ]; then
  echo "   คำเตือน: ไม่มี config.json แพ็กนี้จะไม่มีชื่อบริษัทจริง (ใช้ --allow-no-config)" >&2
else
  echo "ไม่พบ config.json (มีข้อมูลบริษัทจริง ไม่อยู่ใน git) ถ้าแพ็กไป ผู้ใช้จะได้แต่ชื่อบริษัทตัวอย่าง" >&2
  echo "ใส่ไฟล์ไว้ที่ $APP_DIR/config.json หรือรันใหม่ด้วย --allow-no-config" >&2
  exit 1
fi
echo "   จะแพ็ก $(wc -l < "$LIST" | tr -d ' ') ไฟล์ (เฉพาะที่ git ติดตามอยู่ + config.json + แอปที่สร้างใหม่)"

echo "== 3/4 สร้างไฟล์ zip: $OUT"
mkdir -p "$OUT_DIR"
rm -f "$OUT"     # zip would otherwise ADD to an existing archive and keep stale entries
(
  cd "$PARENT"
  # -@ reads the exact file names from stdin (allow-list). -X: no extra uid/gid/time attributes
  # (Unix permission bits, i.e. +x on the scripts, are kept).
  while IFS= read -r f; do printf '%s\n' "$NAME/$f"; done < "$LIST" | zip -q -X "$OUT" -@
)

echo "== 4/4 ตรวจไฟล์ที่ได้"
# ditto is the extraction engine behind Finder's double-click unzip (Archive Utility), so this also proves
# that the Thai file names and the executable bits survive the way the user will unzip it.
if command -v ditto >/dev/null 2>&1; then ditto -x -k "$OUT" "$CHECK_DIR"; else unzip -q "$OUT" -d "$CHECK_DIR"; fi
ROOT="$CHECK_DIR/$NAME"
FAILED=0
# check "description" COMMAND... -> prints pass/fail for the command's status (errexit-safe: used inside `if`).
check() {
  local desc="$1"; shift
  if "$@"; then echo "   [ผ่าน] $desc"; else echo "   [ไม่ผ่าน] $desc" >&2; FAILED=1; fi
}
same_file()     { [ -f "$1" ] && cmp -s "$1" "$2"; }
is_exec_file()  { [ -f "$1" ] && [ -x "$1" ]; }
scripts_exec()  { [ -x "$ROOT/scripts/setup-all.command" ] && [ -x "$ROOT/scripts/install.sh" ] && [ -x "$ROOT/scripts/run.sh" ] \
                  && [ -x "$ROOT/scripts/status.sh" ]; }
# The extracted app (like Finder's unzip leaves it): binary executable, signature still valid, and which CPU it is for.
app_binary_exec() { [ -x "$ROOT/$APP_BUNDLE_REL/Contents/MacOS/QuotationSummary" ]; }
app_signature()   { codesign --verify --strict "$ROOT/$APP_BUNDLE_REL" >/dev/null 2>&1; }
app_arch_report() {
  local kind
  kind="$(file -b "$ROOT/$APP_BUNDLE_REL/Contents/MacOS/QuotationSummary" 2>/dev/null)"
  echo "   ชนิดของแอป: ${kind:-อ่านไม่ได้}"
  case "$kind" in *arm64*|*x86_64*) return 0;; *) return 1;; esac
}
no_junk()       { [ -z "$(find "$ROOT" \( -name __pycache__ -o -name '*.pyc' -o -name .DS_Store -o -name .git \) -print -quit)" ]; }
# The extracted file list must equal the allow-list: nothing extra (stray data files), nothing missing.
only_allowed() {
  LC_ALL=C sort "$LIST" > "$LIST.sorted"
  ( cd "$ROOT" && find . -type f | sed 's|^\./||' ) | LC_ALL=C sort > "$LIST.found"
  if diff "$LIST.sorted" "$LIST.found" > "$LIST.diff" 2>&1; then rm -f "$LIST.diff"; return 0; fi
  sed 's/^/      /' "$LIST.diff" >&2; rm -f "$LIST.diff"; return 1
}
if [ -f "$APP_DIR/config.json" ]; then
  check "config.json อยู่ใน zip และตรงกับต้นฉบับ" same_file "$ROOT/config.json" "$APP_DIR/config.json"
else
  check "ไม่มี config.json ใน zip (ตามที่ขอด้วย --allow-no-config)" test ! -e "$ROOT/config.json"
fi
check "รายชื่อไฟล์ใน zip ตรงกับรายการที่อนุญาตทุกไฟล์ (ไม่มีไฟล์แปลกปลอม ไม่ขาด)" only_allowed
check "ติดตั้ง.command อยู่ใน zip และมีสิทธิ์รัน" is_exec_file "$ROOT/ติดตั้ง.command"
check "ถอนการติดตั้ง.command อยู่ใน zip และมีสิทธิ์รัน" is_exec_file "$ROOT/ถอนการติดตั้ง.command"
check "scripts/uninstall-all.command อยู่ใน zip และมีสิทธิ์รัน" is_exec_file "$ROOT/scripts/uninstall-all.command"
check "สคริปต์ใน scripts/ ยังมีสิทธิ์รัน" scripts_exec
if [ "$WITH_APP" = 1 ]; then
  check "แอปหน้าต่างอยู่ใน zip และตัวโปรแกรมมีสิทธิ์รัน" app_binary_exec
  check "ลายเซ็นของแอปหลังแตก zip ยังผ่าน (codesign --verify --strict)" app_signature
  check "รู้ชนิด CPU ของแอป (arm64 สำหรับ Mac Apple Silicon)" app_arch_report
else
  check "ไม่มีแอปหน้าต่างใน zip (ตามที่ขอด้วย --no-app)" test ! -e "$ROOT/build"
fi
check "ไม่มีซอร์ส Swift (gui/) ใน zip" test ! -e "$ROOT/gui"
check "ไม่มี .venv" test ! -e "$ROOT/.venv"
check "ไม่มี tests/samples.local.json (ข้อมูลลูกค้า)" test ! -e "$ROOT/tests/samples.local.json"
check "ไม่มี __pycache__ / .DS_Store / .git" no_junk
if [ "$FAILED" -ne 0 ]; then
  echo "แพ็กไม่ครบหรือมีของที่ไม่ควรมี: อย่าส่งไฟล์นี้ ($OUT)" >&2
  exit 1
fi

echo
echo "เสร็จแล้ว: $OUT ($(du -h "$OUT" | cut -f1))"
echo "ส่งให้ผู้ใช้ทางช่องทางส่วนตัวเท่านั้น (ในไฟล์มีชื่อบริษัทและเลขผู้เสียภาษีจริง)"
echo "บอกผู้ใช้: ดับเบิลคลิกไฟล์ zip เพื่อแตกไฟล์ แล้วดับเบิลคลิก ติดตั้ง.command ในโฟลเดอร์ quotation-summary"
echo "ถ้า macOS ไม่ยอมเปิด (macOS 15 ขึ้นไป คลิกขวา > เปิด อาจใช้ไม่ได้แล้ว) ให้ทำทางใดทางหนึ่ง:"
echo "  ก) เปิด การตั้งค่าระบบ (System Settings) > ความเป็นส่วนตัวและความปลอดภัย (Privacy & Security) เลื่อนลงล่าง"
echo "     กด \"เปิดต่อไป\" (Open Anyway) ที่ขึ้นข้างชื่อไฟล์ แล้วยืนยันอีกครั้ง (อาจถามรหัสผ่านเครื่อง)"
echo "  ข) เปิด Terminal พิมพ์ bash (มีเว้นวรรคท้าย) แล้วลากไฟล์ ติดตั้ง.command มาวางที่หน้าต่าง กด Enter (ทางนี้ไม่ต้องผ่านหน้าต่างตรวจสอบของ macOS)"
