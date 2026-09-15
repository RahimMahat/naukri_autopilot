"""Chart rendering and dashboard routes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from naukri_autopilot import config, store
from naukri_autopilot.dashboard import chart
from naukri_autopilot.results import RunResult, Status, Trigger

TODAY = date(2026, 9, 14)


# -- chart ------------------------------------------------------------------ #


def test_success_outranks_failure_on_the_same_day():
    """A day that ended with the profile updated is a good day, however many
    retries it took."""
    assert chart.day_status([Status.FAILED, Status.SUCCESS]) == Status.SUCCESS


def test_failure_outranks_needs_login():
    assert chart.day_status([Status.NEEDS_LOGIN, Status.FAILED]) == Status.FAILED


def test_bookkeeping_statuses_do_not_colour_a_day():
    assert chart.day_status([Status.SKIPPED_LOCKED]) is None
    assert chart.day_status([]) is None


def test_bucket_groups_by_calendar_day():
    rows = [
        ("2026-09-14T08:00:00+00:00", Status.FAILED),
        ("2026-09-14T09:00:00+00:00", Status.SUCCESS),
        ("2026-09-13T09:00:00+00:00", Status.SUCCESS),
    ]
    by_day = chart.bucket_by_day(rows)
    assert by_day[date(2026, 9, 14)] == Status.SUCCESS
    assert len(by_day) == 2


def test_bucket_ignores_unparseable_timestamps():
    assert chart.bucket_by_day([("not-a-date", Status.SUCCESS), (None, Status.SUCCESS)]) == {}


def test_render_produces_svg_without_external_references():
    """No CDN, no webfont, no <image href>. The page must work offline and make
    no outbound request at all - see the README's Security model."""
    svg = chart.render([("2026-09-14T09:00:00+00:00", Status.SUCCESS)], today=TODAY)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    for bad in ("http://", "https://", "//cdn", "<script"):
        assert bad not in svg


def test_render_marks_the_right_day():
    svg = chart.render([("2026-09-14T09:00:00+00:00", Status.SUCCESS)], today=TODAY)
    assert '<title>2026-09-14 — updated</title>' in svg
    assert svg.count('class="cell ok"') == 1


def test_render_omits_future_days():
    svg = chart.render([], today=TODAY)
    assert (TODAY + timedelta(days=1)).isoformat() not in svg


def test_render_escapes_status_labels():
    svg = chart.render([("2026-09-14T09:00:00+00:00", "<script>")], today=TODAY)
    assert "<script>" not in svg


# -- streak ----------------------------------------------------------------- #


def days(*offsets, status=Status.SUCCESS):
    return chart.bucket_by_day(
        [((TODAY - timedelta(days=o)).isoformat() + "T09:00:00+00:00", status)
         for o in offsets]
    )


def test_streak_counts_consecutive_success_days():
    assert chart.streak(days(0, 1, 2), today=TODAY) == 3


def test_streak_survives_a_run_not_yet_fired_today():
    """With a 24h interval plus jitter, today's run may simply not have happened
    yet. Resetting the streak at midnight would be wrong and dispiriting."""
    assert chart.streak(days(1, 2, 3), today=TODAY) == 3


def test_streak_breaks_after_two_missed_days():
    assert chart.streak(days(2, 3, 4), today=TODAY) == 0


def test_streak_stops_at_a_gap():
    assert chart.streak(days(0, 1, 3), today=TODAY) == 2


def test_streak_of_nothing_is_zero():
    assert chart.streak(chart.bucket_by_day([]), today=TODAY) == 0


def test_failed_day_does_not_extend_a_streak():
    by_day = days(1, 2)
    by_day.update(days(0, status=Status.FAILED))
    assert chart.streak(by_day, today=TODAY) == 2


# -- routes ----------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "run.lock")
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path / "shots")
    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    from naukri_autopilot import scheduling
    monkeypatch.setattr(scheduling, "query",
                        lambda *a, **k: scheduling.TaskInfo(registered=False))

    from naukri_autopilot.dashboard.app import create_app

    return fastapi_testclient.TestClient(create_app())


