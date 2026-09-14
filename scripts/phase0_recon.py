"""Phase 0 recon: answer the questions the design rests on, before writing the design.

This is a throwaway. It is not imported by the package and it is allowed to be
scrappy. What it must do is produce *evidence*, not impressions.

    python scripts/phase0_recon.py login
    python scripts/phase0_recon.py probe
    python scripts/phase0_recon.py measure --resume "C:\\path\\to\\resume.pdf"

  login    Opens a real Chromium window. You log into Naukri yourself. Nothing
           types your password but you. On success the session is persisted so
           every later command reuses it.

  probe    Reuses the session, opens the profile page, and dumps everything the
           real driver will need to know: candidate "last updated" strings, every
           file input, headline-ish fields, and the full HTML. Writes a report to
           data/debug/.

  measure  THE experiment (README section 14.1). Reads the last-updated value,
           re-uploads a byte-identical PDF, reloads, reads it again, and prints a
           verdict. If the answer is NO, headline rotation is promoted from
           backstop to primary mechanism and parts of the design change.

Nothing here writes to the profile except `measure`, which is the entire point of
`measure`. Run it against your own account, knowingly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Run before the package is installed: src/ onto the path, then reuse the real
# config so the session saved here is the session Phase 1 picks up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from naukri_autopilot import config  # noqa: E402

def _sync_playwright():
    """Imported lazily so `--help` works before anything is installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is not installed.\n"
            "    py -3 -m venv .venv\n"
            "    .venv\\Scripts\\pip install playwright\n"
            "    .venv\\Scripts\\playwright install chromium"
        )
    return sync_playwright


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #

LOGIN_TIMEOUT_S = 300
UPLOAD_SETTLE_S = 8


class Session:
    """Persistent-profile browser. Always headed during recon - you need to see it.

    Prefers a real installed browser (Chrome, then Edge) over Playwright's bundled
    Chromium: Google's sign-in checks are markedly less hostile to signed builds.
    """

    def __init__(self, offscreen: bool = False, channel: "str | None" = "auto"):
        self._offscreen = offscreen
        self._channel = channel
        self._pw = None
        self.ctx = None
        self.channel_used = None

    def _launch(self, name):
        """Launch one named browser, or raise. Each build gets its own profile."""
        kwargs = {}
        if name == "brave":
            brave = config.find_brave()
            if brave is None:
                raise RuntimeError("Brave is not installed")
            kwargs["executable_path"] = str(brave)
        elif name != "chromium":
            kwargs["channel"] = name

        args = list(config.STEALTH_ARGS)
        if self._offscreen:
            args += config.OFFSCREEN_ARGS
        return self._pw.chromium.launch_persistent_context(
            user_data_dir=str(config.profile_dir(name)),
            headless=False,
            viewport=config.VIEWPORT,
            args=args,
            **kwargs
        )

    def __enter__(self):
        config.ensure_dirs()
        self._pw = _sync_playwright()().start()

        candidates = (
            config.CHANNEL_PREFERENCE if self._channel == "auto" else [self._channel]
        )
        errors = []
        for name in candidates:
            try:
                self.ctx = self._launch(name)
                self.channel_used = name
                break
            except Exception as exc:
                errors.append("{}: {}".format(name, str(exc).splitlines()[0]))
        if self.ctx is None:
            self._pw.stop()
            sys.exit(
                "Could not launch any browser:\n  "
                + "\n  ".join(errors)
                + "\n\nIf a profile is 'already in use', close leftover windows or run:\n"
                "  taskkill /F /IM chrome.exe /FI \"WINDOWTITLE eq about:blank\""
            )

        # Second line of defence: some builds still expose the flag to page JS.
        self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        print("Browser: {}".format(self.channel_used))
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def page(self):
        return self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def save_storage_state(self) -> None:
        """Portable cookie snapshot alongside the persistent profile.

        The persistent profile is what actually keeps us logged in; this is a
        belt-and-braces copy the driver can fall back to if the profile
        directory is ever corrupted or moved.
        """
        self.ctx.storage_state(path=str(config.STORAGE_STATE))


# --------------------------------------------------------------------------- #
# page inspection
# --------------------------------------------------------------------------- #

