"""When should a run happen?

Deliberately pure: no database, no browser, no wall clock. Everything is
computed from an injected `now` and a `SchedState` snapshot, which is what makes
the awkward cases (catch-up, quiet hours, retry ladders, DST) testable at all.

Task Scheduler fires a dumb heartbeat every 15 minutes and this module decides
whether that tick becomes a run. See README section 4.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .results import Trigger

# Retry ladder after a FAILED run: attempt 2 comes 30 min later, attempt 3 two
# hours after that. Beyond this the schedule falls back to a normal interval so
# a persistently broken setup is not hammering Naukri every half hour.
RETRY_BACKOFF = [timedelta(minutes=30), timedelta(hours=2)]
MAX_ATTEMPTS = len(RETRY_BACKOFF) + 1

MAX_JITTER_MINUTES = 45

VALID_INTERVALS = (12, 24, 48)


@dataclass
class SchedState:
    """Everything the decision depends on. Read from the store, never by this module."""

    last_success_at: "datetime | None" = None
    last_attempt_at: "datetime | None" = None
    last_status: "str | None" = None
    consecutive_failures: int = 0


@dataclass
class Settings:
    interval_hours: int = 24
    quiet_start: "int | None" = 23  # local hour, inclusive
    quiet_end: "int | None" = 7  # local hour, exclusive
    jitter: bool = True


@dataclass
class Decision:
    run: bool
    reason: str
    trigger: "str | None" = None
    next_due_at: "datetime | None" = None

    def describe(self, now: datetime) -> str:
        if self.run:
            return "RUN ({}) - {}".format(self.trigger, self.reason)
        if self.next_due_at is None:
            return "HOLD - {}".format(self.reason)
        delta = self.next_due_at - now
        return "HOLD - {} (next in {})".format(self.reason, _humanize(delta))


def _humanize(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 0:
        return "now"
    if secs < 3600:
        return "{}m".format(secs // 60)
    if secs < 86400:
        return "{}h{:02d}m".format(secs // 3600, (secs % 3600) // 60)
    return "{}d{}h".format(secs // 86400, (secs % 86400) // 3600)


# --------------------------------------------------------------------------- #
# jitter
# --------------------------------------------------------------------------- #


def jitter_for(anchor: datetime, max_minutes: int = MAX_JITTER_MINUTES) -> timedelta:
    """Deterministic offset derived from the anchor timestamp.

    Must be stable for a given cycle: a fresh random number on every 15-minute
    tick would make the due time wander, so a run could be perpetually 10
    minutes away. Hashing the anchor gives a value that is fixed for the cycle
    but unpredictable across cycles.
    """
    digest = hashlib.sha256(anchor.isoformat().encode("utf-8")).digest()
    seconds = int.from_bytes(digest[:4], "big") % (max_minutes * 60)
    return timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# quiet hours
# --------------------------------------------------------------------------- #


def in_quiet_hours(local_dt: datetime, start: "int | None", end: "int | None") -> bool:
    """Handles the wrap-around case (23:00-07:00) as well as 01:00-06:00."""
    if start is None or end is None or start == end:
        return False
    hour = local_dt.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def next_quiet_end(local_dt: datetime, end: int) -> datetime:
    """First instant at or after local_dt that is outside the quiet window."""
    candidate = local_dt.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= local_dt:
        candidate += timedelta(days=1)
    return candidate


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #


def next_due(state: SchedState, settings: Settings) -> "datetime | None":
    """When the next run is wanted, before quiet hours are applied."""
    interval = timedelta(hours=settings.interval_hours)

    # Never run successfully: go as soon as possible.
    if state.last_success_at is None and state.last_attempt_at is None:
        return None  # None means "immediately"

    # Inside the retry ladder.
    if (
        state.consecutive_failures > 0
        and state.consecutive_failures < MAX_ATTEMPTS
        and state.last_attempt_at is not None
    ):
        backoff = RETRY_BACKOFF[state.consecutive_failures - 1]
        return state.last_attempt_at + backoff

    # Retries exhausted: stop hammering, wait a full interval from the last try.
    if state.consecutive_failures >= MAX_ATTEMPTS and state.last_attempt_at is not None:
        anchor = state.last_attempt_at
        return anchor + interval + (jitter_for(anchor) if settings.jitter else timedelta())

    if state.last_success_at is None:
        return None

    anchor = state.last_success_at
    return anchor + interval + (jitter_for(anchor) if settings.jitter else timedelta())


def decide(now: datetime, state: SchedState, settings: Settings) -> Decision:
    """Should this tick become a run?"""
    if settings.interval_hours not in VALID_INTERVALS:
        return Decision(False, "invalid interval: {}".format(settings.interval_hours))

    due = next_due(state, settings)

    if due is not None and now < due:
        return Decision(False, "not due", next_due_at=due)

    # Quiet hours are about not updating a profile at 3am; they defer, never skip.
    local_now = now.astimezone()
    if in_quiet_hours(local_now, settings.quiet_start, settings.quiet_end):
        resume_at = next_quiet_end(local_now, settings.quiet_end)
        return Decision(False, "quiet hours", next_due_at=resume_at.astimezone(timezone.utc))

    if due is None:
        # Whoever asked owns the label; a tick firing the first-ever run is still
        # a scheduled run, not a manual one.
        return Decision(True, "no successful run yet", trigger=Trigger.SCHEDULE)

    if state.consecutive_failures > 0 and state.consecutive_failures < MAX_ATTEMPTS:
        return Decision(
            True,
            "retry {} of {}".format(state.consecutive_failures + 1, MAX_ATTEMPTS),
            trigger=Trigger.RETRY,
        )

    # Catch up, never backfill. Being three cycles late still means one run:
    # only the latest timestamp counts, and three updates in a row is a
    # signature, not a benefit.
    interval = timedelta(hours=settings.interval_hours)
    if state.last_success_at is not None and now - state.last_success_at > interval * 2:
        return Decision(
            True,
            "overdue by {}".format(_humanize(now - state.last_success_at)),
            trigger=Trigger.CATCHUP,
        )

    return Decision(True, "due", trigger=Trigger.SCHEDULE)
