#!/usr/bin/env bash
# Creates the bundled Python environment for the Telethon daemon.
set -euo pipefail
cd "$(dirname "$0")/../daemon"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Python daemon environment ready at daemon/.venv"
