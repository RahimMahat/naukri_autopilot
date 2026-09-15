"""Every selector, in one file, each as a fallback chain.

Naukri will redesign. When it does, this file is the whole repair surface - which
is the only reason the rest of the driver is allowed to stay short.

Chains are tried in order and the first match wins. Put the most specific,
most stable hook first (an id), then structural fallbacks. A chain that runs out
raises SelectorMiss, which the runner records with a DOM dump.

Captured from a live profile on 2026-09-14; see README, Open questions.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Profile freshness - the value the entire product exists to move.
#
# Renders the literal string "Today" once updated, or an absolute date like
# "22Jul , 2026" otherwise. Day-granular. See FRESH below.
# --------------------------------------------------------------------------- #
PROFILE_UPDATED = [
    ".mod-date-val",
    ".mod-date .typ-14Medium",
    ".mode-date-wrap .typ-14Medium",
]

# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #

# DANGER: #fileUpload, immediately next to this in the DOM, is the profile PHOTO
# input. Never fall back to a bare input[type=file] - the :not([accept*='image'])
# guard is what keeps a PDF out of the user's profile picture.
RESUME_INPUT = [
    "#attachCV",
    "input[type=file].fileUpload:not([accept*='image'])",
    "input[type=file]:not([accept*='image'])",
]

# Never targeted. Present so the driver can assert it has NOT selected this.
PHOTO_INPUT = [
    "#fileUpload",
    "input[type=file][accept*='image']",
]

RESUME_UPLOADED = [
    ".updateOn",
    ".cvPreview .typ-14Regular",
]

RESUME_NAME = [
    ".resume-name-inline .exten",
    ".resume-name-inline [title]",
    ".cvPreview .truncate",
]

# --------------------------------------------------------------------------- #
# Headline
#
# Renders as a plain div with an adjacent edit icon - editing means driving a
# modal, not typing into a field. Reading is all that is needed: rotation is a
# deferred backstop (see README, open question 2), so nothing writes this.
# --------------------------------------------------------------------------- #
HEADLINE_TEXT = [
    "#lazyResumeHead .widgetCont .prefill",
    "#lazyResumeHead .prefill",
    ".resumeHeadline .prefill",
]

# --------------------------------------------------------------------------- #
# Session / challenge detection
# --------------------------------------------------------------------------- #

# Presence of any of these means the page rendered as a logged-in profile.
LOGGED_IN_MARKER = [
    ".mod-date",
    "#attachCV",
    ".cvPreview",
]

LOGIN_URL_HINTS = ("nlogin", "/login")

CHALLENGE_TEXT_HINTS = (
    "captcha",
    "verify it's you",
    "verify its you",
    "unusual activity",
    "enter otp",
)

# --------------------------------------------------------------------------- #

# What PROFILE_UPDATED reads when the profile counts as updated today. Compared
# case-insensitively after whitespace collapse. Anything else means stale -
# deliberately not an enumeration of Naukri's relative-date vocabulary, since
# guessing at "Yesterday"/"2 days ago" would just be a second thing to get wrong.
FRESH = "today"


def is_fresh(value: "str | None") -> bool:
    return bool(value) and " ".join(value.split()).lower() == FRESH


# Naukri states its own limit on the profile page: "Supported Formats: doc,
# docx, rtf, pdf, upto 2 MB". Checked before a browser is opened, because an
# oversized file fails silently-ish - the upload is simply refused.
MAX_RESUME_BYTES = 2 * 1024 * 1024
ALLOWED_RESUME_SUFFIXES = (".pdf", ".doc", ".docx", ".rtf")


class SelectorMiss(Exception):
    """Every candidate in a chain failed to resolve."""

    def __init__(self, name: str, chain: "list[str]"):
        self.name = name
        self.chain = chain
        super().__init__(
            "{} did not resolve. Tried: {}".format(name, ", ".join(chain))
        )
