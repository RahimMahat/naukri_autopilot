"""Store behaviour, against a temporary database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from naukri_autopilot import lock, store
from naukri_autopilot.results import RunResult, Status, Trigger

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


def add(conn, status, when, trigger=Trigger.SCHEDULE, **kw):
    r = RunResult(status=status, trigger=trigger, started_at=when.isoformat())
    r.finish(status, **kw)
    store.record(conn, r)
    return r


# -- settings --------------------------------------------------------------- #


def test_defaults_are_populated(conn):
    assert store.get_int(conn, "interval_hours") == 24
    assert store.get(conn, "resume_path") == ""


def test_put_then_get_roundtrips(conn):
    store.put(conn, "interval_hours", 48)
    assert store.get_int(conn, "interval_hours") == 48


def test_settings_object_reflects_store(conn):
    store.put(conn, "interval_hours", 12)
    store.put(conn, "quiet_start", 22)
    s = store.settings(conn)
    assert s.interval_hours == 12 and s.quiet_start == 22


# -- scheduling state ------------------------------------------------------- #


def test_empty_store_has_no_history(conn):
    st = store.sched_state(conn)
    assert st.last_success_at is None
    assert st.consecutive_failures == 0


def test_tracks_last_success(conn):
    add(conn, Status.SUCCESS, T0 - timedelta(hours=30))
    add(conn, Status.FAILED, T0 - timedelta(hours=1))
    st = store.sched_state(conn)
    assert st.last_success_at == T0 - timedelta(hours=30)
    assert st.last_attempt_at == T0 - timedelta(hours=1)
    assert st.consecutive_failures == 1


def test_consecutive_failures_reset_on_success(conn):
    add(conn, Status.FAILED, T0 - timedelta(hours=5))
    add(conn, Status.FAILED, T0 - timedelta(hours=4))
    add(conn, Status.SUCCESS, T0 - timedelta(hours=3))
    assert store.sched_state(conn).consecutive_failures == 0


def test_dry_runs_do_not_affect_schedule(conn):
    """A dry run proves selectors resolve; it changes nothing on Naukri.

    Counting it as a success would silence the schedule for a full interval
    without the profile ever being touched.
    """
    add(conn, Status.SUCCESS, T0 - timedelta(hours=30))
    add(conn, Status.DRY_RUN, T0 - timedelta(minutes=5), trigger=Trigger.MANUAL)
    st = store.sched_state(conn)
    assert st.last_success_at == T0 - timedelta(hours=30)
    assert st.last_attempt_at == T0 - timedelta(hours=30)


def test_skipped_locked_does_not_affect_schedule(conn):
    add(conn, Status.SUCCESS, T0 - timedelta(hours=30))
    add(conn, Status.SKIPPED_LOCKED, T0 - timedelta(minutes=2))
    st = store.sched_state(conn)
    assert st.last_attempt_at == T0 - timedelta(hours=30)
    assert st.consecutive_failures == 0


def test_needs_login_is_not_counted_as_a_failure(conn):
    """It waits for a human - a retry ladder would burn attempts for nothing."""
    add(conn, Status.SUCCESS, T0 - timedelta(hours=40))
    add(conn, Status.NEEDS_LOGIN, T0 - timedelta(hours=1))
    st = store.sched_state(conn)
    assert st.consecutive_failures == 0
    assert st.last_status == Status.NEEDS_LOGIN


def test_needs_login_interrupts_a_failure_streak(conn):
    add(conn, Status.FAILED, T0 - timedelta(hours=3))
    add(conn, Status.NEEDS_LOGIN, T0 - timedelta(hours=1))
    assert store.sched_state(conn).consecutive_failures == 0


# -- runs ------------------------------------------------------------------- #


def test_record_roundtrips_all_fields(conn):
    add(conn, Status.FAILED, T0, error_kind="SELECTOR_MISS",
        error_detail="boom", profile_ts="Today", already_fresh=True)
    row = store.recent(conn, 1)[0]
    assert row["error_kind"] == "SELECTOR_MISS"
    assert row["error_detail"] == "boom"
    assert row["already_fresh"] == 1


def test_recent_is_newest_first(conn):
    add(conn, Status.SUCCESS, T0 - timedelta(hours=2))
    add(conn, Status.FAILED, T0 - timedelta(hours=1))
    rows = store.recent(conn, 10)
    assert rows[0]["status"] == Status.FAILED


def test_prune_removes_only_beyond_retention(conn, tmp_path, monkeypatch):
    from naukri_autopilot import config

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    shots = []
    for i in range(5):
        p = tmp_path / "s{}.png".format(i)
        p.write_bytes(b"x")
        shots.append(p)
        add(conn, Status.SUCCESS, T0 + timedelta(minutes=i), screenshot=p.name)

    removed = store.prune_screenshots(conn, keep=2)
    assert removed == 3
    assert sum(1 for p in shots if p.exists()) == 2


# -- lock ------------------------------------------------------------------- #


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "run.lock"
    with lock.exclusive(path) as first:
        assert first
        with lock.exclusive(path) as second:
            assert not second


def test_lock_is_released_on_exit(tmp_path):
    path = tmp_path / "run.lock"
    with lock.exclusive(path) as a:
        assert a
    with lock.exclusive(path) as b:
        assert b


def test_lock_released_even_when_body_raises(tmp_path):
    path = tmp_path / "run.lock"
    with pytest.raises(ValueError):
        with lock.exclusive(path) as a:
            assert a
            raise ValueError("boom")
    with lock.exclusive(path) as b:
        assert b
