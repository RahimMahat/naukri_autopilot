"""Hand-rolled contribution-grid SVG.

No chart library and no CDN - partly because the page must work offline, and
partly because the privacy claim in the README's Security model is absolute: the
dashboard makes no outbound request of any kind. A charting library pulled from a CDN
would quietly break that.

Pure functions over a list of (date, status) pairs, so the layout is testable
without a server or a browser.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
from html import escape

from ..results import Status

CELL = 12
GAP = 3
WEEKS = 26
DAYS = 7

# Status precedence within one day: a success outranks anything else, because a
# day that ended with the profile updated is a good day regardless of how many
# retries it took to get there.
PRECEDENCE = [Status.SUCCESS, Status.FAILED, Status.NEEDS_LOGIN, Status.DRY_RUN]

CLASS_FOR = {
    Status.SUCCESS: "ok",
    Status.FAILED: "bad",
    Status.NEEDS_LOGIN: "login",
    Status.DRY_RUN: "dry",
}

LABEL_FOR = {
    Status.SUCCESS: "updated",
    Status.FAILED: "failed",
    Status.NEEDS_LOGIN: "sign-in needed",
    Status.DRY_RUN: "dry run",
}


def day_status(statuses: "list[str]") -> "str | None":
    """Collapse one day's runs to the single status worth showing."""
    for candidate in PRECEDENCE:
        if candidate in statuses:
            return candidate
    return None


def bucket_by_day(rows, today: "date | None" = None) -> "OrderedDict[date, str]":
    """rows: iterable of (iso_timestamp, status). Newest or oldest order is fine."""
    per_day = {}
    for started_at, status in rows:
        if not started_at:
            continue
        try:
            day = date.fromisoformat(started_at[:10])
        except ValueError:
            continue
        per_day.setdefault(day, []).append(status)

    out = OrderedDict()
    for day in sorted(per_day):
        collapsed = day_status(per_day[day])
        if collapsed:
            out[day] = collapsed
    return out


def grid_start(today: date, weeks: int = WEEKS) -> date:
    """Monday of the week that begins the window."""
    start = today - timedelta(weeks=weeks - 1)
    return start - timedelta(days=start.weekday())


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LABEL_H = 14


def month_labels(start: date, weeks: int = WEEKS) -> "list[tuple[int, str]]":
    """(week index, name) for each week where a new month first appears.

    Without these the grid is decorative: pretty, but you cannot tell which
    period it covers or when a gap happened.
    """
    out = []
    seen = None
    for w in range(weeks):
        month = (start + timedelta(weeks=w)).month
        if month != seen:
            # Skip a label in the final column; there is no room to draw it.
            if w < weeks - 1:
                out.append((w, MONTHS[month - 1]))
            seen = month
    return out


def render(rows, today: "date | None" = None, weeks: int = WEEKS) -> str:
    today = today or date.today()
    by_day = bucket_by_day(rows)
    start = grid_start(today, weeks)

    width = weeks * (CELL + GAP) + GAP
    height = DAYS * (CELL + GAP) + GAP + LABEL_H

    parts = [
        '<svg class="grid" viewBox="0 0 {} {}" width="{}" height="{}" '
        'role="img" aria-label="Daily profile-update activity">'.format(
            width, height, width, height
        )
    ]

    for w, name in month_labels(start, weeks):
        parts.append(
            '<text class="mlabel" x="{}" y="10">{}</text>'.format(
                GAP + w * (CELL + GAP), name
            )
        )

    for w in range(weeks):
        for d in range(DAYS):
            day = start + timedelta(weeks=w, days=d)
            if day > today:
                continue
            status = by_day.get(day)
            cls = CLASS_FOR.get(status, "none")
            label = LABEL_FOR.get(status, "nothing")
            x = GAP + w * (CELL + GAP)
            y = GAP + LABEL_H + d * (CELL + GAP)
            parts.append(
                '<rect class="cell {}" x="{}" y="{}" width="{}" height="{}" rx="2">'
                "<title>{} — {}</title></rect>".format(
                    cls, x, y, CELL, CELL, escape(day.isoformat()), escape(label)
                )
            )

    parts.append("</svg>")
    return "".join(parts)


def legend() -> str:
    items = [("none", "nothing"), ("ok", "updated"), ("bad", "failed"),
             ("login", "sign-in needed"), ("dry", "dry run")]
    cells = "".join(
        '<span class="key"><i class="cell {}"></i>{}</span>'.format(cls, escape(text))
        for cls, text in items
    )
    return '<div class="legend">{}</div>'.format(cells)


def streak(by_day: "OrderedDict[date, str]", today: "date | None" = None) -> int:
    """Consecutive days ending today (or yesterday) with a successful update.

    Yesterday counts as still alive: with a 24h interval and jitter, today's run
    may simply not have fired yet, and resetting the streak at midnight would be
    both wrong and dispiriting.
    """
    today = today or date.today()
    if not by_day:
        return 0
    cursor = today
    if by_day.get(cursor) != Status.SUCCESS:
        cursor = today - timedelta(days=1)
        if by_day.get(cursor) != Status.SUCCESS:
            return 0
    count = 0
    while by_day.get(cursor) == Status.SUCCESS:
        count += 1
        cursor -= timedelta(days=1)
    return count
