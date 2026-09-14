#!/usr/bin/env bash
# Naukri Autopilot - one-command setup, for Git Bash / WSL.
#
#     ./setup.sh
#
# PowerShell users: run setup.ps1 instead. Both do exactly the same thing.
# Safe to run again at any time; it repairs a half-finished install.

set -euo pipefail
cd "$(dirname "$0")"

cyan()  { printf '\n\033[36m[%s] %s\033[0m\n' "$1" "$2"; }
ok()    { printf '    \033[32m%s\033[0m\n' "$1"; }
dim()   { printf '\033[90m%s\033[0m\n' "$1"; }
die()   { printf '\n    \033[31m%s\033[0m\n\nSetup stopped. Nothing was broken - fix the above and run it again.\n\n' "$1"; exit 1; }

printf '\nNaukri Autopilot setup\n'
printf 'Everything installs into this folder. Nothing is sent anywhere.\n'

# -- 1. Python --------------------------------------------------------------
cyan 1 "Checking Python"
PY=""
for candidate in "py -3" "python3" "python"; do
    if $candidate --version >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || die "Python not found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH', then run this again."

VERSION="$($PY --version 2>&1 | sed 's/Python //')"
MAJOR="${VERSION%%.*}"
REST="${VERSION#*.}"
MINOR="${REST%%.*}"
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 9 ]; }; then
    die "Python $VERSION is too old. 3.9 or newer is required."
fi
ok "Python $VERSION"

# -- 2. Virtual environment -------------------------------------------------
cyan 2 "Creating the virtual environment (.venv)"
# Git Bash on Windows still gets a Scripts/ layout, not bin/.
if [ -f ".venv/Scripts/python.exe" ]; then
    VPY=".venv/Scripts/python.exe"; ok "Already exists - reusing it"
elif [ -f ".venv/bin/python" ]; then
    VPY=".venv/bin/python"; ok "Already exists - reusing it"
else
    $PY -m venv .venv
    if   [ -f ".venv/Scripts/python.exe" ]; then VPY=".venv/Scripts/python.exe"
    elif [ -f ".venv/bin/python" ];        then VPY=".venv/bin/python"
    else die "Could not create .venv"; fi
    ok "Created"
fi

# -- 3. Dependencies --------------------------------------------------------
cyan 3 "Installing dependencies (this is the slow part)"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e . || die "pip install failed - see the output above."
ok "Installed"

# -- 4. Browser -------------------------------------------------------------
cyan 4 "Downloading the browser Playwright needs (~150 MB, once)"
"$VPY" -m playwright install chromium || die "Browser download failed. Check your connection and run this again."
ok "Ready"

# -- 5. Health --------------------------------------------------------------
cyan 5 "Checking what is left to do"
"$VPY" -m naukri_autopilot.cli doctor || true

# -- next steps -------------------------------------------------------------
if [ -d ".venv/Scripts" ]; then ACTIVATE="source .venv/Scripts/activate"; BIN=".venv/Scripts"
else                            ACTIVATE="source .venv/bin/activate";     BIN=".venv/bin"; fi

dim "--------------------------------------------------------------"
printf 'Setup done. Anything still marked [FAIL] above is a next step,\nnot a broken install.\n\n'
printf 'In any new terminal, run this first:\n\n'
printf '    \033[33m%s\033[0m\n\n' "$ACTIVATE"
printf 'Then the tool is just its name:\n'
printf '    \033[33mnaukri-autopilot login\033[0m       sign in to Naukri (once)\n'
printf '    \033[33mnaukri-autopilot dashboard\033[0m   open the control panel\n'
printf '    \033[33mnaukri-autopilot doctor\033[0m      check what is wrong\n\n'
dim "Without activating, use the full path instead:"
dim "    $BIN/naukri-autopilot dashboard"
dim "--------------------------------------------------------------"
printf '\n'
