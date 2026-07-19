"""Decimal chapters (60.5 …) are specials/bonus chapters that many sources
never release. They are tracked and searched for like any other chapter, but
they must not keep a series from counting as fully downloaded — otherwise a
title like "Kage no Jitsuryokusha ni Naritakute" sits at 99% forever over a
ch. 60.5 nobody scanlated."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.api.series import get_series, list_series
from mangarr.models import Base, Chapter, Series
from mangarr.util import is_special_chapter


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _series_with(session, numbers_downloaded):
    series = Series(title="Kage no Jitsuryokusha", sort_title="kage no jitsuryokusha")
    for number, downloaded in numbers_downloaded:
        series.chapters.append(Chapter(number=number, downloaded=downloaded))
    session.add(series)
    await session.commit()
    return series


@pytest.mark.parametrize(
    "number,special",
    [(1.0, False), (60.0, False), (60.5, True), (9.1, True), (0.5, True)],
)
def test_only_decimal_chapters_are_special(number, special):
    assert is_special_chapter(number) is special


async def test_missing_special_still_counts_as_fully_downloaded(db_session):
    # every main chapter on disk, ch. 60.5 never released
    await _series_with(db_session, [(59.0, True), (60.0, True), (60.5, False), (61.0, True)])

    [out] = await list_series(db_session)

    assert (out.downloaded_count, out.chapter_count) == (3, 3)
    assert out.downloaded_count == out.chapter_count  # the UI's "complete" test
    assert (out.special_downloaded_count, out.special_count) == (0, 1)


async def test_missing_main_chapter_still_blocks_completion(db_session):
    await _series_with(db_session, [(59.0, True), (60.0, False), (60.5, True)])

    [out] = await list_series(db_session)

    assert (out.downloaded_count, out.chapter_count) == (1, 2)
    assert (out.special_downloaded_count, out.special_count) == (1, 1)


async def test_detail_counts_match_the_list_counts(db_session):
    series = await _series_with(
        db_session, [(59.0, True), (60.0, True), (60.5, False), (61.0, True)]
    )

    [listed] = await list_series(db_session)
    detail = await get_series(series.id, db_session)

    assert (detail.chapter_count, detail.downloaded_count) == (
        listed.chapter_count,
        listed.downloaded_count,
    )
    assert (detail.special_count, detail.special_downloaded_count) == (
        listed.special_count,
        listed.special_downloaded_count,
    )


async def test_excluded_chapters_are_absent_from_all_completion_counts(db_session):
    series = await _series_with(
        db_session, [(1.0, True), (2.0, False), (2.5, False)]
    )
    series.chapters[1].excluded = True
    series.chapters[2].excluded = True
    await db_session.commit()

    [listed] = await list_series(db_session)
    detail = await get_series(series.id, db_session)

    assert (listed.chapter_count, listed.downloaded_count) == (1, 1)
    assert (listed.special_count, listed.special_downloaded_count) == (0, 0)
    assert (detail.chapter_count, detail.downloaded_count) == (1, 1)
    assert len(detail.chapters) == 3  # retained in the editor so exclusion is reversible
    assert sum(ch.excluded for ch in detail.chapters) == 2


async def test_series_without_chapters_reports_zeroes(db_session):
    session = db_session
    session.add(Series(title="Empty", sort_title="empty"))
    await session.commit()

    [out] = await list_series(session)

    assert (out.chapter_count, out.downloaded_count) == (0, 0)
    assert (out.special_count, out.special_downloaded_count) == (0, 0)
