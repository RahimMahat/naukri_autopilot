"""Command surface for Naukri Autopilot."""

from __future__ import annotations

import argparse
import sys

from . import __version__, config, scheduling
from .results import RunResult, Status, Trigger

BROWSERS = ["auto", "brave", "chrome", "msedge", "chromium"]


def cmd_login(args) -> int:
    from .runner import login

    print("Opening a browser. Sign in there - nothing is typed for you.")
    result = login(channel=args.browser, timeout_s=args.timeout)
    if result.status == Status.SUCCESS:
        print("Signed in. Session saved to {}".format(config.STORAGE_STATE))
        print("That file is password-equivalent. Do not copy it anywhere.")
        return 0
    print(result.summary(), file=sys.stderr)
    return 1


def _resolve_resume(args, conn) -> "str | None":
    """CLI flag wins; otherwise the stored setting. Flag also updates the store."""
    from . import store

    if args.resume:
        store.put(conn, "resume_path", args.resume)
        return args.resume
    stored = store.get(conn, "resume_path")
    return stored or None


def cmd_run(args) -> int:
    from . import lock, store
    from .runner import run_once

    conn = store.connect()
    resume = _resolve_resume(args, conn)
    if not resume:
        print(
            "No resume configured. Pass --resume PATH once and it is remembered.",
            file=sys.stderr,
        )
        return 2

    with lock.exclusive() as acquired:
        if not acquired:
            print("Another run is in progress.", file=sys.stderr)
            result = RunResult(status=Status.SKIPPED_LOCKED, trigger=Trigger.MANUAL)
            result.finish(Status.SKIPPED_LOCKED)
            store.record(conn, result)
            return 1

        result = run_once(
            resume_path=resume,
            trigger=Trigger.MANUAL,
            dry_run=args.dry_run,
            offscreen=not args.headed,
            channel=args.browser,
        )

    # Dry runs are excluded from scheduling state but still worth a record.
    store.record(conn, result)
    store.prune_screenshots(conn)
    print(result.summary())
    if result.screenshot:
        print("screenshot: {}".format(result.screenshot))
    if result.status == Status.DRY_RUN:
        print("\nDry run - nothing was uploaded. Selectors all resolved.")
    if result.already_fresh and result.status == Status.SUCCESS:
        print("\nNote: profile already read 'Today' before this run, so the")
        print("upload could not be proven to have moved anything.")
    return 0 if result.ok else 1


def cmd_tick(args) -> int:
    """The heartbeat. Task Scheduler runs this every 15 minutes.

    Cheap and silent when nothing is due, which is the overwhelming majority of
    invocations - it must not open a browser just to decide.
    """
    from datetime import datetime, timezone

    from . import lock, store
    from .runner import run_once
    from .scheduler import decide

    conn = store.connect()
    now = datetime.now(timezone.utc)
    state = store.sched_state(conn)
    settings = store.settings(conn)
    decision = decide(now, state, settings)

    if args.explain or not decision.run:
        print(decision.describe(now))
    if not decision.run:
        return 0

    resume = store.get(conn, "resume_path")
    if not resume:
        print("No resume configured - run `naukri-autopilot run --resume PATH` once.",
              file=sys.stderr)
        return 2

    with lock.exclusive() as acquired:
        if not acquired:
            result = RunResult(status=Status.SKIPPED_LOCKED, trigger=decision.trigger)
            result.finish(Status.SKIPPED_LOCKED)
            store.record(conn, result)
            print("SKIPPED_LOCKED - another run is in progress")
            return 0

        result = run_once(
            resume_path=resume,
            trigger=decision.trigger,
            dry_run=False,
            offscreen=store.get_int(conn, "headed_mode", 0) == 0,
            channel="auto",
        )

    store.record(conn, result)
    store.prune_screenshots(conn)
    print(result.summary())
    return 0 if result.ok else 1


