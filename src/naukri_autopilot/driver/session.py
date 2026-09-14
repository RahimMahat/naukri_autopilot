"""Browser lifecycle and session persistence.

Two hard-won constraints from Phase 0, both load-bearing:

1. Bundled Chromium cannot complete a Google OAuth sign-in - Google refuses it
   with "this browser or app may not be secure". A real installed browser can.
   Hence the channel preference and the Brave executable_path path.
2. Chromium builds cannot share a user-data-dir. Doing so fails with a
   misleading "already in use by another instance", so each build gets its own.
"""

from __future__ import annotations

import time

from .. import config
from . import selectors as sel


class BrowserUnavailable(RuntimeError):
    pass


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment problem
        raise BrowserUnavailable(
            "Playwright is not installed. Run:  pip install -e .  "
            "then:  playwright install chromium"
        ) from exc
    return sync_playwright


class Session:
    """A persistent-profile browser context.

    Headed by default. Scheduled runs pass offscreen=True, which keeps a real
    browser (no headless fingerprint) but parks the window off the desktop.
    """

    def __init__(self, channel: str = "auto", offscreen: bool = False):
        self._channel = channel
        self._offscreen = offscreen
        self._pw = None
        self.ctx = None
        self.browser_used = None

    # -- lifecycle -------------------------------------------------------- #

    def _launch(self, name: str):
        kwargs = {}
        if name == "brave":
            brave = config.find_brave()
            if brave is None:
                raise BrowserUnavailable("Brave is not installed")
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

    def __enter__(self) -> "Session":
        config.ensure_dirs()
        self._pw = _import_playwright()().start()

        candidates = (
            config.CHANNEL_PREFERENCE if self._channel == "auto" else [self._channel]
        )
        errors = []
        for name in candidates:
            try:
                self.ctx = self._launch(name)
                self.browser_used = name
                break
            except Exception as exc:
                errors.append("{}: {}".format(name, str(exc).splitlines()[0]))

        if self.ctx is None:
            self._pw.stop()
            raise BrowserUnavailable(
                "no browser could be launched:\n  " + "\n  ".join(errors)
            )

        self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx is not None:
                self.ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # -- pages ------------------------------------------------------------ #

    def page(self):
        return self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def save_storage_state(self) -> None:
        self.ctx.storage_state(path=str(config.STORAGE_STATE))

    # -- login ------------------------------------------------------------ #

    def wait_for_login(self, timeout_s: int = 300):
        """Block while the user signs in by hand. Returns the logged-in page.

        Polls every page in the context, not just the one we opened: "Continue
        with Google" can land in a popup or a second tab.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for page in list(self.ctx.pages):
                try:
                    if looks_logged_in(page):
                        return page
                except Exception:
                    continue  # navigating or closing mid-check
            time.sleep(2)
        return None


# -- page-level predicates ------------------------------------------------- #


def looks_logged_in(page) -> bool:
    """True when the page is rendering as a signed-in Naukri profile.

    Deliberately structural: a logged-out response redirects to the login URL
    and never contains the profile markers.
    """
    url = (page.url or "").lower()
    if any(h in url for h in sel.LOGIN_URL_HINTS):
        return False
    for marker in sel.LOGGED_IN_MARKER:
        try:
            if page.query_selector(marker) is not None:
                return True
        except Exception:
            continue
    # Fall back to chrome-level text for the pre-profile landing pages.
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return "logout" in body or "/mnjuser/" in url


def detect_challenge(page) -> "str | None":
    """Captcha / OTP / 'unusual activity' interstitials. None when clear."""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return None
    for hint in sel.CHALLENGE_TEXT_HINTS:
        if hint in body:
            return hint
    return None
