#!/bin/bash
# Assemble "AI Group Whisper.app" from this source folder. Usage: make-app.sh <output-folder>
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$SRC/release}"
APP="$OUT/AI Group Whisper.app"
VER="$(cat "$SRC/VERSION")"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
cp -R "$SRC/daemon" "$SRC/renderer" "$SRC/run.py" "$SRC/VERSION" "$APP/Contents/Resources/app/"
cp "$SRC/assets/logo.icns" "$APP/Contents/Resources/AIGroupWhisper.icns"
rm -rf "$APP/Contents/Resources/app/daemon/.venv"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
cp "$SRC/scripts/launcher.sh" "$APP/Contents/MacOS/AI Group Whisper"
chmod +x "$APP/Contents/MacOS/AI Group Whisper"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>AI Group Whisper</string>
  <key>CFBundleDisplayName</key><string>AI Group Whisper</string>
  <key>CFBundleIdentifier</key><string>com.alpharomeo99.aigroupwhisper</string>
  <key>CFBundleExecutable</key><string>AI Group Whisper</string>
  <key>CFBundleIconFile</key><string>AIGroupWhisper.icns</string>
  <key>CFBundleIconName</key><string>AIGroupWhisper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VER</string>
  <key>CFBundleVersion</key><string>$VER</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
touch "$APP"  # make Finder and the Dock pick up the icon right away
echo "Built: $APP"
