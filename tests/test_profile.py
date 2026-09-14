"""Selector chains against fixture DOM, in a real browser.

These are the regression tests for Naukri redesigns: when a chain stops matching
production, the first thing to do is capture a fresh DOM, update the fixture,
and watch these fail for the right reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from naukri_autopilot.driver import profile, selectors as sel

FIXTURES = Path(__file__).parent / "fixtures"

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page_for(browser):
    pages = []

    def _open(fixture_name):
        page = browser.new_page()
        page.goto((FIXTURES / fixture_name).resolve().as_uri(),
                  wait_until="domcontentloaded")
        pages.append(page)
        return page

    yield _open
    for p in pages:
        p.close()


def test_reads_stale_profile(page_for):
    state = profile.read_state(page_for("profile_stale.html"))
    assert state.profile_updated == "22Jul , 2026"
    assert state.resume_uploaded == "Uploaded on Feb 23, 2026"
    assert state.resume_name == "sample_resume.pdf"
    assert not state.fresh


def test_reads_fresh_profile(page_for):
    state = profile.read_state(page_for("profile_fresh.html"))
    assert state.profile_updated == "Today"
    assert state.fresh


def test_resume_input_resolves_to_attachcv(page_for):
    page = page_for("profile_stale.html")
    el = profile.find(page, "resume input", sel.RESUME_INPUT)
    assert el.get_attribute("id") == "attachCV"


def test_resume_chain_skips_photo_input_when_attachcv_is_gone(page_for):
    """The dangerous case: primary selector missing, fallbacks still must not
    land on the photo input."""
    page = page_for("profile_stale.html")
    page.evaluate("document.querySelector('#attachCV').remove()")
    el = profile.find(page, "resume input", sel.RESUME_INPUT, required=False)
    assert el is None or el.get_attribute("id") != "fileUpload"


def test_upload_refuses_image_input(page_for, tmp_path):
    """If the chain ever did resolve to an image field, upload must refuse."""
    page = page_for("profile_stale.html")
    page.evaluate("document.querySelector('#attachCV').setAttribute('accept','image/*')")
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(profile.UploadRejected):
        profile.upload_resume(page, pdf)


def test_missing_required_selector_raises_selector_miss(page_for):
    page = page_for("profile_stale.html")
    page.evaluate("document.querySelectorAll('.mod-date-val,.mod-date').forEach(e=>e.remove())")
    with pytest.raises(sel.SelectorMiss):
        profile.read_state(page)


def test_reads_the_headline(page_for):
    """The first version of this chain was guessed, not captured, and silently
    returned None against the real page - `inspect` is what caught it."""
    state = profile.read_state(page_for("profile_stale.html"))
    assert state.headline == "Senior Widget Engineer | Gadget Specialist"


def test_upload_refuses_an_oversized_resume(page_for, tmp_path):
    """Naukri states a 2 MB limit on the page itself; fail before the browser
    work rather than after a refused upload."""
    page = page_for("profile_stale.html")
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"x" * (sel.MAX_RESUME_BYTES + 1))
    with pytest.raises(profile.UploadRejected) as exc:
        profile.upload_resume(page, big)
    assert "2 MB" in str(exc.value)


def test_upload_refuses_an_unsupported_format(page_for, tmp_path):
    page = page_for("profile_stale.html")
    bad = tmp_path / "cv.txt"
    bad.write_text("nope")
    with pytest.raises(profile.UploadRejected):
        profile.upload_resume(page, bad)
