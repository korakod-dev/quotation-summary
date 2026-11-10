#!/bin/bash
# Read-only status as JSON for the desktop app (SwiftUI): prints ONE JSON document, exit 0 = ok, 1 = problem.
# Same PATH / locale set-up as run.sh. Does not start a run, does not call Claude, writes nothing.
# Extra arguments are passed on (e.g. --config FILE).
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONIOENCODING="utf-8"
export LANG="${LANG:-en_US.UTF-8}"

PY="$APP_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  # No virtualenv = the installer has not run. Same JSON shape the app already understands (only the
  # guaranteed keys for not_installed).
  NOW="$(date +%Y-%m-%dT%H:%M:%S%z | sed 's/\(..\)$/:\1/')"
  printf '{"schema":1,"ok":false,"error_code":"not_installed","error":"ยังไม่ได้ติดตั้ง: ไม่พบสภาพแวดล้อม Python (.venv) ของโปรแกรม","generated_at":"%s"}\n' \
    "$NOW"
  exit 1
fi

cd "$APP_DIR" || exit 1
exec "$PY" -m qsum.status --json "$@"