def cmd_status(args) -> int:
    from datetime import datetime, timezone

    from . import store
    from .scheduler import decide

    conn = store.connect()
    now = datetime.now(timezone.utc)
    state = store.sched_state(conn)
    settings = store.settings(conn)

    print("interval     : {}h".format(settings.interval_hours))
    print("quiet hours  : {}:00-{}:00 local".format(settings.quiet_start, settings.quiet_end))
    print("resume       : {}".format(store.get(conn, "resume_path") or "NOT SET"))
    print("last success : {}".format(state.last_success_at or "never"))
    print("last attempt : {} ({})".format(
        state.last_attempt_at or "never", state.last_status or "-"))
    print("failures     : {}".format(state.consecutive_failures))
    print("decision     : {}".format(decide(now, state, settings).describe(now)))

    rows = store.recent(conn, limit=args.limit)
    if rows:
        print("\nrecent runs:")
        for r in rows:
            print("  {}  {:<16} {:<9} {}".format(
                r["started_at"], r["status"], r["trigger"],
                r["error_detail"] or r["profile_ts"] or ""))
    return 0


def cmd_config(args) -> int:
    from . import store

    conn = store.connect()
    if args.key is None:
        for key in sorted(store.DEFAULTS):
            print("{:<22} {}".format(key, store.get(conn, key)))
        return 0
    if args.value is None:
        print(store.get(conn, args.key))
        return 0
    if args.key == "interval_hours":
        from .scheduler import VALID_INTERVALS

        if int(args.value) not in VALID_INTERVALS:
            print("interval_hours must be one of {}".format(
                ", ".join(str(v) for v in VALID_INTERVALS)), file=sys.stderr)
            return 2
    store.put(conn, args.key, args.value)
    print("{} = {}".format(args.key, args.value))
    return 0


def cmd_doctor(args) -> int:
    from . import diagnostics

    checks = diagnostics.run_all()
    for c in checks:
        print(c.render())
    level = diagnostics.worst(checks)
    print()
    if level == diagnostics.FAIL:
        print("Not working. Fix the [FAIL] lines above.")
        return 1
    if level == diagnostics.WARN:
        print("Working, with warnings.")
        return 0
    print("All good.")
    return 0


def cmd_install_task(args) -> int:
    from . import scheduling

    if args.remove:
        ok, out = scheduling.unregister(args.name)
        print(out or ("removed" if ok else "failed"))
        return 0 if ok else 1

    ok, out = scheduling.register(args.name, minutes=args.minutes)
    if not ok:
        print(out, file=sys.stderr)
        return 1
    print("Registered '{}' - fires `tick` every {} minutes.".format(
        args.name, args.minutes))
    print("Runs as you, only while you are logged in. No admin rights used.")
    print("\nThe task is deliberately dumb: it asks 'is a run due?' every")
    print("{} minutes and usually the answer is no. Your 12/24/48h interval".format(
        args.minutes))
    print("lives in the database - change it with `config interval_hours`,")
    print("no need to touch the task again.")

    info = scheduling.query(args.name)
    if info.next_run:
        print("\nNext tick: {}  (heartbeat, not a run)".format(info.next_run))
    print("Next run : {}".format(next_run_line()))
    return 0


def next_run_line() -> str:
    """When the profile is actually due, as opposed to when the task next wakes.

    The two get confused constantly: the tick fires 96 times a day and almost
    always decides to do nothing, so printing only the tick time reads as if a
    run were seconds away.
    """
    from datetime import datetime, timezone

    from . import store
    from .scheduler import decide, humanize

    conn = store.connect()
    try:
        now = datetime.now(timezone.utc)
        decision = decide(now, store.sched_state(conn), store.settings(conn))
    finally:
        conn.close()

    if decision.run:
        return "due now - the next tick will fire it ({})".format(decision.reason)
    if decision.next_due_at is None:
        return "on hold - {}".format(decision.reason)
    local = decision.next_due_at.astimezone()
    return "{}  (in {}, {})".format(
        local.strftime("%d-%m-%Y %H:%M:%S"),
        humanize(decision.next_due_at - now),
        decision.reason,
    )


