#!/bin/bash
# Uninstall: stop and remove the weekly launchd job. Does NOT delete your PDFs, the Excel file,
# the cache, config.json, .venv, or the Keychain items / Claude Code config folder (commands to remove those are printed).
set -u

LABEL="com.korakod.quotation-summary"
AGENTS_DIR="${QS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"

# Keychain service name is configurable in config.json; ask the program, fall back to the default.
SERVICE="$("$(dirname "$0")/run.sh" --print-keychain-service 2>/dev/null | tail -n 1)"
SERVICE="${SERVICE:-quotation-summary-anthropic}"
# Same for the Claude Code (subscription) token and its private config folder.
CLAUDE_SERVICE="$("$(dirname "$0")/run.sh" --print-claude-token-service 2>/dev/null | tail -n 1)"
CLAUDE_SERVICE="${CLAUDE_SERVICE:-quotation-summary-claude-code}"
CLAUDE_DIR="$("$(dirname "$0")/run.sh" --print-claude-config-dir 2>/dev/null | tail -n 1)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/Library/Application Support/QuotationSummary/claude-code}"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "หยุดงาน launchd แล้ว" || echo "ไม่มีงาน launchd ที่กำลังทำงานอยู่ (ข้าม)"
if [ -f "$PLIST" ]; then
  rm -f "$PLIST" && echo "ลบ $PLIST แล้ว"
else
  echo "ไม่พบ $PLIST (ข้าม)"
fi

echo
echo "สิ่งที่ยังเหลืออยู่ (ลบเองถ้าต้องการ):"
echo "  - cache:   rm -r \"$HOME/Library/Application Support/QuotationSummary\""
echo "  - log:     rm -r \"$HOME/Library/Logs/QuotationSummary\""
echo "  - API key: security delete-generic-password -s \"$SERVICE\""
echo "  - token Claude (แพ็กเกจรายเดือน): security delete-generic-password -s \"$CLAUDE_SERVICE\""
echo "  - โฟลเดอร์ค่าของ Claude Code: rm -r \"$CLAUDE_DIR\""
echo "  - โฟลเดอร์โปรแกรม (รวม .venv และ config.json)"
