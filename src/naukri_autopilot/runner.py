"""One run, start to terminal state.

Phase 2's scheduler decides *whether* to call this; this module owns *what
happens* when it is called. Every path returns a RunResult - the caller never
sees an exception - and every path leaves a screenshot behind.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import config
from .driver import profile, selectors as sel, session as sess
from .results import ErrorKind, RunResult, Status, Trigger


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _screenshot(page, tag: str) -> "str | None":
    """Best effort. A failed screenshot must never mask the real error."""
    try:
        config.ensure_dirs()
        path = config.SCREENSHOT_DIR / "{}-{}.png".format(_stamp(), tag)
        page.screenshot(path=str(path), full_page=True)
        return str(path.relative_to(config.PROJECT_ROOT))
    except Exception:
        return None


def _dump_dom(page, tag: str) -> "str | None":
    """Selector drift is the expected long-term failure; keep the evidence."""
    try:
        config.ensure_dirs()
        path = config.DEBUG_DIR / "{}-{}.html".format(_stamp(), tag)
        path.write_text(page.content(), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def run_once(
    resume_path: "str | Path",
    trigger: str = Trigger.MANUAL,
    dry_run: bool = False,
    offscreen: bool = False,
    channel: str = "auto",
) -> RunResult:
    result = RunResult(status=Status.FAILED, trigger=trigger)
    resume = Path(resume_path).expanduser()

    # Fail before opening a browser - the cheapest possible failure.
    if not dry_run and not resume.is_file():
        return result.finish(
            Status.FAILED,
            error_kind=ErrorKind.RESUME_MISSING,
            error_detail="resume not found: {}".format(resume),
        )

    try:
        with sess.Session(channel=channel, offscreen=offscreen) as s:
            page = s.page()

            try:
                profile.open_profile(page)
            except Exception as exc:
                return result.finish(
                    Status.FAILED,
                    error_kind=ErrorKind.NETWORK,
                    error_detail=str(exc).splitlines()[0],
                    screenshot=_screenshot(page, "network"),
                )

            challenge = sess.detect_challenge(page)
            if challenge:
                return result.finish(
                    Status.NEEDS_LOGIN,
                    error_kind=ErrorKind.CHALLENGE,
                    error_detail="challenge on page: {!r}".format(challenge),
                    screenshot=_screenshot(page, "challenge"),
                )

            if not sess.looks_logged_in(page):
                return result.finish(
                    Status.NEEDS_LOGIN,
                    error_kind=ErrorKind.SESSION_EXPIRED,
                    error_detail="not signed in - run `naukri-autopilot login`",
                    screenshot=_screenshot(page, "needs-login"),
                )

            # Resolving selectors is most of what dry-run is for.
            try:
                before = profile.read_state(page)
            except sel.SelectorMiss as miss:
                _dump_dom(page, "selector-miss")
                return result.finish(
                    Status.FAILED,
                    error_kind=ErrorKind.SELECTOR_MISS,
                    error_detail=str(miss),
                    screenshot=_screenshot(page, "selector-miss"),
                )

            result.already_fresh = before.fresh
            result.headline_used = before.headline

            if dry_run:
                # Prove the upload target resolves, without writing to it.
                try:
                    profile.find(page, "resume input", sel.RESUME_INPUT)
                except sel.SelectorMiss as miss:
                    _dump_dom(page, "selector-miss")
                    return result.finish(
                        Status.FAILED,
                        error_kind=ErrorKind.SELECTOR_MISS,
                        error_detail=str(miss),
                        screenshot=_screenshot(page, "selector-miss"),
                    )
                return result.finish(
                    Status.DRY_RUN,
                    profile_ts=before.profile_updated,
                    screenshot=_screenshot(page, "dry-run"),
                )

            try:
                profile.upload_resume(page, resume)
            except (profile.UploadRejected, FileNotFoundError) as exc:
                return result.finish(
                    Status.FAILED,
                    error_kind=ErrorKind.UPLOAD_REJECTED,
                    error_detail=str(exc),
                    screenshot=_screenshot(page, "upload-rejected"),
                )
            except sel.SelectorMiss as miss:
                _dump_dom(page, "selector-miss")
                return result.finish(
                    Status.FAILED,
                    error_kind=ErrorKind.SELECTOR_MISS,
                    error_detail=str(miss),
                    screenshot=_screenshot(page, "selector-miss"),
                )

            after = profile.verify_fresh(page)
            shot = _screenshot(page, "run")

            if not after.fresh:
                # Uploaded, but the page does not agree it counts. This is the
                # silent-failure case the whole verification step exists for.
                return result.finish(
                    Status.FAILED,
                    error_kind=ErrorKind.UPLOAD_REJECTED,
                    error_detail=(
                        "upload completed but profile still reads {!r}".format(
                            after.profile_updated
                        )
                    ),
                    profile_ts=after.profile_updated,
                    screenshot=shot,
                )

            return result.finish(
                Status.SUCCESS,
                profile_ts=after.profile_updated,
                screenshot=shot,
            )

    except sess.BrowserUnavailable as exc:
        return result.finish(
            Status.FAILED, error_kind=ErrorKind.UNKNOWN, error_detail=str(exc)
        )
    except Exception as exc:
        return result.finish(
            Status.FAILED,
            error_kind=ErrorKind.UNKNOWN,
            error_detail="{}: {}".format(type(exc).__name__, str(exc).splitlines()[0]),
        )


def login(channel: str = "auto", timeout_s: int = 300) -> RunResult:
    """Headed manual sign-in. Nothing types the user's password but the user."""
    result = RunResult(status=Status.FAILED, trigger=Trigger.MANUAL)
    try:
        with sess.Session(channel=channel, offscreen=False) as s:
            page = s.page()
            page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
            landed = s.wait_for_login(timeout_s=timeout_s)
            if landed is None:
                return result.finish(
                    Status.NEEDS_LOGIN,
                    error_kind=ErrorKind.SESSION_EXPIRED,
                    error_detail="timed out after {}s".format(timeout_s),
                    screenshot=_screenshot(page, "login-timeout"),
                )
            s.save_storage_state()
            return result.finish(Status.SUCCESS)
    except sess.BrowserUnavailable as exc:
        return result.finish(
            Status.FAILED, error_kind=ErrorKind.UNKNOWN, error_detail=str(exc)
        )
