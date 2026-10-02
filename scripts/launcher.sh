#!/bin/bash
# Mac app entry point. Sets up its own private Python environment on first launch, then opens the window.
RES="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
export AGW_BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"
SUPPORT="$HOME/Library/Application Support/AI Group Whisper"
VENV="$SUPPORT/pyenv"
LOG="$HOME/Library/Logs/AI Group Whisper.log"
mkdir -p "$SUPPORT" "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== launch $(date) ==="

fail() {
  osascript -e "display dialog \"$1\" with title \"AI Group Whisper\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null
  exit 1
}

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  xcode-select --install 2>/dev/null
  fail "AI Group Whisper needs Python 3. A macOS installer window should have opened — finish it, then open the app again."
fi

REQ="$RES/daemon/requirements.txt"
if [ ! -x "$VENV/bin/python3" ]; then
  osascript -e 'display notification "First launch: setting things up (about a minute)…" with title "AI Group Whisper"'
  "$PY" -m venv "$VENV" || fail "Could not create the app's Python environment. See ~/Library/Logs/AI Group Whisper.log"
fi
if ! cmp -s "$REQ" "$VENV/.req"; then
  "$VENV/bin/python3" -m pip install -q --upgrade pip
  "$VENV/bin/python3" -m pip install -q -r "$REQ" || fail "Could not install the app's components. Check your internet connection and open the app again."
  cp "$REQ" "$VENV/.req"
fi

exec "$VENV/bin/python3" "$RES/run.py"
