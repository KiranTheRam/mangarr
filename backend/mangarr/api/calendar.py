from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Chapter, Series
from ..release_schedule import cadence_label, release_schedule
from ..schemas import CalendarExpectedOut, CalendarOut, CalendarReleaseOut

router = APIRouter(tags=["calendar"])

MAX_RELEASED = 500


@router.get("/calendar", response_model=CalendarOut)
async def calendar(
    days_back: int = Query(14, ge=0, le=90),
    days_ahead: int = Query(14, ge=0, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Chapters released in the last `days_back` days and the next release
    each ongoing series is expected to have within `days_ahead`, going by
    its usual release rhythm."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days_back)
    rows = await session.execute(
        select(Chapter, Series.title, Series.cover_url, Series.monitored)
        .join(Series, Chapter.series_id == Series.id)
        .where(
            Chapter.excluded == False,  # noqa: E712
            Chapter.released_at.isnot(None),
            Chapter.released_at >= since,
        )
        .order_by(Chapter.released_at.desc(), Series.title, Chapter.number)
        .limit(MAX_RELEASED)
    )
    released = [
        CalendarReleaseOut(
            series_id=ch.series_id,
            series_title=title,
            cover_url=cover,
            chapter_id=ch.id,
            number=ch.number,
            volume=ch.volume,
            title=ch.title,
            released_at=ch.released_at,
            downloaded=ch.downloaded,
            monitored=series_monitored and ch.monitored,
        )
        for ch, title, cover, series_monitored in rows.all()
    ]

    # only what the cadence needs, not whole chapter rows for the library
    dated: dict[int, list[Chapter]] = defaultdict(list)
    for series_id, number, released_at in (
        await session.execute(
            select(Chapter.series_id, Chapter.number, Chapter.released_at).where(
                Chapter.excluded == False,  # noqa: E712
                Chapter.released_at.isnot(None),
            )
        )
    ).all():
        dated[series_id].append(Chapter(number=number, released_at=released_at))
    horizon = now + timedelta(days=days_ahead)
    expected: list[CalendarExpectedOut] = []
    for series in (await session.execute(select(Series))).scalars().all():
        schedule = release_schedule(dated.get(series.id, []), series.status, now)
        if schedule.next_expected_at is None or schedule.next_expected_at > horizon:
            continue
        expected.append(CalendarExpectedOut(
            series_id=series.id,
            series_title=series.title,
            cover_url=series.cover_url,
            expected_at=schedule.next_expected_at,
            cadence_days=schedule.cadence_days,
            cadence_label=cadence_label(schedule.cadence_days),
            last_released_at=schedule.last_released_at,
            last_number=schedule.last_number,
            overdue=schedule.next_expected_at.date() < now.date(),
            monitored=series.monitored,
        ))
    expected.sort(key=lambda e: (e.expected_at, e.series_title))
    return CalendarOut(released=released, expected=expected)
