#!/bin/bash
# Double-click to run the summary right now (same job the weekly schedule runs), then open the Excel.
cd "$(dirname "$0")" || exit 1

./run.sh "$@"
STATUS=$?

OUT="$(./run.sh --print-output-path 2>/dev/null | tail -n 1)"
if [ -n "$OUT" ] && [ -f "$OUT" ]; then
  # Prefer Excel; fall back to the default app if Excel is not installed.
  open -a "Microsoft Excel" "$OUT" 2>/dev/null || open "$OUT"
fi

echo
echo "เสร็จแล้ว (รหัสออก $STATUS). กด Enter เพื่อปิดหน้าต่าง"
read -r _
