# Naukri Autopilot

Keeps your Naukri profile at the top of recruiter search results by making a small,
real change to it on a schedule — automatically, on your own machine.

Recruiters sort candidate searches by "recently updated" by default. A profile that
hasn't changed in weeks sinks, regardless of quality. The manual fix is to log in daily
and tweak something. This automates that tweak.

**Everything runs locally.** No server, no account, no data leaves the machine. You log
into Naukri yourself in a real browser window; only the resulting session cookie is
stored, in a folder you control.

---

## 1. How the "update" actually works

One lever does the work; a second is held in reserve.

| Lever | Effect | Status |
|---|---|---|
| **Resume re-upload** | Re-uploading the same PDF moves the profile's *last updated* timestamp | **Primary.** Measured against a live account on 2026-09-14: `22Jul , 2026` → `Today`, with a byte-identical file. No visible change to the profile. |
| **Headline rotation** | Cycles through N user-written variants of the resume headline | **Backstop, off by default.** Only needed if Naukri ever stops counting an identical re-upload as a change. |

Because the primary lever is confirmed, rotation stays unimplemented (§14.2): the
headline sits behind an edit modal, and that work is not worth doing until the backstop
is actually needed.

Headline variants, if ever enabled, are written by the user and must all be truthful
descriptions of the same person — this rotates phrasing, not facts.

---

## 2. Architecture

```
                        ┌──────────────────────────────┐
  Windows Task          │  tick  (every 15 min)        │
  Scheduler  ─────────► │  "is a run due right now?"   │
                        └───────────────┬──────────────┘
                                        │ yes
                                        ▼
  ┌──────────┐   settings   ┌───────────────────────┐   writes   ┌────────────┐
  │ Dashboard│ ◄──────────► │      Scheduler        │ ─────────► │  SQLite    │
  │ 127.0.0.1│    runs      │  (due / catch-up /    │            │  state.db  │
  │  :8765   │ ◄──────────  │   jitter / retry)     │ ◄───────── │            │
  └──────────┘              └───────────┬───────────┘   reads    └────────────┘
       │                                │
       │ "Log in"                       │ execute
       ▼                                ▼
  ┌──────────────────┐        ┌─────────────────────┐        ┌──────────────┐
  │  Login flow      │        │   Naukri driver     │ ─────► │ screenshots/ │
  │  (headed, manual)│ ─────► │   (Playwright)      │        │  <run_id>.png│
  └──────────────────┘ state  └─────────────────────┘        └──────────────┘
                        .json
```

**The core design decision:** Task Scheduler does *not* own the 12/24/48h cadence. It
fires a dumb heartbeat every 15 minutes; the app decides whether a run is due by
comparing `now` against stored state. Consequences:

- Catch-up is free. Laptop off for two days → first tick after wake sees an overdue run
  and executes it. No special-case code.
- No drift, no duplicate-trigger bugs, no re-registering the OS task when the user
  changes the interval.
- The interval lives in the database, editable from the dashboard with no admin rights.

### Components

| Module | Responsibility |
|---|---|
| `cli.py` | Entry points: `login`, `run`, `tick`, `status`, `config`, `doctor`, `setup`, `install-task`, `dashboard` |
| `scheduler.py` | Due/overdue calculation, jitter, retry backoff, quiet hours. Pure functions over an injected clock — trivially unit-testable. |
| `driver/session.py` | Browser context lifecycle, `storage_state` load/save, login detection |
| `driver/profile.py` | The Naukri page interactions: upload resume, set headline, read back last-updated |
| `driver/selectors.py` | Every CSS/XPath selector in one file, each with a fallback chain |
| `store.py` | SQLite access. Plain `sqlite3`, no ORM. |
| `dashboard/` | FastAPI app + Jinja templates + hand-rolled SVG chart |
| `lock.py` | Single-instance OS file lock so a manual run and a tick can't collide |
| `runner.py` | One run, start to terminal state. Never raises. |
| `results.py` | `Status` / `ErrorKind` / `RunResult`, shaped for the SQLite schema |
| `scheduling.py` | `schtasks` registration and query parsing |
| `diagnostics.py` | The checks behind `doctor` and the setup checklist |

---

## 3. Run lifecycle

```
 due? ──► acquire lock ──► load session ──► open profile page
                                │                  │
                          no session /         challenge
                          expired cookie       (captcha/OTP)
                                │                  │
                                ▼                  ▼
                          NEEDS_LOGIN         NEEDS_LOGIN
                          (alert user)        (alert user)
                                                   │
       ┌───────────────────────────────────────────┘
       ▼
 upload resume ──► rotate headline ──► READ BACK last-updated ──► screenshot ──► SUCCESS
       │                  │                      │
       └── selector miss / upload rejected ──────┴──► FAILED (screenshot + DOM dump)
```

