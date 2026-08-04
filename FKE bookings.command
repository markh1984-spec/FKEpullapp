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

# Look for a real Python before falling back to `python3` on the PATH. On a Mac
# without the Xcode Command Line Tools, /usr/bin/python3 is only a stub: running
# it pops up the developer tools installer rather than doing anything. Checking
# the usual install locations first means someone who installed Python from
# python.org never triggers that dialog.
find_python() {
  local candidate
  for candidate in \
    /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v python3 2>/dev/null || return 1
}

no_python() {
  say "This needs Python, which isn't installed yet."
  say ""
  say "Get it from   https://www.python.org/downloads/"
  say "(the big yellow button), open the file it downloads, click through the"
  say "installer, then double-click this again. It's about 65MB."
  stop
}

PY=$(find_python) || no_python
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  version=$("$PY" -V 2>&1 || true)
  case "$version" in
    Python\ 3.[0-8]*|Python\ 2*)
      say "Your Python is $version, which is too old — 3.9 or newer is needed."
      say "Get a current one from https://www.python.org/downloads/"
      stop
      ;;
    *) no_python ;;
  esac
fi

# --- one-off setup ----------------------------------------------------------

if [ ! -x .venv/bin/python ]; then
  say "First run — setting things up, this takes a minute…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  # lxml has to be compiled if there's no ready-made build for this Python, and
  # a Mac without developer tools has no compiler. The tool works without it.
  if ! .venv/bin/python -m pip install --quiet -r requirements.txt 2>/dev/null; then
    say "  (skipping one optional speed-up that won't install here)"
    .venv/bin/python -m pip install --quiet -r requirements-core.txt
  fi
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
