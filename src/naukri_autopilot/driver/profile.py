"""Naukri profile page operations.

Everything here addresses the DOM through selectors.py chains - no literal
selector belongs in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import selectors as sel

UPLOAD_SETTLE_MS = 8000
PAGE_SETTLE_MS = 4000


@dataclass
class ProfileState:
    profile_updated: "str | None" = None
    resume_uploaded: "str | None" = None
    resume_name: "str | None" = None
    headline: "str | None" = None

    @property
    def fresh(self) -> bool:
        return sel.is_fresh(self.profile_updated)


# -- selector plumbing ----------------------------------------------------- #


def find(page, name: str, chain: "list[str]", required: bool = True):
    """First element matching any selector in the chain, else SelectorMiss."""
    for selector in chain:
        try:
            el = page.query_selector(selector)
        except Exception:
            continue
        if el is not None:
            return el
    if required:
        raise sel.SelectorMiss(name, chain)
    return None


def text_of(page, name: str, chain: "list[str]", required: bool = False):
    el = find(page, name, chain, required=required)
    if el is None:
        return None
    try:
        return " ".join((el.inner_text() or "").split())
    except Exception:
        return None


# -- reads ----------------------------------------------------------------- #


def open_profile(page) -> None:
    page.goto(config.PROFILE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(PAGE_SETTLE_MS)


def read_state(page) -> ProfileState:
    """Read the page without touching it. Safe in dry-run."""
    name_el = find(page, "resume name", sel.RESUME_NAME, required=False)
    resume_name = None
    if name_el is not None:
        try:
            resume_name = name_el.get_attribute("title") or " ".join(
                (name_el.inner_text() or "").split()
            )
        except Exception:
            resume_name = None

    return ProfileState(
        # Required: this is the field the product exists to move. If it stops
        # resolving we need SELECTOR_MISS and a DOM dump, not a None that
        # quietly reads as "stale" and blames the upload.
        profile_updated=text_of(
            page, "profile updated", sel.PROFILE_UPDATED, required=True
        ),
        # Informational - absence is not worth failing a run over.
        resume_uploaded=text_of(page, "resume uploaded", sel.RESUME_UPLOADED),
        resume_name=resume_name,
        headline=text_of(page, "headline", sel.HEADLINE_TEXT),
    )


# -- writes ---------------------------------------------------------------- #


class UploadRejected(RuntimeError):
    pass


def upload_resume(page, resume: Path) -> None:
    """Re-upload the resume. The one action that moves the ranking timestamp.

    Guards against the adjacent profile-photo input, which would otherwise be a
    very unpleasant surprise: a PDF silently installed as the user's picture.
    """
    if not resume.is_file():
        raise FileNotFoundError(resume)

    target = find(page, "resume input", sel.RESUME_INPUT)

    photo = find(page, "photo input", sel.PHOTO_INPUT, required=False)
    if photo is not None:
        try:
            same = page.evaluate("(a, b) => a === b", [target, photo])
        except Exception:
            same = False
        if same:
            raise UploadRejected(
                "resume selector resolved to the profile-photo input - refusing"
            )

    accept = (target.get_attribute("accept") or "").lower()
    if "image" in accept:
        raise UploadRejected(
            "resume input reports accept={!r} - refusing to upload a PDF".format(accept)
        )

    target.set_input_files(str(resume))
    page.wait_for_timeout(UPLOAD_SETTLE_MS)


def verify_fresh(page) -> ProfileState:
    """Reload and confirm the profile now reads as updated today.

    Assertion, not comparison. The field is day-granular and renders the literal
    string "Today", so a second run on the same day shows no before/after diff at
    all - a diff-based check would call a good run a failure. See README section 3.
    """
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(PAGE_SETTLE_MS)
    return read_state(page)


def set_headline(page, text: str) -> None:
    """Not implemented - the headline sits behind an edit modal (README 14.2).

    Phase 0 answered 14.1 with YES, so resume re-upload alone moves the
    timestamp and rotation is an optional backstop. Mapping the modal is not
    worth doing until that backstop is actually needed.
    """
    raise NotImplementedError(
        "headline editing needs the edit modal mapped; see README section 14.2"
    )
