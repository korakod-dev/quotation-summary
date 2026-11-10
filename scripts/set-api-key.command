#!/bin/bash
# Double-click to store the Anthropic API key in the macOS Keychain (never in a file).
# The key is typed into a hidden prompt; it is not shown, logged, or put on the command line.
cd "$(dirname "$0")" || exit 1

SERVICE="$(./run.sh --print-keychain-service 2>/dev/null | tail -n 1)"
SERVICE="${SERVICE:-quotation-summary-anthropic}"

echo "จะเก็บ API key ไว้ใน Keychain (ชื่อรายการ: $SERVICE)"
echo "วาง key (sk-ant-...) แล้วกด Enter — ตัวอักษรจะไม่แสดงบนจอ และต้องพิมพ์ยืนยันอีกครั้ง"
# -w as the LAST option makes `security` prompt for the password; -U updates an existing item.
if security add-generic-password -a "$USER" -s "$SERVICE" -U -w; then
  echo "บันทึกแล้ว"
else
  echo "บันทึกไม่สำเร็จ" >&2
fi
echo "กด Enter เพื่อปิดหน้าต่าง"
read -r _
