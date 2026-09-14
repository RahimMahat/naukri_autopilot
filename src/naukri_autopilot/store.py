"""SQLite persistence. Plain sqlite3 - an ORM would be dead weight at this size.

Timestamps are stored as ISO-8601 UTC strings and returned as aware datetimes.
The user's machine changes timezone (travel, DST) and the schedule must not
jump when it does; only quiet hours are evaluated in local time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .results import RunResult, Status
from .scheduler import SchedState, Settings

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,
  trigger       TEXT NOT NULL,
  headline_used TEXT,
  profile_ts    TEXT,
  error_kind    TEXT,
  error_detail  TEXT,
  screenshot    TEXT,
  already_fresh INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS runs_status  ON runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DEFAULTS = {
    "interval_hours": "24",
    "resume_path": "",
    "quiet_start": "23",
    "quiet_end": "7",
    "headed_mode": "0",
    "screenshot_retention": "60",
    "schema_version": str(SCHEMA_VERSION),
}


def connect(path: "Path | None" = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # survives an abrupt power-off
    conn.execute("PRAGMA foreign_keys=ON")
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for key, value in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


# -- settings -------------------------------------------------------------- #


def get(conn, key: str, default: "str | None" = None) -> "str | None":
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    return row["value"]


def get_int(conn, key: str, default: int = 0) -> int:
    try:
        return int(get(conn, key) or default)
    except (TypeError, ValueError):
        return default


def put(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def settings(conn) -> Settings:
    quiet_start = get(conn, "quiet_start")
    quiet_end = get(conn, "quiet_end")
    return Settings(
        interval_hours=get_int(conn, "interval_hours", 24),
        quiet_start=int(quiet_start) if quiet_start not in (None, "") else None,
        quiet_end=int(quiet_end) if quiet_end not in (None, "") else None,
    )


# -- runs ------------------------------------------------------------------ #


def _parse(value: "str | None") -> "datetime | None":
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record(conn, result: RunResult) -> int:
    cur = conn.execute(
        "INSERT INTO runs(started_at, finished_at, status, trigger, headline_used,"
        " profile_ts, error_kind, error_detail, screenshot, already_fresh)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            result.started_at,
            result.finished_at,
            result.status,
            result.trigger,
            result.headline_used,
            result.profile_ts,
            result.error_kind,
            result.error_detail,
            result.screenshot,
            1 if result.already_fresh else 0,
        ),
    )
    conn.commit()
    return cur.lastrowid


def recent(conn, limit: int = 50) -> "list[sqlite3.Row]":
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


# Statuses that represent an actual attempt against Naukri. SKIPPED_* runs are
# bookkeeping - counting them would poison both the retry ladder and the
# staleness alert.
ATTEMPT_STATUSES = (Status.SUCCESS, Status.FAILED, Status.NEEDS_LOGIN)


def sched_state(conn) -> SchedState:
    """Snapshot for the scheduler. DRY_RUN and SKIPPED_* are excluded."""
    placeholders = ",".join("?" for _ in ATTEMPT_STATUSES)

    success = conn.execute(
        "SELECT started_at FROM runs WHERE status=? ORDER BY started_at DESC LIMIT 1",
        (Status.SUCCESS,),
    ).fetchone()

    attempt = conn.execute(
        "SELECT started_at, status FROM runs WHERE status IN ({})"
        " ORDER BY started_at DESC LIMIT 1".format(placeholders),
        ATTEMPT_STATUSES,
    ).fetchone()

    # Consecutive FAILED runs since the last success. NEEDS_LOGIN deliberately
    # does not count: it waits for a human, and a retry ladder would just burn
    # attempts against a session that cannot recover on its own.
    failures = 0
    for row in conn.execute(
        "SELECT status FROM runs WHERE status IN ({})"
        " ORDER BY started_at DESC".format(placeholders),
        ATTEMPT_STATUSES,
    ):
        if row["status"] == Status.FAILED:
            failures += 1
        else:
            break

    return SchedState(
        last_success_at=_parse(success["started_at"]) if success else None,
        last_attempt_at=_parse(attempt["started_at"]) if attempt else None,
        last_status=attempt["status"] if attempt else None,
        consecutive_failures=failures,
    )


def prune_screenshots(conn, keep: "int | None" = None) -> int:
    """Delete screenshot files for runs beyond the retention count."""
    keep = keep if keep is not None else get_int(conn, "screenshot_retention", 60)
    rows = conn.execute(
        "SELECT screenshot FROM runs WHERE screenshot IS NOT NULL"
        " ORDER BY started_at DESC LIMIT -1 OFFSET ?",
        (keep,),
    ).fetchall()
    removed = 0
    for row in rows:
        path = config.PROJECT_ROOT / row["screenshot"]
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
