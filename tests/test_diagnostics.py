"""Health checks. Each must name a fix when it fails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from naukri_autopilot import config, diagnostics as dx, store
from naukri_autopilot.results import RunResult, Status, Trigger


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path / "s")
    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "d")
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


def add(conn, status, hours_ago, **kw):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    r = RunResult(status=status, trigger=Trigger.SCHEDULE, started_at=when.isoformat())
    store.record(conn, r.finish(status, **kw))


# -- resume ----------------------------------------------------------------- #


def test_resume_unset_fails_with_a_fix(conn):
    c = dx.check_resume(conn)
    assert c.state == dx.FAIL and "resume_path" in c.fix


def test_resume_missing_file_fails(conn, tmp_path):
    store.put(conn, "resume_path", str(tmp_path / "gone.pdf"))
    assert dx.check_resume(conn).state == dx.FAIL


def test_resume_ok(conn, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    store.put(conn, "resume_path", str(pdf))
    assert dx.check_resume(conn).state == dx.OK


def test_resume_in_downloads_warns(conn, tmp_path):
    """Downloads is one browser cleanup away from silently breaking the
    schedule, and nobody would notice until they read the history."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    pdf = downloads / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    store.put(conn, "resume_path", str(pdf))
    c = dx.check_resume(conn)
    assert c.state == dx.WARN and "permanent" in c.fix


def test_non_pdf_warns(conn, tmp_path):
    doc = tmp_path / "cv.docx"
    doc.write_bytes(b"x")
    store.put(conn, "resume_path", str(doc))
    assert dx.check_resume(conn).state == dx.WARN


# -- history ---------------------------------------------------------------- #


def test_no_runs_warns_not_fails(conn):
    """A fresh install is incomplete, not broken."""
    assert dx.check_history(conn).state == dx.WARN


def test_recent_success_is_ok(conn):
    add(conn, Status.SUCCESS, hours_ago=2)
    assert dx.check_history(conn).state == dx.OK


def test_stale_success_fails(conn):
    """Beyond 2x the interval the tool has quietly stopped working - which is
    the failure this product must never hide."""
    store.put(conn, "interval_hours", 24)
    add(conn, Status.SUCCESS, hours_ago=72)
    c = dx.check_history(conn)
    assert c.state == dx.FAIL and "stale" in c.detail


def test_needs_login_points_at_login(conn):
    add(conn, Status.SUCCESS, hours_ago=4)
    add(conn, Status.NEEDS_LOGIN, hours_ago=1)
    c = dx.check_history(conn)
    assert c.state == dx.FAIL and "login" in c.fix


def test_failures_warn_while_still_within_the_window(conn):
    add(conn, Status.SUCCESS, hours_ago=5)
    add(conn, Status.FAILED, hours_ago=1)
    assert dx.check_history(conn).state == dx.WARN


# -- aggregation ------------------------------------------------------------ #


def test_worst_prefers_fail_then_warn():
    assert dx.worst([dx.Check("a", dx.OK), dx.Check("b", dx.WARN)]) == dx.WARN
    assert dx.worst([dx.Check("a", dx.WARN), dx.Check("b", dx.FAIL)]) == dx.FAIL
    assert dx.worst([dx.Check("a", dx.OK)]) == dx.OK


def test_every_failing_check_offers_a_fix(conn):
    """A diagnostic without a next step is a slower way of saying nothing."""
    failing = [dx.check_resume(conn), dx.check_session()]
    for c in failing:
        if c.state == dx.FAIL:
            assert c.fix, "{} failed without a fix".format(c.name)


def test_render_includes_the_fix_only_when_not_ok():
    assert "->" not in dx.Check("x", dx.OK, "fine", fix="do thing").render()
    assert "-> do thing" in dx.Check("x", dx.FAIL, "bad", fix="do thing").render()
