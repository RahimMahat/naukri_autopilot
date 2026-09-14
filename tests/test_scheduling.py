"""schtasks argument construction and output parsing.

The pure parts are tested directly; register/unregister are tested through a
stubbed subprocess so the suite never touches the real Task Scheduler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from naukri_autopilot import scheduling


# -- command construction --------------------------------------------------- #


def test_tick_command_quotes_the_interpreter():
    """Paths contain spaces far more often than not on Windows."""
    cmd = scheduling.tick_command(Path(r"C:\Program Files\py\pythonw.exe"))
    assert cmd.startswith('"C:\\Program Files\\py\\pythonw.exe"')
    assert cmd.endswith("-m naukri_autopilot.cli tick")


def test_create_args_use_minute_schedule():
    args = scheduling.build_create_args(minutes=15)
    assert args[:2] == ["schtasks", "/Create"]
    assert "/SC" in args and args[args.index("/SC") + 1] == "MINUTE"
    assert args[args.index("/MO") + 1] == "15"
    assert "/F" in args


def test_create_args_never_request_elevation_or_a_password():
    """User scope keeps setup to a single command with no admin prompt, and
    means no password is ever stored to run the task."""
    args = scheduling.build_create_args()
    assert "/RU" not in args
    assert "/RP" not in args
    assert "/RL" not in args


def test_task_name_flows_through_every_command():
    for build in (
        scheduling.build_create_args,
        scheduling.build_delete_args,
        scheduling.build_query_args,
    ):
        args = build("CustomName")
        assert args[args.index("/TN") + 1] == "CustomName"


def test_prefers_pythonw_to_avoid_console_flashes():
    """python.exe would pop a console window 96 times a day."""
    exe = scheduling.pythonw()
    assert exe.name in ("pythonw.exe", Path(exe).name)


# -- query parsing ---------------------------------------------------------- #

SAMPLE = """
Folder: \\
HostName:                             DESKTOP
TaskName:                             \\NaukriAutopilot
Next Run Time:                        14-09-2026 20:15:00
Status:                               Ready
Last Run Time:                        14-09-2026 20:00:00
Last Result:                          0
Author:                               DESKTOP\\RAHIM
Schedule Type:                        One Time Only, Minute
"""


def test_parse_query_extracts_the_useful_fields():
    info = scheduling.parse_query(SAMPLE)
    assert info.registered
    assert info.next_run == "14-09-2026 20:15:00"
    assert info.last_run == "14-09-2026 20:00:00"
    assert info.last_result == "0"


def test_parse_query_survives_unknown_locale():
    """Field labels are localised; a miss must be an empty field, not a crash."""
    info = scheduling.parse_query("Nächste Laufzeit: 20:15:00\nStatus: Bereit")
    assert info.registered
    assert info.next_run is None


def test_parse_query_handles_empty_output():
    assert scheduling.parse_query("").registered


# -- subprocess boundary ---------------------------------------------------- #


@pytest.fixture
def fake_schtasks(monkeypatch):
    calls = {"args": None, "code": 0, "out": SAMPLE}

    def _run(args):
        calls["args"] = args
        return calls["code"], calls["out"]

    monkeypatch.setattr(scheduling, "_run", _run)
    return calls


def test_register_reports_success(fake_schtasks):
    ok, _ = scheduling.register()
    assert ok
    assert fake_schtasks["args"][1] == "/Create"


def test_register_reports_failure(fake_schtasks):
    fake_schtasks["code"] = 1
    fake_schtasks["out"] = "ERROR: Access is denied."
    ok, out = scheduling.register()
    assert not ok and "denied" in out


def test_query_reports_not_registered_on_nonzero_exit(fake_schtasks):
    fake_schtasks["code"] = 1
    fake_schtasks["out"] = "ERROR: The system cannot find the file specified."
    info = scheduling.query()
    assert not info.registered


def test_missing_schtasks_is_not_a_crash(monkeypatch):
    """schtasks is absent off Windows; doctor must still render."""
    def _boom(args, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(scheduling.subprocess, "run", _boom)
    assert not scheduling.query().registered


# -- last-resort log -------------------------------------------------------- #


def test_log_line_writes(tmp_path, monkeypatch):
    from naukri_autopilot import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path / "s")
    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "d")
    scheduling.log_line("hello")
    assert "hello" in (tmp_path / "tick.log").read_text(encoding="utf-8")


def test_log_line_never_raises(monkeypatch):
    """Under pythonw there is nobody to see a logging failure; it must not
    take the run down with it."""
    from naukri_autopilot import config

    monkeypatch.setattr(config, "ensure_dirs", lambda: (_ for _ in ()).throw(OSError()))
    scheduling.log_line("still fine")
