#!/bin/bash
# Double-click to check that everything works (Thai pass/fail checklist). Changes nothing.
# Thin launcher for scripts/doctor.command (started with /bin/bash so a lost executable bit does not matter).
cd "$(dirname "$0")" || exit 1
exec /bin/bash "$(pwd)/scripts/doctor.command" "$@"
