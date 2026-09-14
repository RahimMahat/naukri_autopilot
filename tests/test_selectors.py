"""Freshness parsing. No browser needed - pure string logic."""

from __future__ import annotations

import pytest

from naukri_autopilot.driver import selectors as sel


@pytest.mark.parametrize(
    "value",
    ["Today", "today", "TODAY", "  Today  ", "Today\n"],
)
def test_fresh_values(value):
    assert sel.is_fresh(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "22Jul , 2026",
        "14Sep , 2026",  # an absolute date for today still is not "Today"
        "Yesterday",
        "2 days ago",
        "Today's date",  # substring match would wrongly pass this
    ],
)
def test_stale_values(value):
    assert not sel.is_fresh(value)


def test_resume_chain_never_falls_back_to_bare_file_input():
    """The profile-photo input sits next to the resume input in the DOM.

    A bare `input[type=file]` fallback would be able to match it, which means a
    PDF posted as the user's profile picture. Every fallback must exclude images.
    """
    for selector in sel.RESUME_INPUT[1:]:
        assert "not([accept*='image'])" in selector.replace('"', "'"), selector


def test_selector_miss_names_what_it_tried():
    miss = sel.SelectorMiss("resume input", ["#a", "#b"])
    assert "resume input" in str(miss)
    assert "#a" in str(miss) and "#b" in str(miss)
