#!/bin/bash
# Double-click this in Finder to open your bookings in a browser.
#
# On the first run it sets up a private Python environment inside this folder
# (.venv), which takes a minute. After that it starts straight up. Closing the
# Terminal window that appears stops the app.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "First run — setting things up, this takes a minute…"
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  echo
fi

if [ ! -f .env ]; then
  echo "No .env file found in $(pwd)."
  echo "Copy .env.example to .env and put your portal login in it, then try again."
  echo
  read -r -p "Press return to close."
  exit 1
fi

exec .venv/bin/python fke-pull --serve "$@"
