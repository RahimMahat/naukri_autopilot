"""Command surface for Naukri Autopilot.

`login` and `run` are live (Phase 1). The rest are declared so the shape of the
tool is fixed, and each says which phase it lands in rather than pretending.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config
from .results import RunResult, Status, Trigger

PHASES = {"dashboard": 3, "setup": 4, "doctor": 4}

BROWSERS = ["auto", "brave", "chrome", "msedge", "chromium"]


def _not_yet(command: str) -> int:
    print(
        "'{}' lands in Phase {} - see README section 12.".format(
            command, PHASES[command]
        ),
        file=sys.stderr,
    )
    return 2


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

    for name, help_text in (
        ("dashboard", "serve the local dashboard on 127.0.0.1:8765"),
        ("setup", "first-run checklist"),
        ("doctor", "diagnose a broken install"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=lambda a, n=name: _not_yet(n))

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
