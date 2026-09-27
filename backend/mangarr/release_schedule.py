"""Release cadence per series, from Chapter.released_at.

Release dates come from the MangaUpdates release feed (update_chapters
stamps them), so they describe when a chapter first appeared anywhere, not
when a source served it. A "release" here is a distinct day: a batch of
chapters dropped together is one event, otherwise a series that releases
three chapters every Monday would look like it releases daily."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from .models import Chapter, SeriesStatus

# how many recent release days the estimate looks at: enough to smooth out a
# skipped week, few enough that a schedule change shows up quickly
CADENCE_WINDOW = 10
MIN_RELEASE_DAYS = 3
# a series that has gone this many cadences without a release is on an
# unannounced break — predicting "any day now" forever would be noise
STALE_AFTER_CADENCES = 4
# statuses that can still produce new chapters
ACTIVE_STATUSES = {SeriesStatus.RELEASING, SeriesStatus.UNKNOWN}


@dataclass
class ReleaseSchedule:
    cadence_days: float | None = None
    last_released_at: datetime | None = None
    last_number: float | None = None
    next_expected_at: datetime | None = None


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; everything is stored as UTC
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def estimate_cadence(release_days: list[datetime]) -> float | None:
    """Median gap in days between the most recent distinct release days, or
    None with too little history to call it a schedule."""
    days = sorted({_aware(d).date() for d in release_days})[-CADENCE_WINDOW:]
    if len(days) < MIN_RELEASE_DAYS:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    return float(median(gaps))


def release_schedule(
    chapters: list[Chapter], status: SeriesStatus | str, now: datetime | None = None
) -> ReleaseSchedule:
    now = now or datetime.now(timezone.utc)
    dated = [c for c in chapters if not c.excluded and c.released_at is not None]
    if not dated:
        return ReleaseSchedule()
    latest = max(dated, key=lambda c: (_aware(c.released_at), c.number))
    out = ReleaseSchedule(
        cadence_days=estimate_cadence([c.released_at for c in dated]),
        last_released_at=_aware(latest.released_at),
        last_number=max(c.number for c in dated),
    )
    try:
        active = SeriesStatus(status) in ACTIVE_STATUSES
    except ValueError:
        active = False
    if active and out.cadence_days:
        # cadence_days can't be 0: release days are distinct dates
        expected = out.last_released_at + timedelta(days=out.cadence_days)
        if now - expected <= timedelta(days=out.cadence_days * STALE_AFTER_CADENCES):
            out.next_expected_at = expected
    return out


def cadence_label(cadence_days: float | None) -> str:
    if cadence_days is None:
        return ""
    if cadence_days <= 1.5:
        return "Daily"
    if 6 <= cadence_days <= 8:
        return "Weekly"
    if 12 <= cadence_days <= 16:
        return "Every 2 weeks"
    if 26 <= cadence_days <= 35:
        return "Monthly"
    return f"Every ~{round(cadence_days)} days"
