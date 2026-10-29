#!/bin/bash
# One-paste installer: builds AI Group Whisper.app straight into /Applications (no Gatekeeper prompts).
set -euo pipefail
REPO="alpharomeo99/ai-group-whisper-mac"

echo "Closing any running instances of AI Group Whisper..."
pkill -9 -f "AI Group Whisper" 2>/dev/null || true
pkill -9 -f "whisperd.py" 2>/dev/null || true
pkill -9 -f "run.py" 2>/dev/null || true
sleep 1

TMP="$(mktemp -d)"
TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)"
TAG="${TAG:-main}"
echo "Installing AI Group Whisper ${TAG}..."
if [ "${TAG}" = "main" ]; then URL="https://codeload.github.com/$REPO/zip/refs/heads/main"; else URL="https://codeload.github.com/$REPO/zip/refs/tags/${TAG}"; fi
curl -fsSL "$URL" -o "$TMP/src.zip"
cd "$TMP" && unzip -q src.zip && SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -n1)"
DEST="/Applications"; [ -w "$DEST" ] || DEST="$HOME/Applications"; mkdir -p "$DEST"
bash "$SRC/scripts/make-app.sh" "$DEST"
rm -rf "$TMP"
echo "Done. Opening AI Group Whisper..."
open -n "$DEST/AI Group Whisper.app"
