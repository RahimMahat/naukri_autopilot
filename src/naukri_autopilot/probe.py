"""Selector diagnostics, behind `naukri-autopilot inspect`.

When a run fails with SELECTOR_MISS, Naukri has changed its markup. This reports
which chains in selectors.py still resolve, which have broken, and what the page
offers as replacements - then saves the DOM so the repair can be made offline.

It drives the real `driver` modules rather than keeping its own copy of the
browser plumbing, so there is only ever one definition of how a session is
opened. Read-only: it never uploads, clicks or types.
"""

from __future__ import annotations

from datetime import datetime

from . import config
from .driver import profile, selectors as sel, session as sess

# Every chain worth checking, in the order a run would need them.
CHAINS = [
    ("profile last updated", sel.PROFILE_UPDATED, True),
    ("resume upload input", sel.RESUME_INPUT, True),
    ("resume uploaded date", sel.RESUME_UPLOADED, False),
    ("resume filename", sel.RESUME_NAME, False),
    ("headline text", sel.HEADLINE_TEXT, False),
    ("logged-in marker", sel.LOGGED_IN_MARKER, True),
]

# Candidate replacements offered when a chain breaks. Deliberately broad - the
# point is to show what the page actually contains, not to guess correctly.
_CANDIDATES_JS = """
() => {
  const out = {dates: [], files: [], headlines: []};
  const dateRe = /(last\\s*updated|updated\\s*on|uploaded\\s*on|profile\\s*last)/i;
  for (const el of document.querySelectorAll('div,span,p,li,td')) {
    const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (t && t.length < 120 && dateRe.test(t)) {
      const cls = typeof el.className === 'string' ? el.className : '';
      const hit = {text: t, tag: el.tagName.toLowerCase(), cls: cls.slice(0, 90),
                   id: el.id || null};
      if (!out.dates.some(d => d.text === t)) out.dates.push(hit);
    }
    if (out.dates.length >= 10) break;
  }
  for (const el of document.querySelectorAll('input[type=file]')) {
    const r = el.getBoundingClientRect();
    out.files.push({id: el.id || null, name: el.getAttribute('name'),
                    accept: el.getAttribute('accept'),
                    hidden: r.width === 0 || r.height === 0,
                    near: (el.closest('div,section,form') || el)
                          .textContent.trim().slice(0, 120)});
  }
  for (const el of document.querySelectorAll(
        '[class*=headline i],[id*=headline i],textarea,[contenteditable=true]')) {
    const cls = typeof el.className === 'string' ? el.className : '';
    out.headlines.push({tag: el.tagName.toLowerCase(), id: el.id || null,
                        cls: cls.slice(0, 90),
                        maxlength: el.getAttribute('maxlength'),
                        value: ((el.value != null ? el.value : el.textContent) || '')
                               .trim().slice(0, 140)});
    if (out.headlines.length >= 8) break;
  }
  return out;
}
"""


def _which(page, chain: "list[str]") -> "str | None":
    """The first selector in the chain that matches, or None."""
    for selector in chain:
        try:
            if page.query_selector(selector) is not None:
                return selector
        except Exception:
            continue
    return None


def inspect(channel: str = "auto", headed: bool = True) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    config.ensure_dirs()
    lines = ["# Selector inspection - {}".format(stamp), ""]

    try:
        with sess.Session(channel=channel, offscreen=not headed) as s:
            page = s.page()
            profile.open_profile(page)
            lines.append("Browser: {}".format(s.browser_used))
            lines.append("URL: {}".format(page.url))

            challenge = sess.detect_challenge(page)
            if challenge:
                lines.append("! Challenge on page: {!r}".format(challenge))

            if not sess.looks_logged_in(page):
                print("Not signed in. Run `naukri-autopilot login` first.")
                return 1

            lines += ["", "## Selector chains", ""]
            broken = []
            for name, chain, required in CHAINS:
                match = _which(page, chain)
                if match:
                    rank = chain.index(match)
                    note = "" if rank == 0 else "  (FALLBACK #{} - primary is stale)".format(rank)
                    lines.append("  ok    {:<22} {}{}".format(name, match, note))
                else:
                    broken.append(name)
                    lines.append("  MISS  {:<22} tried: {}".format(name, ", ".join(chain)))
                    if required:
                        lines[-1] += "   <-- REQUIRED"

            cand = page.evaluate(_CANDIDATES_JS)
            lines += ["", "## What the page actually offers", "", "### Date-ish text"]
            lines += ["  {!r}  <{} class={!r} id={!r}>".format(
                d["text"], d["tag"], d["cls"], d["id"]) for d in cand["dates"]] or ["  none"]

            lines += ["", "### File inputs"]
            lines += ["  id={id!r} accept={accept!r} hidden={hidden}\n    near: {near!r}".format(**f)
                      for f in cand["files"]] or ["  none"]

            lines += ["", "### Headline-ish fields"]
            lines += ["  <{tag}> id={id!r} maxlength={maxlength}\n    {value!r}".format(**h)
                      for h in cand["headlines"]] or ["  none"]

            html = config.DEBUG_DIR / "{}-inspect.html".format(stamp)
            html.write_text(page.content(), encoding="utf-8")
            png = config.DEBUG_DIR / "{}-inspect.png".format(stamp)
            page.screenshot(path=str(png), full_page=True)
            lines += ["", "DOM:        {}".format(html), "Screenshot: {}".format(png)]

    except sess.BrowserUnavailable as exc:
        print(str(exc))
        return 1

    report = "\n".join(lines)
    out = config.DEBUG_DIR / "{}-inspect.md".format(stamp)
    out.write_text(report, encoding="utf-8")
    print(report)
    print("\nSaved to {}".format(out))

    if broken:
        print(
            "\n{} chain(s) broken. Fix driver/selectors.py using the candidates above,\n"
            "update tests/fixtures/ to match the new structure, then re-run the tests."
            .format(len(broken))
        )
        return 1
    print("\nAll selector chains resolve.")
    return 0
