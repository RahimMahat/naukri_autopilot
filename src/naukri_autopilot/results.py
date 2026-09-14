"""Run outcomes. Shared by the driver, the store (Phase 2) and the dashboard (Phase 3).

Plain strings rather than enums: these go straight into a SQLite TEXT column and
come back out again, and a round-trip through .value/.name buys nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class Status:
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    DRY_RUN = "DRY_RUN"


class ErrorKind:
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SELECTOR_MISS = "SELECTOR_MISS"
    UPLOAD_REJECTED = "UPLOAD_REJECTED"
    NETWORK = "NETWORK"
    CHALLENGE = "CHALLENGE"
    RESUME_MISSING = "RESUME_MISSING"
    UNKNOWN = "UNKNOWN"


class Trigger:
    SCHEDULE = "schedule"
    MANUAL = "manual"
    CATCHUP = "catchup"
    RETRY = "retry"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunResult:
    status: str
    trigger: str
    started_at: str = field(default_factory=utcnow)
    finished_at: "str | None" = None
    profile_ts: "str | None" = None  # .mod-date-val as read after the write
    headline_used: "str | None" = None
    error_kind: "str | None" = None
    error_detail: "str | None" = None
    screenshot: "str | None" = None
    # True when the profile already read "Today" before we touched it - the run
    # still succeeded, but it proved nothing about our own upload.
    already_fresh: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.SUCCESS, Status.DRY_RUN)

    def finish(self, status: str, **kw) -> "RunResult":
        self.status = status
        self.finished_at = utcnow()
        for k, v in kw.items():
            setattr(self, k, v)
        return self

    def summary(self) -> str:
        bits = [self.status]
        if self.profile_ts:
            bits.append("profile={!r}".format(self.profile_ts))
        if self.already_fresh:
            bits.append("(was already fresh)")
        if self.error_kind:
            bits.append("{}: {}".format(self.error_kind, self.error_detail))
        return "  ".join(bits)