# Builds a best-effort unique selector for an element, preferring stable hooks
# (id, data-* attributes) over generated class names.
_SUGGEST_JS = """
(el) => {
  const stable = (e) => {
    if (e.id && !/^[0-9]/.test(e.id) && !/\\d{4,}/.test(e.id)) return '#' + CSS.escape(e.id);
    for (const a of ['data-testid','data-test','data-qa','name','aria-label']) {
      const v = e.getAttribute && e.getAttribute(a);
      if (v) return e.tagName.toLowerCase() + '[' + a + '="' + v + '"]';
    }
    return null;
  };
  const own = stable(el);
  if (own) return own;
  const parts = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && parts.length < 5) {
    const anchor = stable(cur);
    if (anchor) { parts.unshift(anchor); break; }
    let part = cur.tagName.toLowerCase();
    const cls = (cur.className && typeof cur.className === 'string')
      ? cur.className.trim().split(/\\s+/)
          .filter(c => c.length < 30 && !/\\d{3,}|^css-|^sc-/.test(c))
          .slice(0, 2)
      : [];
    if (cls.length) part += '.' + cls.map(c => CSS.escape(c)).join('.');
    const sibs = cur.parentElement
      ? Array.from(cur.parentElement.children).filter(s => s.tagName === cur.tagName)
      : [];
    if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
    parts.unshift(part);
    cur = cur.parentElement;
  }
  return parts.join(' > ');
}
"""

# Discovered by the first probe run against a live profile (2026-09-14).
# Naukri tracks TWO independent dates and they do not move together:
#   .mod-date-val  "22Jul , 2026"          <- what recruiter search sorts on
#   .updateOn      "Uploaded on Feb 23, 2026" <- resume file date only
# The whole product depends on the first one. Day granularity only.
SEL_PROFILE_UPDATED = ".mod-date-val"
SEL_PROFILE_UPDATED_BLOCK = ".mod-date"
SEL_RESUME_UPLOADED = ".updateOn"
SEL_RESUME_NAME = ".resume-name-inline .exten"

# #attachCV is the resume. #fileUpload is the PROFILE PHOTO - never send a PDF
# there. Addressing by id rather than index is what keeps those apart.
SEL_RESUME_INPUT = "#attachCV"
SEL_PHOTO_INPUT = "#fileUpload"

_READ_STATE_JS = """
() => {
  const txt = (sel) => {
    const e = document.querySelector(sel);
    return e ? (e.textContent || '').trim().replace(/\\s+/g, ' ') : null;
  };
  const name = document.querySelector('%s');
  const state = {
    profile_updated: txt('%s'),
    profile_updated_block: txt('%s'),
    resume_uploaded: txt('%s'),
    resume_name: name ? (name.getAttribute('title') || name.textContent.trim()) : null,
  };
  // Fallback for selector drift: any short text mentioning updated/uploaded,
  // including non-leaf nodes (the date usually sits in a child span).
  if (!state.profile_updated) {
    const re = /(last\\s*updated|updated\\s*on|uploaded\\s*on|profile\\s*last)/i;
    const seen = [];
    for (const el of document.querySelectorAll('div,span,p,li,td')) {
      const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
      if (t && t.length < 120 && re.test(t) && !seen.includes(t)) seen.push(t);
      if (seen.length >= 10) break;
    }
    state.fallback_matches = seen;
  }
  return state;
}
""" % (SEL_RESUME_NAME, SEL_PROFILE_UPDATED, SEL_PROFILE_UPDATED_BLOCK, SEL_RESUME_UPLOADED)

_FILE_INPUTS_JS = """
() => Array.from(document.querySelectorAll('input[type=file]')).map((el, i) => {
  const r = el.getBoundingClientRect();
  return {
    index: i,
    id: el.id || null,
    name: el.getAttribute('name'),
    accept: el.getAttribute('accept'),
    multiple: el.multiple,
    hidden: r.width === 0 || r.height === 0,
    nearby: (el.closest('div,section,form') || el).textContent.trim().slice(0, 160),
  };
})
"""

_HEADLINE_JS = """
() => {
  const re = /headline/i;
  const out = [];
  for (const el of document.querySelectorAll(
      '[class*=headline i],[id*=headline i],textarea,[contenteditable=true]')) {
    const t = (el.textContent || '').trim();
    const around = (el.closest('div,section') || el).textContent.trim().slice(0, 160);
    if (!re.test(el.className + ' ' + el.id + ' ' + around)) continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 120),
      maxlength: el.getAttribute('maxlength'),
      value: (el.value != null ? el.value : t).slice(0, 200),
      editable: el.getAttribute('contenteditable') === 'true'
                || ['TEXTAREA','INPUT'].includes(el.tagName),
    });
    if (out.length >= 10) break;
  }
  return out;
}
"""

_CHALLENGE_HINTS = ("captcha", "verify it's you", "verify its you", "otp", "unusual activity")


def looks_logged_in(page) -> bool:
    """Heuristic, deliberately loose - recon confirms it, the driver hardens it."""
    url = page.url.lower()
    if "login" in url or "nlogin" in url:
        return False
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return "logout" in body or "my naukri" in body or "/mnjuser/" in url


def detect_challenge(page) -> "str | None":
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return None
    for hint in _CHALLENGE_HINTS:
        if hint in body:
            return hint
    return None


