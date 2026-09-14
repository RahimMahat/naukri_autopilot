"""`tick` wiring: decision -> lock -> run -> record. No browser involved.

run_once is stubbed. What is under test is the plumbing between the scheduler,
the lock and the store, which is where a heartbeat command goes wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from naukri_autopilot import cli, config, runner, store
from naukri_autopilot.results import RunResult, Status, Trigger

T_NOW = datetime.now(timezone.utc)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect all on-disk state into tmp_path."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "run.lock")
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path / "shots")
    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    conn = store.connect(tmp_path / "state.db")
    store.put(conn, "resume_path", str(tmp_path / "cv.pdf"))
    (tmp_path / "cv.pdf").write_bytes(b"%PDF-1.4\n")
    yield conn
    conn.close()


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _fake(resume_path, trigger, dry_run=False, offscreen=True, channel="auto"):
        calls.append({"trigger": trigger, "dry_run": dry_run, "offscreen": offscreen})
        r = RunResult(status=Status.SUCCESS, trigger=trigger)
        return r.finish(Status.SUCCESS, profile_ts="Today")

    monkeypatch.setattr(runner, "run_once", _fake)
    return calls


def tick(explain=False):
    return cli.main(["tick"] + (["--explain"] if explain else []))


def test_tick_runs_when_due(sandbox, fake_run):
    assert tick() == 0
    assert len(fake_run) == 1
    assert store.sched_state(sandbox).last_success_at is not None


def test_tick_holds_when_not_due(sandbox, fake_run):
    assert tick() == 0
    fake_run.clear()
    assert tick() == 0
    assert fake_run == []  # second tick inside the interval does nothing


def test_tick_is_cheap_when_holding(sandbox, fake_run):
    """The overwhelming majority of ticks hold; none may open a browser."""
    tick()
    fake_run.clear()
    for _ in range(10):
        tick()
    assert fake_run == []


def test_tick_records_the_run(sandbox, fake_run):
    tick()
    rows = store.recent(sandbox, 5)
    assert rows[0]["status"] == Status.SUCCESS
    assert rows[0]["trigger"] == Trigger.SCHEDULE


def test_tick_uses_catchup_trigger_after_a_long_gap(sandbox, fake_run):
    old = (T_NOW - timedelta(days=5)).isoformat()
    r = RunResult(status=Status.SUCCESS, trigger=Trigger.SCHEDULE, started_at=old)
    store.record(sandbox, r.finish(Status.SUCCESS))
    tick()
    assert fake_run[-1]["trigger"] == Trigger.CATCHUP


def test_tick_defers_to_headed_mode_setting(sandbox, fake_run):
    store.put(sandbox, "headed_mode", "1")
    tick()
    assert fake_run[-1]["offscreen"] is False


def test_tick_never_dry_runs(sandbox, fake_run):
    """A scheduled tick that silently dry-ran would look healthy forever."""
    tick()
    assert fake_run[-1]["dry_run"] is False


def test_tick_without_resume_refuses(sandbox, fake_run):
    store.put(sandbox, "resume_path", "")
    assert tick() == 2
    assert fake_run == []


def test_tick_skips_when_locked(sandbox, fake_run, monkeypatch):
    from naukri_autopilot import lock

    with lock.exclusive(config.LOCK_PATH) as held:
        assert held
        assert tick() == 0
    assert fake_run == []
    assert store.recent(sandbox, 1)[0]["status"] == Status.SKIPPED_LOCKED


def test_locked_tick_does_not_poison_the_schedule(sandbox, fake_run):
    """SKIPPED_LOCKED must not read as an attempt, or the real run's retry
    ladder and staleness alert both start lying."""
    from naukri_autopilot import lock

    with lock.exclusive(config.LOCK_PATH):
        tick()
    st = store.sched_state(sandbox)
    assert st.last_attempt_at is None
    assert st.consecutive_failures == 0
