"""Scheduler decisions against a fake clock. No DB, no browser, no sleeping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from naukri_autopilot import scheduler as sch
from naukri_autopilot.results import Trigger

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def settings(**kw):
    base = dict(interval_hours=24, quiet_start=None, quiet_end=None, jitter=False)
    base.update(kw)
    return sch.Settings(**base)


def state(**kw):
    return sch.SchedState(**kw)


# -- first run -------------------------------------------------------------- #


def test_runs_when_nothing_has_ever_happened():
    d = sch.decide(T0, state(), settings())
    assert d.run


# -- ordinary cadence ------------------------------------------------------- #


def test_holds_before_interval_elapses():
    d = sch.decide(T0, state(last_success_at=T0 - timedelta(hours=5)), settings())
    assert not d.run
    assert d.reason == "not due"


def test_runs_once_interval_elapses():
    d = sch.decide(T0, state(last_success_at=T0 - timedelta(hours=24)), settings())
    assert d.run and d.trigger == Trigger.SCHEDULE


@pytest.mark.parametrize("hours", sch.VALID_INTERVALS)
def test_each_supported_interval(hours):
    s = settings(interval_hours=hours)
    just_before = state(last_success_at=T0 - timedelta(hours=hours, minutes=-1))
    just_after = state(last_success_at=T0 - timedelta(hours=hours, minutes=1))
    assert not sch.decide(T0, just_before, s).run
    assert sch.decide(T0, just_after, s).run


def test_rejects_unsupported_interval():
    assert not sch.decide(T0, state(), settings(interval_hours=6)).run


# -- catch-up, not backfill ------------------------------------------------- #


def test_long_outage_produces_exactly_one_catchup_run():
    """Laptop off for five days is one run, not five.

    Only the latest timestamp counts, so backfilling achieves nothing and looks
    like a machine.
    """
    d = sch.decide(T0, state(last_success_at=T0 - timedelta(days=5)), settings())
    assert d.run and d.trigger == Trigger.CATCHUP


def test_slightly_late_is_normal_not_catchup():
    d = sch.decide(T0, state(last_success_at=T0 - timedelta(hours=25)), settings())
    assert d.run and d.trigger == Trigger.SCHEDULE


# -- quiet hours ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "hour,quiet",
    [(23, True), (2, True), (6, True), (7, False), (12, False), (22, False)],
)
def test_quiet_window_wraps_midnight(hour, quiet):
    local = datetime(2026, 9, 14, hour, 0)
    assert sch.in_quiet_hours(local, 23, 7) is quiet


@pytest.mark.parametrize(
    "hour,quiet", [(0, False), (1, True), (5, True), (6, False)]
)
def test_quiet_window_without_wrap(hour, quiet):
    assert sch.in_quiet_hours(datetime(2026, 9, 14, hour, 0), 1, 6) is quiet


def test_no_quiet_hours_when_unset():
    assert not sch.in_quiet_hours(datetime(2026, 9, 14, 3, 0), None, None)
    assert not sch.in_quiet_hours(datetime(2026, 9, 14, 3, 0), 5, 5)


def test_quiet_hours_defer_rather_than_skip():
    """A deferred run must come back, not be silently dropped."""
    local_3am = datetime(2026, 9, 15, 3, 0).astimezone()
    d = sch.decide(
        local_3am.astimezone(timezone.utc),
        state(last_success_at=local_3am.astimezone(timezone.utc) - timedelta(days=2)),
        settings(quiet_start=23, quiet_end=7),
    )
    assert not d.run
    assert d.reason == "quiet hours"
    assert d.next_due_at is not None


# -- retry ladder ----------------------------------------------------------- #


def test_first_retry_waits_thirty_minutes():
    st = state(
        last_success_at=T0 - timedelta(days=2),
        last_attempt_at=T0 - timedelta(minutes=10),
        consecutive_failures=1,
    )
    assert not sch.decide(T0, st, settings()).run
    st.last_attempt_at = T0 - timedelta(minutes=31)
    d = sch.decide(T0, st, settings())
    assert d.run and d.trigger == Trigger.RETRY


def test_second_retry_waits_two_hours():
    st = state(
        last_success_at=T0 - timedelta(days=2),
        last_attempt_at=T0 - timedelta(hours=1),
        consecutive_failures=2,
    )
    assert not sch.decide(T0, st, settings()).run
    st.last_attempt_at = T0 - timedelta(hours=2, minutes=1)
    assert sch.decide(T0, st, settings()).run


def test_exhausted_retries_stop_hammering():
    """After MAX_ATTEMPTS the ladder is over: wait a full interval.

    Without this the natural due time is already long past (the last success is
    old), so every 15-minute tick would fire another doomed attempt.
    """
    st = state(
        last_success_at=T0 - timedelta(days=5),
        last_attempt_at=T0 - timedelta(minutes=5),
        consecutive_failures=sch.MAX_ATTEMPTS,
    )
    d = sch.decide(T0, st, settings())
    assert not d.run
    assert d.next_due_at >= T0 + timedelta(hours=23)


def test_needs_login_does_not_consume_retries():
    """NEEDS_LOGIN waits for a human; a ladder would burn attempts for nothing.

    The store models this by not counting NEEDS_LOGIN as a failure, so the
    scheduler sees an ordinary cadence and recovers by itself once the user
    signs in again.
    """
    st = state(
        last_success_at=T0 - timedelta(hours=30),
        last_attempt_at=T0 - timedelta(minutes=5),
        last_status="NEEDS_LOGIN",
        consecutive_failures=0,
    )
    d = sch.decide(T0, st, settings())
    assert d.run and d.trigger == Trigger.SCHEDULE


# -- jitter ----------------------------------------------------------------- #


def test_jitter_is_stable_for_a_given_cycle():
    """Recomputed every tick, so it must not wander.

    A fresh random offset each time would leave the run perpetually a few
    minutes away and it would never fire.
    """
    anchor = T0
    assert sch.jitter_for(anchor) == sch.jitter_for(anchor)


def test_jitter_differs_across_cycles():
    a = sch.jitter_for(T0)
    b = sch.jitter_for(T0 + timedelta(hours=24))
    assert a != b


def test_jitter_within_bounds():
    for i in range(200):
        j = sch.jitter_for(T0 + timedelta(minutes=i))
        assert timedelta(0) <= j < timedelta(minutes=sch.MAX_JITTER_MINUTES)


def test_jitter_delays_but_does_not_prevent():
    s_no = settings(jitter=False)
    s_yes = settings(jitter=True)
    st = state(last_success_at=T0 - timedelta(hours=24))
    assert sch.decide(T0, st, s_no).run
    # With jitter the same moment may hold, but never beyond the jitter ceiling.
    later = T0 + timedelta(minutes=sch.MAX_JITTER_MINUTES)
    assert sch.decide(later, st, s_yes).run


def test_first_ever_run_is_labelled_scheduled_not_manual():
    """A tick firing the first run is still a scheduled run.

    Mislabelling it 'manual' would make the dashboard history claim the user
    pressed a button they never pressed.
    """
    d = sch.decide(T0, state(), settings())
    assert d.run and d.trigger == Trigger.SCHEDULE
