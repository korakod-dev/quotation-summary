#!/bin/bash
# Entry point used by both launchd (weekly) and run-now.command.
# Runs the summary with the project's own virtualenv. Safe to run by hand any time.
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# launchd starts jobs with a minimal PATH; add Homebrew so pdftotext (poppler) is found, and ~/.local/bin
# where the native Claude Code installer puts `claude` (docs: https://code.claude.com/docs/en/setup).
# An npm install of Claude Code needs `node` on this PATH too (e.g. nvm's is not): use the native installer.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# Make Python print Thai correctly even when launched without a locale.
export PYTHONIOENCODING="utf-8"
export LANG="${LANG:-en_US.UTF-8}"

PY="$APP_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ยังไม่ได้ติดตั้ง: ไม่พบ $PY (รัน scripts/install.sh ก่อน)" >&2
  exit 1
fi

cd "$APP_DIR" || exit 1
"$PY" -m qsum "$@"
RC=$?

# Keep the launchd log small: trim to the last 2000 lines once it passes ~1 MB. Done AFTER the run:
# launchd already holds the log file open, so a mid-run `mv` would send this run's output to the
# deleted old file. By now this run's output is complete and nothing more is written.
LOG="$HOME/Library/Logs/QuotationSummary/run.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && cat "$LOG.tmp" > "$LOG"   # rewrite in place (same file)
  rm -f "$LOG.tmp"
fi
exit "$RC"
