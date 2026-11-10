#!/bin/bash
# Double-click to install everything (folder move, Python packages, weekly schedule, Claude Code, token, first test).
# Thin launcher: the real work is in scripts/setup-all.command. It is started with /bin/bash explicitly, so it
# also works if the executable bit of that file was lost while unzipping.
cd "$(dirname "$0")" || exit 1
exec /bin/bash "$(pwd)/scripts/setup-all.command" "$@"