Every run ends in exactly one terminal state, always with a screenshot:

`SUCCESS` · `FAILED` · `NEEDS_LOGIN` · `SKIPPED_NOT_DUE` · `SKIPPED_LOCKED` · `DRY_RUN`

**Read-back verification matters.** A screenshot proves a page was reached; parsing the
profile's own "last updated" string proves the change registered. A run that uploads
without moving that timestamp is a *silent failure* — the worst possible outcome for
this product, since the user believes they're covered while their profile sinks.

**Verify by assertion, not by comparison.** `.mod-date-val` renders as the literal string
`Today` once updated (confirmed in Phase 0 — see §14). So the check is:

```
success  <=>  .mod-date-val == "Today"   (after reload)
```

Not `before != after`. The difference is not cosmetic. The field is day-granular, so a
second run on the same day reads `Today` → `Today` — a before/after comparison sees no
movement and reports `FAILED` on a run that worked perfectly. Retries, manual "Run now"
after a scheduled run, and catch-up all hit that path routinely. An absolute assertion is
correct in every one of those cases; a diff is correct in none of them.

---

## 4. Scheduling semantics

```
next_due_at = last_success_at + interval_hours + jitter
```

- **Jitter** — 0–45 min, seeded per run. A profile updating at exactly 09:00:00 every
  day is a machine signature. Rounding off that edge costs nothing.
- **Catch up, don't backfill.** Missed three cycles? Run *once*. Firing three updates in
  a row is pointless (only the latest timestamp counts) and looks automated.
- **Quiet hours** (optional, default 23:00–07:00) — defer rather than update at 3am.
- **Retry** — on `FAILED`, retry after 30 min, then 2 h, then give up until the next
  natural cycle. Max 3 attempts. `NEEDS_LOGIN` does *not* retry; it waits for the human.
- **Staleness alert** — dashboard shows a warning banner when the last success is older
  than `2 × interval`. The real failure mode to defend against is the tool quietly dying
  and the user not noticing for a month.

**What counts as an attempt.** Only `SUCCESS`, `FAILED` and `NEEDS_LOGIN` feed the
scheduler. `DRY_RUN` and `SKIPPED_*` are recorded for the history view but excluded from
scheduling state — a dry run that registered as a success would silence the schedule for
a full interval without Naukri ever being touched, and a `SKIPPED_LOCKED` counted as an
attempt would corrupt both the retry ladder and the staleness alert.

**`NEEDS_LOGIN` is not a failure.** It does not increment the failure count and does not
enter the retry ladder: the session cannot recover without a human, so retrying just
burns attempts. The normal cadence still applies, which means the tool heals itself on
the next cycle once the user signs in again.

**Jitter must be deterministic per cycle.** It is derived by hashing the anchor
timestamp, not drawn fresh each tick. A new random offset on every 15-minute heartbeat
would leave the due time perpetually a few minutes away, and the run would never fire.

---

## 5. Data model (SQLite)

```sql
CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,      -- ISO-8601, UTC
  finished_at   TEXT,
  status        TEXT NOT NULL,      -- SUCCESS | FAILED | NEEDS_LOGIN | SKIPPED_* | DRY_RUN
  trigger       TEXT NOT NULL,      -- schedule | manual | catchup | retry
  headline_used TEXT,
  profile_ts    TEXT,               -- "last updated" string read back from the page
  error_kind    TEXT,               -- SESSION_EXPIRED | SELECTOR_MISS | UPLOAD_REJECTED
                                    -- | NETWORK | CHALLENGE | UNKNOWN
  error_detail  TEXT,
  screenshot    TEXT                -- relative path
);

CREATE TABLE settings  (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE headlines (id INTEGER PRIMARY KEY, text TEXT NOT NULL,
                        position INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
```

`settings` keys: `interval_hours`, `resume_path`, `rotate_headline`, `quiet_start`,
`quiet_end`, `headed_mode`, `screenshot_retention`, `last_headline_index`.

Timestamps stored UTC, rendered local. The user's machine will change timezones (travel,
DST) and the schedule must not jump when it does.

---

## 6. Security model

The session file **is** account access. Anyone holding it can act as the user on Naukri
without a password. Design accordingly:

- Stored at `%LOCALAPPDATA%\NaukriAutopilot\state.json` — never in the repo, never in the
  project folder the user might zip and share. `.gitignore` covers `data/` regardless.
