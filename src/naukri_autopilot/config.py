"""Paths, URLs and constants shared by every entry point.

Session state deliberately lives OUTSIDE the project folder (see README, Security model):
it is password-equivalent, and project folders get zipped, synced and shared.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "NaukriAutopilot"

BASE_URL = "https://www.naukri.com"
LOGIN_URL = BASE_URL + "/nlogin/login"
PROFILE_URL = BASE_URL + "/mnjuser/profile"

# Chromium reports its own real user agent; we never spoof one. Viewport is a
# common laptop size rather than Playwright's unusual 1280x720 default.
VIEWPORT = {"width": 1440, "height": 900}

# Moves the window far off-screen instead of running headless, so scheduled runs
# are invisible without carrying a headless fingerprint (README, Anti-detection posture).
OFFSCREEN_ARGS = ["--window-position=-32000,-32000"]

# Drops the `navigator.webdriver` flag that Chromium sets when driven over CDP.
# This is what Google's "this browser or app may not be secure" check looks at
# during sign-in. Not a cloak - it removes one obvious tell, nothing more.
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
]

# Real, signed browsers get treated better than Playwright's bundled Chromium,
# which is what Google's sign-in check objects to. Tried in this order.
CHANNEL_PREFERENCE = ["brave", "chrome", "msedge", "chromium"]


def state_dir() -> Path:
    """Per-user private directory. %LOCALAPPDATA% on Windows, XDG-ish elsewhere."""
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


STATE_DIR = state_dir()
STORAGE_STATE = STATE_DIR / "state.json"  # portable cookie snapshot


def profile_dir(tag: str) -> Path:
    """One browser profile per browser build.

    Chromium builds cannot share a user-data-dir - Edge refuses to open a profile
    Chromium created, with a misleading "already in use by another instance".
    Keying the directory on the build avoids the whole class of problem.
    """
    return STATE_DIR / ("profile-" + tag)


# Brave ships no Playwright "channel", so it is driven by executable path.
BRAVE_PATHS = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "BraveSoftware/Brave-Browser/Application/brave.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "BraveSoftware/Brave-Browser/Application/brave.exe",
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "BraveSoftware/Brave-Browser/Application/brave.exe",
]


def find_brave() -> "Path | None":
    for p in BRAVE_PATHS:
        if p.is_file():
            return p
    return None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "state.db"
LOCK_PATH = DATA_DIR / "run.lock"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DEBUG_DIR = DATA_DIR / "debug"


def ensure_dirs() -> None:
    for d in (STATE_DIR, DATA_DIR, SCREENSHOT_DIR, DEBUG_DIR):
        d.mkdir(parents=True, exist_ok=True)
