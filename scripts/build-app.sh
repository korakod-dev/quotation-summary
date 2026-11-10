#!/bin/bash
# scripts/build-app.sh - build the desktop app and assemble QuotationSummary.app
#
# Usage:
#   scripts/build-app.sh [--out DIR]     DIR defaults to <app folder>/build
#
# Result: DIR/QuotationSummary.app (arm64, ad-hoc signed). The LAST line printed on stdout is the absolute path of
# the bundle; every other message goes to stderr, so a caller can use  BUNDLE="$(scripts/build-app.sh --out X | tail -1)".
# Exit code is non-zero on any failure.
#
# Needs only the Xcode Command Line Tools (swift, codesign, plutil). No Xcode, no third-party tools.
# Do NOT call lipo / xcrun / git here: on a Mac with bare CLT those stubs can pop an install dialog.
# Recipe: Swift Package + hand-made Info.plist + ad-hoc signature.

set -eu

APP_NAME="QuotationSummary"
DISPLAY_NAME="สรุปใบเสนอราคา"
BUNDLE_ID="dev.korakod.QuotationSummary"
MIN_MACOS="13.0"

script_dir=$(cd "$(dirname "$0")" && pwd)
app_root=$(cd "$script_dir/.." && pwd)        # repo root (or the installed copy)
gui_dir="$app_root/gui"
out_dir="$app_root/build"

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      [ $# -ge 2 ] || { echo "build-app.sh: --out needs a folder" >&2; exit 2; }
      out_dir="$2"; shift 2 ;;
    --out=*) out_dir="${1#--out=}"; shift ;;
    *) echo "build-app.sh: unknown option: $1 (only --out DIR)" >&2; exit 2 ;;
  esac
done

[ -f "$gui_dir/Package.swift" ] || { echo "build-app.sh: $gui_dir/Package.swift not found" >&2; exit 1; }
mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)               # absolute, so the last line is always absolute
bundle="$out_dir/$APP_NAME.app"

# Version = the engine version, so the About/version of the app and the engine agree.
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$app_root/qsum/__init__.py" 2>/dev/null | head -1)
[ -n "$version" ] || version="1.0.0"

echo "==> swift build (release, arm64)" >&2
swift build --package-path "$gui_dir" -c release --arch arm64 >&2
bin_path=$(swift build --package-path "$gui_dir" -c release --arch arm64 --show-bin-path)
[ -x "$bin_path/$APP_NAME" ] || { echo "build-app.sh: binary not found: $bin_path/$APP_NAME" >&2; exit 1; }

echo "==> assemble $bundle" >&2
rm -rf "$bundle"
mkdir -p "$bundle/Contents/MacOS"
cp "$bin_path/$APP_NAME" "$bundle/Contents/MacOS/$APP_NAME"

# Info.plist (a normal Dock app: no LSUIElement). Thai display name; the file name on disk stays ASCII.
cat > "$bundle/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>               <string>$DISPLAY_NAME</string>
  <key>CFBundleDisplayName</key>        <string>$DISPLAY_NAME</string>
  <key>CFBundleIdentifier</key>         <string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key>         <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>        <string>APPL</string>
  <key>CFBundleShortVersionString</key> <string>$version</string>
  <key>CFBundleVersion</key>            <string>$version</string>
  <key>LSMinimumSystemVersion</key>     <string>$MIN_MACOS</string>
  <key>NSHighResolutionCapable</key>    <true/>
</dict>
</plist>
PLIST
plutil -lint "$bundle/Contents/Info.plist" >/dev/null

echo "==> ad-hoc sign" >&2
codesign --force --sign - --identifier "$BUNDLE_ID" "$bundle" >&2
codesign --verify --strict "$bundle" >&2

echo "$bundle"
