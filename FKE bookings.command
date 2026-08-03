#!/bin/bash
# Double-click this in Finder to open your bookings in a browser.
#
# The first run sets up a private Python environment inside this folder (.venv)
# and asks for your portal login, which takes a minute. After that it starts
# straight up. Closing the Terminal window that appears stops the app.

set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
stop() { say ""; read -r -p "Press return to close."; exit 1; }

# --- Python -----------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  say "This needs Python, which your Mac doesn't have yet."
  say "Open Terminal, run:  xcode-select --install"
  say "then try this again once it has finished installing."
  stop
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  say "Your Python is $(python3 -V 2>&1), which is too old — 3.9 or newer is needed."
  say "Installing from python.org, or 'brew install python3', will sort it."
  stop
fi

# --- one-off setup ----------------------------------------------------------

if [ ! -x .venv/bin/python ]; then
  say "First run — setting things up, this takes a minute…"
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  say "Done."
  say ""
fi

if [ ! -f .env ]; then
  say "Your Fun Kids Entertainers login is needed, just this once."
  say "It is saved in a file called .env in this folder, readable only by you,"
  say "and is never sent anywhere except the portal itself."
  say ""
  read -r -p "Portal email: " fke_user
  read -r -s -p "Portal password: " fke_pass
  say ""
  if [ -z "$fke_user" ] || [ -z "$fke_pass" ]; then
    say ""
    say "Both are needed — nothing saved. Try again."
    stop
  fi
  ( umask 077; printf 'FKE_USER=%s\nFKE_PASS=%s\n' "$fke_user" "$fke_pass" > .env )
  chmod 600 .env
  unset fke_pass
  say ""
  say "Saved. To change it later, delete .env and run this again."
  say ""
fi

exec .venv/bin/python fke-pull --serve "$@"
