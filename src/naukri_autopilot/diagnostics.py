"""Health checks behind `doctor` and the setup checklist.

Each check answers one question and, when it fails, says exactly what to type.
A diagnostic that reports "something is wrong" without a next step is just a
slower way of saying nothing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, scheduling, store
from .results import Status

OK = "ok"
WARN = "warn"
FAIL = "fail"

ICON = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    fix: str = ""

    def render(self) -> str:
        line = "{} {:<22} {}".format(ICON[self.state], self.name, self.detail)
        if self.fix and self.state != OK:
            line += "\n         -> {}".format(self.fix)
        return line


def check_python() -> Check:
    v = sys.version_info
    text = "{}.{}.{}".format(v.major, v.minor, v.micro)
    if (v.major, v.minor) < (3, 9):
        return Check("python", FAIL, text, "Python 3.9 or newer is required")
    return Check("python", OK, text)


def check_playwright() -> Check:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return Check("playwright", FAIL, "not installed",
                     "pip install -e .")
    return Check("playwright", OK, "installed")


def check_browser() -> Check:
    brave = config.find_brave()
    if brave is not None:
        return Check("browser", OK, "brave at {}".format(brave))
    # Bundled Chromium works for everything except Google OAuth sign-in.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        if Path(path).is_file():
            return Check(
                "browser", WARN, "bundled chromium only",
                "Google sign-in will be refused. Install Brave, Chrome or Edge "
                "if your Naukri login uses Continue with Google.",
            )
    except Exception:
        pass
    return Check("browser", FAIL, "none found",
                 "playwright install chromium, or install Brave/Chrome/Edge")


def check_session() -> Check:
    path = config.STORAGE_STATE
    if not path.is_file():
        return Check("session", FAIL, "not signed in",
                     "naukri-autopilot login")
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return Check("session", OK, "saved {} days ago".format(age.days))


def check_resume(conn) -> Check:
    raw = store.get(conn, "resume_path") or ""
    if not raw:
        return Check("resume", FAIL, "not configured",
                     'naukri-autopilot config resume_path "C:/path/to/cv.pdf"')
    path = Path(raw).expanduser()
    if not path.is_file():
        return Check("resume", FAIL, "missing: {}".format(path),
                     "point resume_path at a file that exists")
    if path.suffix.lower() != ".pdf":
        return Check("resume", WARN, "not a .pdf: {}".format(path.name))
    size_kb = path.stat().st_size // 1024
    detail = "{} ({} KB)".format(path.name, size_kb)
    # A resume inside Downloads is one browser cleanup away from breaking the
    # schedule, and the failure would be silent until someone reads the history.
    if "downloads" in str(path).lower():
        return Check("resume", WARN, detail,
                     "it is in Downloads - move it somewhere permanent")
    return Check("resume", OK, detail)


def check_schedule() -> Check:
    info = scheduling.query()
    if not info.registered:
        return Check("scheduled task", FAIL, "not registered",
                     "naukri-autopilot install-task")
    detail = "every {} min".format(scheduling.TICK_MINUTES)
    if info.next_run:
        detail += ", next {}".format(info.next_run)
    return Check("scheduled task", OK, detail)


def check_history(conn) -> Check:
    state = store.sched_state(conn)
    settings = store.settings(conn)

    if state.last_success_at is None:
        return Check("runs", WARN, "no successful run yet",
                     "naukri-autopilot run  (performs a real upload)")

    age = datetime.now(timezone.utc) - state.last_success_at
    stale_after = timedelta(hours=settings.interval_hours * 2)
    detail = "last success {}h ago".format(int(age.total_seconds() // 3600))

    if state.last_status == Status.NEEDS_LOGIN:
        return Check("runs", FAIL, detail + ", session expired",
                     "naukri-autopilot login")
    if age > stale_after:
        return Check("runs", FAIL, detail + " (stale)",
                     "check `naukri-autopilot status` for the last error")
    if state.consecutive_failures:
        return Check("runs", WARN,
                     detail + ", {} consecutive failures".format(state.consecutive_failures))
    return Check("runs", OK, detail)


def run_all() -> "list[Check]":
    conn = store.connect()
    try:
        return [
            check_python(),
            check_playwright(),
            check_browser(),
            check_session(),
            check_resume(conn),
            check_schedule(),
            check_history(conn),
        ]
    finally:
        conn.close()


def worst(checks: "list[Check]") -> str:
    for level in (FAIL, WARN, OK):
        if any(c.state == level for c in checks):
            return level
    return OK
