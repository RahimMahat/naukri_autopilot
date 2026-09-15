"""Windows Task Scheduler registration.

The OS task is deliberately dumb: it fires `tick` every 15 minutes and nothing
else. All cadence logic lives in scheduler.py, so changing the interval is a
database write rather than a task re-registration (README, Architecture).

Registered at user scope, so no admin prompt and no stored password. The task
therefore only runs while the user is logged in - which the product spec already
requires.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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


# Registered from XML rather than plain `schtasks /Create` flags, for one
# reason: the command-line form cannot set the battery policy, and its defaults
# are DisallowStartIfOnBatteries=true and StopIfGoingOnBatteries=true. On a
# laptop that means the heartbeat never fires unless mains power is connected -
# and it fails silently, because the task still registers and still looks
# healthy. This is a background tool for laptops; it has to run on battery.
#
# WakeToRun is deliberately left off. Waking a sleeping machine to touch a job
# board is rude, and catch-up already covers it: the first tick after the lid
# opens sees the run is overdue and fires it.
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keeps your Naukri profile fresh. Asks every {minutes} minutes whether a run is due; almost always the answer is no.</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>false</WakeToRun>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>-m naukri_autopilot.cli tick</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return "{}\\{}".format(domain, user) if domain else user


def build_task_xml(
    task_name: str = TASK_NAME,
    minutes: int = TICK_MINUTES,
    interpreter: "Path | None" = None,
) -> str:
    exe = interpreter or pythonw()
    return _TASK_XML.format(
        name=task_name,
        minutes=minutes,
        user=current_user(),
        exe=str(exe),
        # Any past instant works; the repetition is what actually drives it.
        start="2026-01-01T00:00:00",
    )


def build_create_args(
    task_name: str = TASK_NAME,
    xml_path: "Path | None" = None,
) -> "list[str]":
    return ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"]


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
    """Register the heartbeat. Writes the XML to a temp file schtasks can read.

    The file is UTF-16: schtasks rejects UTF-8 task XML on some Windows builds
    with an unhelpful parse error.
    """
    xml = build_task_xml(task_name, minutes, interpreter)
    tmp = Path(tempfile.gettempdir()) / "naukri-autopilot-task.xml"
    try:
        tmp.write_text(xml, encoding="utf-16")
        code, out = _run(build_create_args(task_name, tmp))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
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
