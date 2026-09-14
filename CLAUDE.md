# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (once) - setup.ps1 / setup.sh do all of this, and are what users run
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m playwright install chromium

# Tests - fast, no network, synthetic fixtures in a headless browser
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest tests/test_profile.py::test_reads_stale_profile -q

# Drive the tool
.venv/Scripts/python.exe -m naukri_autopilot.cli login
.venv/Scripts/python.exe -m naukri_autopilot.cli run --dry-run --resume PATH.pdf
.venv/Scripts/python.exe -m naukri_autopilot.cli run --headed --resume PATH.pdf

# Schedule state - all read-only except tick, which may fire a real run
.venv/Scripts/python.exe -m naukri_autopilot.cli status
.venv/Scripts/python.exe -m naukri_autopilot.cli config
.venv/Scripts/python.exe -m naukri_autopilot.cli config interval_hours 12
.venv/Scripts/python.exe -m naukri_autopilot.cli tick --explain

# Diagnose and schedule
.venv/Scripts/python.exe -m naukri_autopilot.cli doctor
.venv/Scripts/python.exe -m naukri_autopilot.cli install-task
.venv/Scripts/python.exe -m naukri_autopilot.cli install-task --remove

# Dashboard (127.0.0.1 only, opens a browser)
.venv/Scripts/python.exe -m naukri_autopilot.cli dashboard
.venv/Scripts/python.exe -m naukri_autopilot.cli dashboard --no-browser --port 8799

# Rediscover selectors after a Naukri redesign (read-only)
.venv/Scripts/python.exe -m naukri_autopilot.cli inspect
```

`run` parks the browser offscreen by default. Pass `--headed` when debugging, or you
are staring at a blank terminal wondering what the browser is doing.

**Shell note:** the `!` prefix in Claude Code runs Git Bash here, not PowerShell.
Use forward slashes — `.venv\Scripts\python` silently collapses to `.venvScriptspython`.

## Architecture

Read `README.md` first; it is the design doc and records *why* each decision was made.
The short version:

```
cli.py ──► scheduler.decide()  (pure: now + state + settings -> Decision)
       ──► lock.exclusive()    (OS file lock, never blocks)
       ──► runner.run_once() ──► driver/session.py   (browser + login state)
                             ──► driver/profile.py   (page operations)
                             ──► driver/selectors.py (every selector, fallback chains)
       ──► store.py            (SQLite: runs, settings, headlines)
                                 results.py          (Status / ErrorKind / RunResult)
```

`scheduler.py` is pure - no DB, no browser, no wall clock. It takes an injected `now`
and a `SchedState` snapshot. That is what makes catch-up, quiet hours, retry ladders and
DST testable at all; keep it that way.

`runner.run_once()` never raises — every path returns a `RunResult` with a terminal
status and, where a page existed, a screenshot. The scheduler decides *whether* to call
it; the runner owns *what happens* when it is called.

**Phase status:** all five phases are done (0 recon, 1 driver, 2 state + scheduler,
3 dashboard, 4 setup + scheduling). Every CLI command is live. See README section 12.

## Invariants — these are load-bearing, not style preferences

**Never address file inputs by index.** `#attachCV` is the resume; `#fileUpload`
immediately beside it is the **profile photo**. An index-based or bare
`input[type=file]` selector can put a PDF into the user's profile picture. Every
fallback in `RESUME_INPUT` carries `:not([accept*='image'])`, and a test enforces it.

**Verify freshness by assertion, never by comparison.** Success is
`.mod-date-val == "Today"` after a reload — not `before != after`. The field is
day-granular, so a second run the same day reads `Today` → `Today`; a diff-based check
reports `FAILED` on a run that worked. Retries, manual runs after a scheduled one, and
catch-up all hit that path.

**Only `profile_updated` is a required selector.** If it stops resolving we need
`SELECTOR_MISS` plus a DOM dump. Making it optional yields a `None` that reads as
"stale" and blames the upload — wrong diagnosis, no evidence. The other fields are
informational.