- The password is never seen by this tool. Login is a real headed browser window where
  the user types into Naukri's own page; we only call `context.storage_state()` afterward.
- Dashboard binds `127.0.0.1` only — never `0.0.0.0`. It is unauthenticated by design,
  which is only safe because it is not reachable off-box.
- No telemetry, no crash reporting, no update check. The privacy claim in the product
  spec is absolute, so there must be zero outbound calls to anything but naukri.com.
- **Optional hardening:** encrypt `state.json` at rest with Windows DPAPI
  (`CryptProtectData`, user-scoped). Cheap, and it stops casual file-copy theft.

### Rejected: credentials in `.env`

Considered and turned down. Recorded here so it doesn't get re-proposed.

- **This account signs in with Google OAuth — there is no Naukri password to store.** The
  credential is a Google session. Scripting that login means storing a *Google* password
  and, with 2FA on, the TOTP seed too — which cancels the 2FA.
- **Blast radius.** A Naukri cookie loses you a job profile. A Google password loses you
  email, Drive, and the password-reset link for every other account you own.
- **Google blocks it anyway.** Automated sign-in is the flow they have hardened most;
  CDP-driven browsers get "this browser or app may not be secure."
- **Worse location.** `.env` lives in the project folder — the thing that gets zipped,
  synced to OneDrive, and `git add -A`'d. `%LOCALAPPDATA%` is none of those.
- **It wouldn't even remove the session file.** Playwright still writes cookies after
  login, so you would hold the password *and* the session. Strictly more to lose.
- **It deletes a product promise.** "Your password is never seen, typed or stored" is a
  feature, not an implementation detail.

The friction this was meant to solve — having to log in again when the session lapses —
is addressed instead by: the persistent profile keeping the *Google* session too (so
re-auth is usually one click on the account chooser, no password), the 12–48h cadence
keeping the session warm on its own, and a Windows toast on `NEEDS_LOGIN` so you find out
without having to open the dashboard.

`.env` is still fine for non-secret dev overrides (dashboard port, log level). Never for
credentials.

**Honest risk note:** this automates your own account doing something you are allowed to
do by hand. That is not the same as being invisible. Naukri can change its UI,
rate-limit, or flag unusual patterns at any time, and the account risk is yours. Jitter,
human-ish pacing, a real browser profile, and a hard cap of one update per cycle are
mitigations — not guarantees.

---

## 7. Anti-detection posture

Modest and honest, not an arms race:

- **Headed, not headless.** The spec already requires the PC on and logged in, so a real
  browser costs nothing and avoids the headless fingerprint entirely. Hide it with
  `--window-position=-32000,-32000` so nothing pops up mid-workday. `headed_mode=false`
  stays available for debugging.
- **A real installed browser, not bundled Chromium.** Auto-detection order is Brave →
  Chrome → Edge → bundled. This is not paranoia: Google's sign-in refuses bundled
  Chromium outright with *"this browser or app may not be secure"*, which blocks OAuth
  login entirely. Brave is driven by `executable_path` (it has no Playwright channel).
- **`--disable-blink-features=AutomationControlled`** plus an init script clearing
  `navigator.webdriver`. Removes the one obvious tell; not a cloak.
- **Persistent user-data-dir**, not a fresh context per run — stable fingerprint, and
  cookies survive naturally. **One profile directory per browser build**
  (`profile-brave`, `profile-msedge`, …) — Chromium builds cannot share a
  user-data-dir, and the failure mode is a misleading "already in use by another
  instance" error.