def read_state(page) -> dict:
    """Both dates plus the resume filename. The design hangs on profile_updated."""
    try:
        return page.evaluate(_READ_STATE_JS)
    except Exception as exc:
        print("  ! could not read profile state: {}".format(exc))
        return {}


def show_state(label: str, st: dict) -> None:
    print("{}:".format(label))
    print("  profile last updated : {}".format(st.get("profile_updated") or "NOT FOUND"))
    print("  resume uploaded on   : {}".format(st.get("resume_uploaded") or "NOT FOUND"))
    print("  resume file          : {}".format(st.get("resume_name") or "NOT FOUND"))
    if st.get("fallback_matches"):
        print("  ! primary selector missed; fallback saw:")
        for t in st["fallback_matches"][:5]:
            print("      {!r}".format(t))


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def shot(page, name: str) -> Path:
    path = config.DEBUG_DIR / "{}-{}.png".format(stamp(), name)
    page.screenshot(path=str(path), full_page=True)
    return path


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_login(args) -> int:
    print("Opening a browser. Log into Naukri in that window - nothing is typed for you.")
    print("Waiting up to {} minutes.\n".format(LOGIN_TIMEOUT_S // 60))
    with Session(channel=args.browser) as s:
        page = s.page()
        page.goto(config.LOGIN_URL, wait_until="domcontentloaded")

        # "Continue with Google" can finish in a popup or a second tab, so poll
        # every page in the context rather than only the one we opened.
        deadline = time.time() + LOGIN_TIMEOUT_S
        landed = None
        while time.time() < deadline and landed is None:
            for candidate in list(s.ctx.pages):
                try:
                    if looks_logged_in(candidate):
                        landed = candidate
                        break
                except Exception:
                    continue  # page navigating or closing mid-check
            if landed is None:
                time.sleep(2)

        if landed is None:
            shot(page, "login-timeout")
            print("\nTimed out - still not logged in. Screenshot saved to data/debug/.")
            print(
                "\nIf Google said 'This browser or app may not be secure':\n"
                "  1. Retry with a different browser:  --browser msedge\n"
                "  2. If it still blocks, set a Naukri-native password via Forgot\n"
                "     Password on naukri.com, then sign in with email + password.\n"
                "     That path never touches Google and is what the tool wants anyway."
            )
            return 1
        page = landed

        s.save_storage_state()
        print("Logged in. Session saved:")
        print("  profile  {}".format(config.profile_dir(s.channel_used)))
        print("  cookies  {}".format(config.STORAGE_STATE))
        print("\nThat file is password-equivalent. Do not copy it anywhere.")
        print("Next:  python scripts/phase0_recon.py probe")
    return 0


def cmd_probe(args) -> int:
    with Session(channel=args.browser) as s:
        page = s.page()
        page.goto(config.PROFILE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if not looks_logged_in(page):
            shot(page, "probe-not-logged-in")
            print("Session is not valid - run `login` first.")
            return 1

        challenge = detect_challenge(page)
        if challenge:
            shot(page, "probe-challenge")
            print("! Challenge detected on the profile page: {!r}".format(challenge))

        state = read_state(page)
        files = page.evaluate(_FILE_INPUTS_JS)
        headlines = page.evaluate(_HEADLINE_JS)

        ts = stamp()
        png = shot(page, "probe")
        html_path = config.DEBUG_DIR / "{}-probe.html".format(ts)
        html_path.write_text(page.content(), encoding="utf-8")

        report = [
            "# Phase 0 probe - {}".format(ts),
            "",
            "URL: {}".format(page.url),
            "Challenge: {}".format(challenge or "none"),
            "Screenshot: {}".format(png.name),
            "HTML: {}".format(html_path.name),
            "",
            "## Profile state",
            "",
            "- profile last updated : {!r}  ({})".format(
                state.get("profile_updated"), SEL_PROFILE_UPDATED),
            "- resume uploaded on   : {!r}  ({})".format(
                state.get("resume_uploaded"), SEL_RESUME_UPLOADED),
            "- resume file          : {!r}".format(state.get("resume_name")),
            "",
        ]
        if state.get("fallback_matches"):
            report += ["- PRIMARY SELECTOR MISSED. Fallback matches:", ""]
            report += ["  - {!r}".format(t) for t in state["fallback_matches"]]
            report += [""]
        report += ["", "## File inputs ({})".format(len(files)), ""]
        report += [
            "- [{index}] accept={accept!r} hidden={hidden} name={name!r}\n  near: {nearby!r}".format(**f)
            for f in files
        ] or ["- none (upload control may be behind a click)"]
        report += ["", "## Headline fields ({})".format(len(headlines)), ""]
        report += [
            "- <{tag}> maxlength={maxlength} editable={editable}\n  value: {value!r}".format(**h)
            for h in headlines
        ] or ["- none (headline edit is probably behind a modal)"]

        report_path = config.DEBUG_DIR / "{}-probe.md".format(ts)
        report_path.write_text("\n".join(report), encoding="utf-8")

        print("\n".join(report))
        print("\nWritten to {}".format(report_path))
        if args.keep_open:
            input("\nBrowser stays open - inspect freely, then press Enter to close.")
    return 0


def cmd_measure(args) -> int:
    resume = Path(args.resume).expanduser().resolve()
    if not resume.is_file():
        print("Resume not found: {}".format(resume))
        return 1
    if resume.suffix.lower() != ".pdf":
        print("Expected a .pdf, got {}".format(resume.suffix))
        return 1

    print("THE EXPERIMENT: does a byte-identical re-upload move the timestamp?")
    print("This writes to your real profile. Ctrl-C now if that is not what you want.\n")

    with Session(channel=args.browser) as s:
        page = s.page()
        page.goto(config.PROFILE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if not looks_logged_in(page):
            print("Session is not valid - run `login` first.")
            return 1

        before = read_state(page)
        show_state("BEFORE", before)
        shot(page, "measure-before")

        if not before.get("profile_updated"):
            print(
                "\nNo 'profile last updated' value could be read, so there is nothing to\n"
                "compare against. Re-run `probe` and fix the selector first - a verdict\n"
                "without a baseline is worthless."
            )
            return 1

        # By id, never by index: #fileUpload next door is the profile PHOTO field,
        # and a PDF posted there would be a real mess to undo.
        handle = page.query_selector(SEL_RESUME_INPUT)
        if handle is None:
            shot(page, "measure-no-input")
            print(
                "\n{} not on the page - Naukri changed the upload control.\n"
                "Read the probe HTML and update SEL_RESUME_INPUT.".format(SEL_RESUME_INPUT)
            )
            return 1

        handle.set_input_files(str(resume))
        print("Uploaded {} - waiting {}s to settle.".format(resume.name, UPLOAD_SETTLE_S))
        page.wait_for_timeout(UPLOAD_SETTLE_S * 1000)
        shot(page, "measure-uploaded")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        after = read_state(page)
        shot(page, "measure-after")
        print()
        show_state("AFTER", after)

        moved = before.get("profile_updated") != after.get("profile_updated")
        resume_moved = before.get("resume_uploaded") != after.get("resume_uploaded")

        verdict = {
            "when": datetime.now().isoformat(timespec="seconds"),
            "resume": resume.name,
            "resume_bytes": resume.stat().st_size,
            "before": before,
            "after": after,
            "profile_timestamp_moved": moved,
            "resume_timestamp_moved": resume_moved,
        }
        out = config.DEBUG_DIR / "{}-verdict.json".format(stamp())
        out.write_text(json.dumps(verdict, indent=2), encoding="utf-8")

        print("\n" + "=" * 62)
        if moved:
            print("YES - an identical re-upload moved 'profile last updated'.")
            print("  {!r} -> {!r}".format(
                before.get("profile_updated"), after.get("profile_updated")))
            print("\nThe design holds. Resume re-upload is the primary lever and")
            print("headline rotation stays an optional backstop.")
        else:
            print("NO - 'profile last updated' did NOT move: {!r}".format(
                before.get("profile_updated")))
            print("  (resume upload date moved: {})".format(resume_moved))
            print("\nHeadline rotation is promoted to PRIMARY. That means:")
            print("  - variants become mandatory in setup, minimum 2")
            print("  - the driver must edit the headline, which sits behind a modal")
            print("  - re-run with a byte-DIFFERENT pdf to see if content is what counts")
        print("=" * 62)
        print("Note: the date is day-granular, so a profile already updated today")
        print("cannot show movement. Baseline here was {!r}.".format(
            before.get("profile_updated")))
        print("Verdict written to {}".format(out))

        if args.keep_open:
            input("\nPress Enter to close the browser.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_browser(sp):
        sp.add_argument(
            "--browser",
            default="auto",
            choices=["auto", "brave", "chrome", "msedge", "chromium"],
            help="auto tries brave, chrome, msedge, then bundled chromium",
        )
        return sp

    add_browser(sub.add_parser("login", help="manual login, persist the session"))

    probe = add_browser(sub.add_parser("probe", help="dump selectors and page evidence"))
    probe.add_argument("--keep-open", action="store_true")

    m = add_browser(sub.add_parser("measure", help="does an identical re-upload bump the timestamp?"))
    m.add_argument("--resume", required=True, help="path to your resume PDF")
    m.add_argument("--keep-open", action="store_true")

    args = p.parse_args()
    return {"login": cmd_login, "probe": cmd_probe, "measure": cmd_measure}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
