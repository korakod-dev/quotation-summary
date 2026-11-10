#!/bin/bash
# Double-click to remove EVERYTHING this program installed (schedule, Keychain items, cache, logs, program folder),
# with a summary and a typed confirmation before anything is deleted. Optional extras are asked separately (default: keep).
# Thin launcher for scripts/uninstall-all.command (started with /bin/bash so a lost executable bit does not matter).
cd "$(dirname "$0")" || exit 1
exec /bin/bash "$(pwd)/scripts/uninstall-all.command" "$@"