def cmd_setup(args) -> int:
    """Checklist. Each line is verified, not taken on trust."""
    from . import diagnostics

    checks = diagnostics.run_all()
    print("Naukri Autopilot setup\n")
    for i, c in enumerate(checks, 1):
        print("{}. {}".format(i, c.render()))

    level = diagnostics.worst(checks)
    print()
    if level == diagnostics.FAIL:
        print("Work through the [FAIL] lines above, then run `setup` again.")
        return 1
    print("Setup complete. The scheduler will take it from here.")
    print("Watch it with:  naukri-autopilot status")
    return 0


def cmd_inspect(args) -> int:
    from .probe import inspect

    return inspect(channel=args.browser, headed=args.headed)


def cmd_dashboard(args) -> int:
    try:
        from .dashboard.app import serve
    except ImportError:
        print('Dashboard extras are not installed. Run:\n'
              '    pip install -e ".[dashboard]"', file=sys.stderr)
        return 2
    # 127.0.0.1 is not configurable on purpose: the page can trigger a browser
    # run and read back screenshots, and it is unauthenticated by design.
    return serve(port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="naukri-autopilot",
        description="Keeps your Naukri profile fresh on a schedule, on your own machine.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", metavar="command")

    def browser_flag(sp):
        sp.add_argument("--browser", default="auto", choices=BROWSERS,
                        help="auto tries brave, chrome, msedge, then bundled chromium")
        return sp

    login_p = browser_flag(sub.add_parser("login", help="sign in and save the session"))
    login_p.add_argument("--timeout", type=int, default=300,
                         help="seconds to wait for manual sign-in")
    login_p.set_defaults(func=cmd_login)

    run_p = browser_flag(sub.add_parser("run", help="update the profile once, now"))
    run_p.add_argument("--resume", help="path to your resume PDF")
    run_p.add_argument("--dry-run", action="store_true",
                       help="resolve everything, upload nothing")
    run_p.add_argument("--headed", action="store_true",
                       help="show the browser window (default: parked offscreen)")
    run_p.set_defaults(func=cmd_run)

    tick_p = sub.add_parser("tick", help="run only if one is due (Task Scheduler)")
    tick_p.add_argument("--explain", action="store_true",
                        help="print the decision even when it is to run")
    tick_p.set_defaults(func=cmd_tick)

    status_p = sub.add_parser("status", help="schedule state and recent runs")
    status_p.add_argument("--limit", type=int, default=10)
    status_p.set_defaults(func=cmd_status)

    cfg_p = sub.add_parser("config", help="read or write a setting")
    cfg_p.add_argument("key", nargs="?")
    cfg_p.add_argument("value", nargs="?")
    cfg_p.set_defaults(func=cmd_config)

    insp_p = browser_flag(sub.add_parser(
        "inspect", help="check which Naukri selectors still resolve"))
    insp_p.add_argument("--headed", action="store_true", default=True,
                        help="show the browser (default)")
    insp_p.set_defaults(func=cmd_inspect)

    doctor_p = sub.add_parser("doctor", help="diagnose a broken install")
    doctor_p.set_defaults(func=cmd_doctor)

    setup_p = sub.add_parser("setup", help="first-run checklist")
    setup_p.set_defaults(func=cmd_setup)

    task_p = sub.add_parser("install-task",
                            help="register the 15-minute Task Scheduler heartbeat")
    task_p.add_argument("--name", default=scheduling.TASK_NAME, help="task name")
    task_p.add_argument("--minutes", type=int, default=scheduling.TICK_MINUTES,
                        help="heartbeat interval")
    task_p.add_argument("--remove", action="store_true", help="unregister instead")
    task_p.set_defaults(func=cmd_install_task)

    dash_p = sub.add_parser("dashboard", help="serve the local dashboard on 127.0.0.1:8765")
    dash_p.add_argument("--port", type=int, default=8765)
    dash_p.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    dash_p.set_defaults(func=cmd_dashboard)

    return p


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    config.ensure_dirs()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