def seed(status=Status.SUCCESS, hours_ago=1, **kw):
    conn = store.connect()
    try:
        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        r = RunResult(status=status, trigger=Trigger.SCHEDULE, started_at=when.isoformat())
        return store.record(conn, r.finish(status, **kw))
    finally:
        conn.close()


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Naukri Autopilot" in r.text


def test_index_makes_no_external_requests(client):
    """The privacy claim is absolute - one stray CDN link would break it."""
    body = client.get("/").text
    for bad in ("http://", "https://", "cdn.", "fonts.googleapis"):
        assert bad not in body


def test_needs_login_banner_appears(client):
    seed(Status.SUCCESS, hours_ago=40)
    seed(Status.NEEDS_LOGIN, hours_ago=1)
    assert "Sign-in needed" in client.get("/").text


def test_stale_banner_appears(client):
    seed(Status.SUCCESS, hours_ago=100)
    assert "Not updating" in client.get("/").text


def test_api_status_is_json(client):
    body = client.get("/api/status").json()
    assert body["running"] is False
    assert "decision" in body


def test_settings_rejects_unsupported_interval(client):
    r = client.post("/settings", data={"interval_hours": "6"})
    assert r.status_code == 400


def test_settings_round_trip(client):
    r = client.post("/settings", data={
        "interval_hours": "12", "resume_path": "C:/cv.pdf",
        "quiet_start": "22", "quiet_end": "6", "headed_mode": "1"},
        follow_redirects=False)
    assert r.status_code == 303
    conn = store.connect()
    try:
        assert store.get_int(conn, "interval_hours") == 12
        assert store.get(conn, "resume_path") == "C:/cv.pdf"
        assert store.get(conn, "headed_mode") == "1"
    finally:
        conn.close()


def test_unchecked_headed_mode_clears_the_flag(client):
    """An unchecked checkbox submits nothing; it must read as off, not unchanged."""
    client.post("/settings", data={"interval_hours": "24", "headed_mode": "1"})
    client.post("/settings", data={"interval_hours": "24"})
    conn = store.connect()
    try:
        assert store.get(conn, "headed_mode") == "0"
    finally:
        conn.close()


# -- screenshot serving ----------------------------------------------------- #


def test_screenshot_served_for_a_valid_run(client, tmp_path):
    shots = tmp_path / "shots"
    shots.mkdir(exist_ok=True)
    (shots / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    run_id = seed(screenshot="shots/a.png")
    r = client.get("/screenshot/{}".format(run_id))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_screenshot_404_for_unknown_run(client):
    assert client.get("/screenshot/9999").status_code == 404


def test_screenshot_refuses_paths_outside_the_screenshot_dir(client, tmp_path):
    """Serving by id and re-checking the resolved path is what keeps a crafted
    database row from reading arbitrary files off disk."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top secret")
    run_id = seed(screenshot="../secret.png")
    assert client.get("/screenshot/{}".format(run_id)).status_code == 404


def test_screenshot_404_when_file_is_gone(client):
    run_id = seed(screenshot="shots/missing.png")
    assert client.get("/screenshot/{}".format(run_id)).status_code == 404


def test_month_labels_mark_each_new_month():
    """Without labels the grid is decorative - you cannot tell what it covers."""
    labels = chart.month_labels(date(2026, 3, 30), weeks=14)
    names = [n for _, n in labels]
    assert names[:4] == ["Mar", "Apr", "May", "Jun"]
    assert all(0 <= w < 14 for w, _ in labels)


def test_month_labels_skip_the_final_column():
    """There is no room to draw a label in the last column."""
    labels = chart.month_labels(date(2026, 1, 1), weeks=5)
    assert all(w < 4 for w, _ in labels)


def test_rendered_grid_includes_month_labels():
    svg = chart.render([], today=TODAY)
    assert 'class="mlabel"' in svg
    assert "Sep" in svg
