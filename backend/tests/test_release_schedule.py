from datetime import datetime, timedelta, timezone

from mangarr.models import Chapter, SeriesStatus
from mangarr.release_schedule import cadence_label, estimate_cadence, release_schedule

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _chapters(*days_ago: float, start: float = 1) -> list[Chapter]:
    return [
        Chapter(number=start + i, released_at=NOW - timedelta(days=d), excluded=False)
        for i, d in enumerate(sorted(days_ago, reverse=True))
    ]


def test_weekly_cadence_and_next_release():
    schedule = release_schedule(_chapters(28, 21, 14, 7, 0), SeriesStatus.RELEASING, NOW)
    assert schedule.cadence_days == 7
    assert schedule.last_number == 5
    assert schedule.next_expected_at.date() == (NOW + timedelta(days=7)).date()


def test_same_day_batch_counts_as_one_release():
    # three chapters every other Monday: the gap is 14 days, not 0
    days = [28, 28, 28, 14, 14, 14, 0, 0, 0]
    assert estimate_cadence([NOW - timedelta(days=d) for d in days]) == 14


def test_median_ignores_a_single_break():
    days = [42, 35, 28, 14, 7, 0]  # one skipped week
    assert estimate_cadence([NOW - timedelta(days=d) for d in days]) == 7


def test_too_little_history_has_no_cadence():
    assert estimate_cadence([NOW, NOW - timedelta(days=7)]) is None
    assert release_schedule(_chapters(7, 0), SeriesStatus.RELEASING, NOW).next_expected_at is None


def test_finished_series_expects_nothing():
    schedule = release_schedule(_chapters(21, 14, 7, 0), SeriesStatus.FINISHED, NOW)
    assert schedule.cadence_days == 7
    assert schedule.next_expected_at is None


def test_long_silence_stops_predicting():
    # weekly until three months ago: an unannounced break, not "due any day"
    schedule = release_schedule(_chapters(111, 104, 97, 90), SeriesStatus.RELEASING, NOW)
    assert schedule.cadence_days == 7
    assert schedule.next_expected_at is None


def test_recently_overdue_is_still_expected():
    schedule = release_schedule(_chapters(31, 24, 17, 10), SeriesStatus.RELEASING, NOW)
    assert schedule.next_expected_at < NOW


def test_excluded_and_undated_chapters_are_ignored():
    chapters = _chapters(21, 14, 7, 0)
    chapters.append(Chapter(number=99, released_at=None, excluded=False))
    chapters.append(Chapter(number=100, released_at=NOW, excluded=True))
    assert release_schedule(chapters, SeriesStatus.RELEASING, NOW).last_number == 4


def test_naive_datetimes_are_treated_as_utc():
    chapters = [
        Chapter(number=n, released_at=(NOW - timedelta(days=d)).replace(tzinfo=None),
                excluded=False)
        for n, d in ((1, 14), (2, 7), (3, 0))
    ]
    assert release_schedule(chapters, "releasing", NOW).last_released_at.tzinfo is not None


def test_cadence_labels():
    assert cadence_label(None) == ""
    assert cadence_label(1) == "Daily"
    assert cadence_label(7) == "Weekly"
    assert cadence_label(14) == "Every 2 weeks"
    assert cadence_label(30) == "Monthly"
    assert cadence_label(3.5) == "Every ~4 days"
    assert cadence_label(60) == "Every ~60 days"


async def test_calendar_lists_recent_releases_and_expected_ones():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from mangarr.api.calendar import calendar
    from mangarr.models import Base, Series

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        weekly = Series(title="Weekly", status=SeriesStatus.RELEASING)
        weekly.chapters = [
            Chapter(number=n, released_at=now - timedelta(days=d), downloaded=(n == 3))
            for n, d in ((1, 21), (2, 14), (3, 7))
        ]
        weekly.chapters.append(
            Chapter(number=4, released_at=now - timedelta(days=1), excluded=True)
        )
        done = Series(title="Done", status=SeriesStatus.FINISHED)
        done.chapters = [
            Chapter(number=n, released_at=now - timedelta(days=d)) for n, d in ((1, 9), (2, 2))
        ]
        session.add_all([weekly, done])
        await session.commit()
        out = await calendar(days_back=10, days_ahead=14, session=session)
    await engine.dispose()
    assert [(r.series_title, r.number, r.downloaded) for r in out.released] == [
        ("Done", 2, False), ("Weekly", 3, True), ("Done", 1, False),
    ]
    assert [(e.series_title, e.cadence_label, e.overdue) for e in out.expected] == [
        ("Weekly", "Weekly", False),
    ]
