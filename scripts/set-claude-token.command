#!/bin/bash
# Double-click to store the Claude Code token (from `claude setup-token`) in the macOS Keychain (never in a file).
# The token is typed into a hidden prompt; it is not shown, logged, or put on the command line.
cd "$(dirname "$0")" || exit 1

SERVICE="$(./run.sh --print-claude-token-service 2>/dev/null | tail -n 1)"
SERVICE="${SERVICE:-quotation-summary-claude-code}"

echo "จะเก็บ token ของ Claude ไว้ใน Keychain (ชื่อรายการ: $SERVICE)"
echo "ถ้ายังไม่มี token: เปิด Terminal รัน 'claude setup-token' (ล็อกอินด้วยบัญชี Claude ของคุณ) แล้วคัดลอก token ที่แสดง"
echo "วาง token (sk-ant-oat01-...) แล้วกด Enter — ตัวอักษรจะไม่แสดงบนจอ และต้องพิมพ์ยืนยันอีกครั้ง"
# -w as the LAST option makes `security` prompt for the password; -U updates an existing item.
if security add-generic-password -a "$USER" -s "$SERVICE" -U -w; then
  echo "บันทึกแล้ว (Keychain อาจถามสิทธิ์ครั้งแรกที่โปรแกรมอ่าน token ให้กด Always Allow)"
else
  echo "บันทึกไม่สำเร็จ" >&2
fi
echo "กด Enter เพื่อปิดหน้าต่าง"
read -r _