**Only SUCCESS, FAILED and NEEDS_LOGIN count as attempts.** `DRY_RUN` and `SKIPPED_*`
are recorded for history but excluded from `store.sched_state()`. A dry run treated as a
success would silence the schedule for a full interval without touching Naukri; a
`SKIPPED_LOCKED` treated as an attempt corrupts the retry ladder and the staleness alert.

**`NEEDS_LOGIN` is not a failure.** No retry ladder - the session cannot recover without
a human. Normal cadence still applies so the tool heals itself once the user signs in.

**Jitter is derived by hashing the anchor timestamp, never drawn fresh.** It is
recomputed on every 15-minute tick, so a random value would make the due time wander and
the run would never fire.

**The scheduled task must stay dumb.** It fires `tick` every 15 minutes and knows
nothing about 12/24/48h. Teaching the OS task the real interval would break catch-up,
reintroduce drift, and make every interval change an admin-level re-registration.

**Scheduled runs use `pythonw.exe`.** `python.exe` flashes a console 96 times a day.
The cost is no stdout, so anything that dies before the store is reachable goes to
`data/tick.log` via `scheduling.log_line` - which must never raise.

**The dashboard binds 127.0.0.1 and is unauthenticated.** That is only safe because it
is unreachable off-box - it can trigger a browser run and read back screenshots of the
user's profile. Never bind 0.0.0.0, and never add a "share on the network" option.

**The dashboard makes zero outbound requests.** No CDN, no webfont, no chart library -
hence the hand-rolled SVG in `dashboard/chart.py`. A test asserts the rendered page
contains no external URL; keep it passing.

**Screenshots are served by run id, never by path.** The id is looked up and the
resolved path re-checked against `SCREENSHOT_DIR`. A filename from the URL would be
path traversal into the user's filesystem.

**Runtime dependencies stay in `dependencies`, not an extra.** The dashboard used to
sit behind `[dashboard]`, so a plain `pip install -e .` produced an install where the
`dashboard` command failed at runtime. Optional extras are for dev tooling only.

**`setup.ps1` and `setup.sh` must stay in step.** Users run one of them; a fix applied
to only one is a fix half the users never get.

**Selectors live only in `driver/selectors.py`.** A Naukri redesign is the expected
long-term maintenance burden; keeping it a one-file repair is why the rest of the
driver stays short.

**Bundled Chromium cannot complete Google OAuth sign-in.** Google refuses it with
"this browser or app may not be secure". `CHANNEL_PREFERENCE` tries Brave, Chrome,
Edge, then bundled. Brave is driven by `executable_path` (no Playwright channel).
Do not "simplify" this to plain `chromium`.

**Chromium builds cannot share a `user-data-dir`.** Each gets its own via
`config.profile_dir(name)`. Sharing fails as a misleading "already in use by another
instance".

## Constraints

- **Python 3.9 floor** (the venv runs 3.13; the target does not). `from __future__ import
  annotations` everywhere, quote runtime-evaluated annotations, no `match`, no PEP 604
  unions outside annotations.
- **No outbound calls except naukri.com.** The privacy claim is absolute: no telemetry,
  no CDN, no update check. The dashboard's chart is hand-rolled SVG for this reason.
- **Session state is password-equivalent** and lives in `%LOCALAPPDATA%\NaukriAutopilot\`,
  never in the repo. Naukri auth cookies last ~180 days.
- `data/` is gitignored (screenshots, DOM dumps, SQLite) and so is `*.pdf`.

## Testing

Fixtures in `tests/fixtures/` are **synthetic** — faithful DOM structure, invented
values. Real captures contain the user's name, location and resume filename; never
commit one. After a Naukri redesign: run `naukri-autopilot inspect`, update the fixture
structure from the capture in `data/debug/`, then fix `selectors.py` until the tests pass
again. `inspect` drives the real driver modules on purpose - never give it its own copy
of the browser plumbing.