- One action per cycle. No polling loops, no page crawling, no parallelism.
- Realistic viewport, real user agent (whatever the bundled Chromium reports — don't spoof).

---

## 8. Failure modes

| Failure | Detection | Response |
|---|---|---|
| Session expired | Redirected to login, or profile selectors absent | `NEEDS_LOGIN`, dashboard banner, no retry |
| Captcha / OTP challenge | Challenge markers in DOM | `NEEDS_LOGIN` + screenshot so the user sees why |
| Naukri changed its UI | Every selector in a fallback chain misses | `SELECTOR_MISS` + full DOM dump to `data/debug/` |
| Upload rejected | Error toast, or read-back timestamp unmoved | `UPLOAD_REJECTED`, retry ladder |
| Resume file moved or deleted | Pre-flight `Path.exists()` | Fail fast before opening a browser |
| Machine asleep at due time | Next tick sees overdue | Catch-up run |
| Manual run races a tick | File lock | `SKIPPED_LOCKED` |
| Silent no-op "success" | Read-back verification | `FAILED`, not `SUCCESS` |

`SELECTOR_MISS` is the expected long-term maintenance burden — Naukri will redesign
eventually. Concentrating every selector in `selectors.py` with fallback chains makes
that a one-file fix instead of an archaeology expedition.

---

## 9. Dashboard

Single local page at `http://127.0.0.1:8765`:

- **Status** — next run time, last result, staleness / re-login banners
- **Activity chart** — GitHub-style contribution grid, one cell per day, coloured by
  status, with month labels so the window is readable. Hand-rolled inline SVG; no chart
  library, no CDN (offline must work, and outbound requests would violate §6). A test
  asserts the rendered page contains no external reference at all.
- **Streak** — consecutive fresh days. Yesterday still counts: with a 24h interval plus
  jitter, today's run may not have fired yet, and resetting at midnight would be both
  wrong and dispiriting.
- **Run history** — table with status, trigger, error, screenshot thumbnail
- **Settings** — interval, resume path, headline variants, quiet hours
- **Setup checklist** — first-run wizard, each step self-verifying (§11)
- **Run now** / **Dry run** buttons — a run takes ~30s of browser time, far too long to
  hold an HTTP request open, so the work goes on a thread and the page polls
  `/api/status` until it finishes.

Screenshots are served **by run id, never by path**: the id is looked up and the resolved
path re-checked to be inside the screenshot directory. Taking a filename from the URL
would be a path-traversal hole straight into the user's filesystem.

---

## 10. Tech choices

| Choice | Rationale |
|---|---|
| Python 3.9+ | Spec floor. Means `from __future__ import annotations` everywhere; no `match`, no PEP 604 unions at runtime. |
| Playwright (sync API) | Bundles its own Chromium — no system browser dependency. Sync API because there is no concurrency to exploit and it debugs far more easily. |
| SQLite via stdlib `sqlite3` | Single file, zero setup, survives crashes. An ORM would be dead weight at this size. |
| FastAPI + uvicorn + Jinja2 | Local-only dashboard; typed routes for free. Flask would be equally fine. |
| No chart/JS libraries | Offline-capable, and keeps the no-outbound-calls promise literally true. |
| Windows Task Scheduler | Native, survives reboot, no background process to babysit. Registered via `schtasks` at user scope — no admin prompt, no stored password. |
| `pythonw.exe` for the tick | `python.exe` would flash a console window 96 times a day, which is the fastest way to get a background tool uninstalled. Costs a console to log to, hence `data/tick.log` as a last resort. |

---

## 11. Setup flow (target: 10 minutes)

Each step verifies itself and refuses to tick green on the user's say-so:

1. **Python check** — `py -3 --version`; link to python.org if missing
2. **Install** — `py -3 -m venv .venv`, then `.venv\Scripts\pip install -e .`, then
   `playwright install chromium`
3. **Launch dashboard** — `naukri-autopilot dashboard`, opens the browser
4. **Point at resume** — file picker; validates PDF, size, readability
5. **Log in** — button opens a headed Naukri window; user logs in manually; app waits for
   the logged-in state, saves `storage_state`, closes
6. **Headline variants** — optional; 2–4 phrasings *(character limit read from the live
   page, not hardcoded — see §14)*
7. **Pick interval** — 12 / 24 / 48 h
8. **Dry run** — full flow, no writes. Proves selectors resolve before the user trusts it.
9. **Register schedule** — `naukri-autopilot install-task` (user scope, no admin prompt)
10. **First real run** — user watches it succeed and sees the screenshot

Plus a copy-pasteable AI-assist prompt (per the product spec) for users who would rather
have an assistant walk them through it.

---

## 12. Build order

### Running Phase 0 today

```
.venv\Scripts\python scripts\phase0_recon.py login
.venv\Scripts\python scripts\phase0_recon.py probe
.venv\Scripts\python scripts\phase0_recon.py measure --resume C:\path\to\resume.pdf
```

`login` opens a real window and waits while you sign in — nothing types your password.
`probe` writes a dated report, screenshot and full HTML to `data/debug/`. `measure` is
the experiment for §14.1 and **writes to your live profile**; everything before it is
read-only.

`measure` prints one of three verdicts — YES (design holds), NO (headline rotation is
promoted to primary), or INCONCLUSIVE (no readable timestamp, so §14.3 must be answered
first). It also drops a `*-verdict.json` next to the screenshots as the record.

The recon script is meant to be *edited*. If the upload control turns out to sit behind
a click, add the click — that discovery is the deliverable, not the script.



| Phase | Deliverable | Done when |
|---|---|---|
| **0. Recon** ✅ | `scripts/phase0_recon.py` — login, probe, measure | **Done 2026-09-14.** Session reuse confirmed (180-day cookies); selectors captured; §14.1 answered YES |
| **1. Core driver** ✅ | `driver/` + `selectors.py`, dry-run mode, screenshots, read-back verification | **Done 2026-09-14.** `run --dry-run` passes against a live account; 20 tests green |
| **2. State + scheduler** ✅ | SQLite, `scheduler.py`, `tick`, file lock, `status`, `config` | **Done 2026-09-14.** 76 tests green, covering due / overdue / catch-up / jitter / quiet-hours / retry against a fake clock |
| **3. Dashboard** ✅ | Status, history, chart, settings, checklist | **Done 2026-09-14.** 133 tests green; page verified to make zero outbound requests |
| **4. Setup & scheduling** ✅ | `install-task`, `doctor`, `setup` checklist | **Done 2026-09-14.** 103 tests green; schtasks args and query parsing covered without touching the real scheduler |
| **5. Hardening** | Retry ladder, staleness alerts, Windows toast on `NEEDS_LOGIN`, screenshot retention, DPAPI, structured logs | Survives: revoked session, offline, renamed resume, Naukri DOM change |

Phase 0 is the one that can invalidate the design. Do it before writing anything
permanent — if identical re-uploads don't bump the timestamp, headline rotation stops
being a backstop and becomes the primary mechanism, which changes the settings UI, the
validation rules, and the minimum number of variants a user must supply.

---

## 13. Project layout

```
naukari_autopilot/
├─ src/naukri_autopilot/
│  ├─ cli.py  scheduler.py  store.py  lock.py  config.py
│  ├─ driver/      session.py  profile.py  selectors.py
│  └─ dashboard/   app.py  templates/  static/
├─ tests/          test_scheduler.py  test_store.py
├─ data/           state.db  screenshots/  debug/     (gitignored)
├─ pyproject.toml  README.md  CLAUDE.md
```

Session state lives in `%LOCALAPPDATA%`, not `data/` — see §6.

---

## 14. Open questions

1. ~~Does a byte-identical resume re-upload move the timestamp?~~ **Answered — YES.**
   Measured 2026-09-14: `22Jul , 2026` → `Today`, resume date `Feb 23` → `Sep 14`, with a
   byte-identical file. Re-upload is the primary lever; headline rotation stays an
   optional backstop and can default **off**.
2. Naukri's headline character limit — **still open.** The headline renders as a plain
   `div`, not an editable field, so the limit only becomes visible once the edit modal is
   open. Phase 1 must drive that modal.
3. ~~Is there a visible "profile last updated" string?~~ **Answered — yes, two of them.**
   See below.
4. Is there a rate limit or cooldown on resume uploads?
5. Multiple resume files (one per target role) — worth rotating those too, or scope creep?

### Phase 0 findings (2026-09-14, live profile)

**Naukri tracks two independent dates, and they do not agree.**

| What | Selector | Observed |
|---|---|---|
| Profile last updated | `.mod-date-val` | `22Jul , 2026` |
| Resume uploaded on | `.updateOn` | `Uploaded on Feb 23, 2026` |

Five months apart. Recruiter search sorts on the **first**; the resume file carries the
second. A re-upload moves **both** — measured, not assumed.

**The rendered value is relative, not a date.** After updating, `.mod-date-val` reads the
literal string `Today` — not `14Sep , 2026`. Older profiles render an absolute date
(`22Jul , 2026`). Presumably `Yesterday` exists too; unconfirmed, and the driver should
treat any non-`Today` value as "needs updating" rather than enumerating the vocabulary.

This is why verification asserts `== "Today"` instead of diffing before against after
(§3). Day granularity means a same-day second run shows no diff at all, and a
comparison-based check would call a perfectly good run `FAILED`.

**Upload controls, and a trap:**

| Selector | What it is |
|---|---|
| `#attachCV` | Resume upload |
| `#fileUpload` (`accept="image/*"`) | **Profile photo** |

They sit next to each other in the DOM. Addressing file inputs by index — which the
first draft of the recon script did — can put a PDF into the photo field. Everything
now addresses them by id, and the driver must do the same.

**Headline** lives in a `div` with an adjacent edit icon, so changing it means driving a
modal, not typing into an inline field. That is more work than assumed and lands in
Phase 1.

**Browser:** bundled Chromium is unusable for OAuth sign-in — Google refuses it outright.
Brave works. See §7.

---

*Note: this folder is spelled `naukari_autopilot`; the site is `naukri.com`. Worth
renaming before `git init` if you'd rather not carry the typo forever.*
