"""Windows Task Scheduler registration.

The OS task is deliberately dumb: it fires `tick` every 15 minutes and nothing
else. All cadence logic lives in scheduler.py, so changing the interval is a
database write rather than a task re-registration (README section 2).

Registered at user scope, so no admin prompt and no stored password. The task
therefore only runs while the user is logged in - which the product spec already
requires.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

TASK_NAME = "NaukriAutopilot"
TICK_MINUTES = 15


def pythonw() -> Path:
    """The windowed interpreter beside the current one.

    Using python.exe would flash a console window 96 times a day, which is the
    fastest way to make someone uninstall a background tool. Falls back to the
    current interpreter if pythonw is missing (non-Windows, odd installs).
    """
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.is_file() else exe


def tick_command(interpreter: "Path | None" = None) -> str:
    """The /TR string. Quoted for the path-with-spaces case."""
    exe = interpreter or pythonw()
    return '"{}" -m naukri_autopilot.cli tick'.format(exe)


def build_create_args(
    task_name: str = TASK_NAME,
    minutes: int = TICK_MINUTES,
    interpreter: "Path | None" = None,
) -> "list[str]":
    return [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", tick_command(interpreter),
        "/SC", "MINUTE",
        "/MO", str(minutes),
        # No /RU or /RP: runs as the current user, only while logged on, and
        # needs neither elevation nor a stored password.
        "/F",
    ]


def build_delete_args(task_name: str = TASK_NAME) -> "list[str]":
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def build_query_args(task_name: str = TASK_NAME) -> "list[str]":
    return ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"]


@dataclass
class TaskInfo:
    registered: bool
    detail: str = ""
    next_run: "str | None" = None
    last_run: "str | None" = None
    last_result: "str | None" = None


def _run(args: "list[str]") -> "tuple[int, str]":
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, errors="replace", shell=False
        )
    except FileNotFoundError:
        return 127, "schtasks not found - this command is Windows-only"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_query(output: str) -> TaskInfo:
    """Pull the few fields worth showing out of `schtasks /Query /FO LIST /V`.

    Field labels are locale-dependent, so a miss here means an empty field, not
    an error - the registered/not-registered answer comes from the exit code.
    """
    info = TaskInfo(registered=True)
    for line in output.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip().lower()
        value = value.strip()
        if label == "next run time":
            info.next_run = value
        elif label == "last run time":
            info.last_run = value
        elif label == "last result":
            info.last_result = value
    return info


def query(task_name: str = TASK_NAME) -> TaskInfo:
    code, out = _run(build_query_args(task_name))
    if code != 0:
        return TaskInfo(registered=False, detail=out.strip().splitlines()[0] if out.strip() else "")
    return parse_query(out)


def register(
    task_name: str = TASK_NAME,
    minutes: int = TICK_MINUTES,
    interpreter: "Path | None" = None,
) -> "tuple[bool, str]":
    code, out = _run(build_create_args(task_name, minutes, interpreter))
    return code == 0, out.strip()


def unregister(task_name: str = TASK_NAME) -> "tuple[bool, str]":
    code, out = _run(build_delete_args(task_name))
    return code == 0, out.strip()


def log_path() -> Path:
    return config.DATA_DIR / "tick.log"


def log_line(message: str) -> None:
    """Last-resort log for scheduled runs.

    Under pythonw there is no console, so an unhandled crash before the store is
    reachable would otherwise vanish without trace. Ordinary run outcomes live
    in the runs table; this is only for the failures that never get that far.
    """
    try:
        config.ensure_dirs()
        from datetime import datetime, timezone

        with open(str(log_path()), "a", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(datetime.now(timezone.utc).isoformat(timespec="seconds"), message))
    except OSError:
        pass
