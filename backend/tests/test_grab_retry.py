"""Monitor grabs and direct queue retries against a real database."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from mangarr import settings_service
from mangarr.api import queue
from mangarr.download import direct
from mangarr.download.cbz import write_cbz
from mangarr.jobs import tasks
from mangarr.jobs.tasks import (
    REMOVED_BY_USER,
    auto_grab_best_torrent,
    grab_missing_chapters,
    update_chapters,
)
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    RootFolder,
    Series,
    SeriesSourceLink,
)
from mangarr.sources.base import DirectSource, SourceChapter, TorrentRelease
from mangarr.torrent_selection import TorrentSelection


class FakeSource(DirectSource):
    name = "fake"

    def __init__(self, numbers):
        self.numbers = numbers
        self.list_calls = 0

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        self.list_calls += 1
        return [
            SourceChapter(source_name=self.name, external_id=f"c{n}", number=float(n))
            for n in self.numbers
        ]

    async def get_pages(self, chapter_external_id):
        return []


@pytest.fixture
async def db_session(monkeypatch, tmp_path):
    monkeypatch.setitem(settings_service.DEFAULTS, "source_fake_enabled", "true")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'grab.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        # the monitor only grabs for a series with a root folder
        (tmp_path / "library").mkdir()
        session.info["library"] = RootFolder(path=str(tmp_path / "library"))
        yield session
    await engine.dispose()


async def _make_series(session, chapter_numbers):
    series = Series(title="Test Series", sort_title="test series",
                    root_folder=session.info["library"])
    series.source_links.append(
        SeriesSourceLink(source_name="fake", external_id="x")
    )
    series.chapters.extend(
        Chapter(number=float(n), monitored=True) for n in chapter_numbers
    )
    session.add(series)
    await session.commit()
    # reload with eager-loaded relationships, the way _load_series does — an
    # async session cannot lazy-load on attribute access
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series.id)
    )
    return result.scalar_one()


def _use_source(monkeypatch, source):
    monkeypatch.setattr(
        tasks.registry, "enabled_direct_sources", lambda values: [source]
    )


async def test_grabs_missing_chapters(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)

    queued = await grab_missing_chapters(db_session, series, {})

    assert queued == 2


async def test_excluded_chapters_are_not_grabbed(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    series.chapters[0].excluded = True
    await db_session.commit()
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)

    queued = await grab_missing_chapters(db_session, series, {})

    assert queued == 1
    direct = (
        await db_session.execute(
            select(Download).where(Download.kind == DownloadKind.DIRECT)
        )
    ).scalar_one()
    assert direct.chapter_id == series.chapters[1].id


async def test_recent_failure_blocks_the_source(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error="page 3 failed",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 0


async def test_user_removal_does_not_block_the_source(db_session, monkeypatch):
    """Cancelling a queued grab must not blacklist that (chapter, source)."""
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error=REMOVED_BY_USER,
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 1


async def test_old_failure_is_retried(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    stale = datetime.now(timezone.utc) - tasks.FAILED_GRAB_RETRY_AFTER - timedelta(days=1)
    db_session.add(Download(
        series_id=series.id, chapter_id=series.chapters[0].id,
        kind=DownloadKind.DIRECT, status=DownloadStatus.FAILED,
        source_name="fake", error="page 3 failed",
        created_at=stale, updated_at=stale,
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 1


async def test_series_level_download_still_blocks_grabs(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id, chapter_id=None,
        kind=DownloadKind.TORRENT, status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(db_session, series, {}) == 0


async def test_empty_exclusions_do_not_bypass_series_download_guard(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    source = FakeSource([1])
    _use_source(monkeypatch, source)
    db_session.add(Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()

    assert await grab_missing_chapters(
        db_session, series, {}, exclude_numbers=set()
    ) == 0


async def test_chapter_cache_shared_between_update_and_grab(db_session, monkeypatch):
    """One monitor pass must fetch each source's chapter list once, not once
    per consumer."""
    series = await _make_series(db_session, [])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)
    # keep update_chapters off the network beyond the fake source
    series.mangaupdates_id = None

    cache = {}
    added = await update_chapters(db_session, series, {}, cache)
    queued = await grab_missing_chapters(db_session, series, {}, chapter_cache=cache)

    assert added == 2
    assert queued == 2
    assert source.list_calls == 1
    assert all(ch.available_sources == "fake" for ch in series.chapters)


async def test_unrefreshed_chapter_availability_is_unknown(db_session):
    series = await _make_series(db_session, [1])
    assert series.chapters[0].available_sources is None


async def test_successful_refresh_clears_stale_source_availability(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    for chapter in series.chapters:
        chapter.available_sources = "fake"
    await db_session.commit()
    source = FakeSource([1])
    _use_source(monkeypatch, source)

    await update_chapters(db_session, series, {})

    assert {ch.number: ch.available_sources for ch in series.chapters} == {
        1.0: "fake",
        2.0: "",
    }


async def test_refresh_does_not_mutate_excluded_chapter(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    chapter = series.chapters[0]
    chapter.excluded = True
    chapter.title = "Keep me"
    chapter.volume = 7
    chapter.available_sources = "old-source"
    await db_session.commit()
    source = FakeSource([1])
    _use_source(monkeypatch, source)

    await update_chapters(db_session, series, {})

    assert chapter.title == "Keep me"
    assert chapter.volume == 7
    assert chapter.available_sources == "old-source"


async def test_refresh_clears_availability_from_disabled_sources(db_session, monkeypatch):
    series = await _make_series(db_session, [1])
    series.mangaupdates_id = None
    series.chapters[0].available_sources = "fake"
    await db_session.commit()
    monkeypatch.setattr(tasks.registry, "enabled_direct_sources", lambda values: [])

    await update_chapters(db_session, series, {})

    assert series.chapters[0].available_sources == ""


async def test_new_torrent_coverage_allows_uncovered_direct_grabs(db_session, monkeypatch):
    series = await _make_series(db_session, [1, 2])
    source = FakeSource([1, 2])
    _use_source(monkeypatch, source)
    torrent = Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    )
    db_session.add(torrent)
    await db_session.commit()

    queued = await grab_missing_chapters(
        db_session,
        series,
        {},
        exclude_numbers={1.0},
        allowed_series_download_id=torrent.id,
    )

    assert queued == 1
    direct = (
        await db_session.execute(
            select(Download).where(Download.kind == DownloadKind.DIRECT)
        )
    ).scalar_one()
    assert direct.chapter_id == next(ch.id for ch in series.chapters if ch.number == 2.0)


async def test_auto_torrent_grab_returns_inspected_coverage(db_session, tmp_path, monkeypatch):
    series = await _make_series(db_session, [1, 2.5])
    series.root_folder = RootFolder(path=str(tmp_path))
    await db_session.commit()
    release = TorrentRelease(
        source_name="nyaa",
        title="Test Series pack",
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        size_bytes=1024,
        seeders=4,
    )
    captured = {}

    async def fake_select(*args, **kwargs):
        captured["max_size"] = kwargs["max_size_bytes"]
        captured["min_seeders"] = kwargs["min_seeders"]
        return TorrentSelection(release=release, coverage={1.0, 2.5})

    async def fake_enqueue(session, selected_series, magnet, title, values):
        captured["enqueued"] = (selected_series.id, magnet, title)
        return SimpleNamespace(id=321)

    monkeypatch.setattr(tasks, "select_best_torrent", fake_select)
    monkeypatch.setattr(tasks, "enqueue_torrent", fake_enqueue)
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [object()])

    grab = await auto_grab_best_torrent(
        db_session,
        series,
        {
            "qbittorrent_enabled": "true",
            "torrent_auto_max_size_gib": "30",
            "torrent_auto_min_seeders": "1",
        },
    )

    assert grab is not None
    assert grab.download_id == 321
    assert grab.coverage == {1.0, 2.5}
    assert captured["max_size"] == 30 * 1024**3
    assert captured["min_seeders"] == 1
    assert captured["enqueued"] == (series.id, release.magnet, release.title)


async def test_auto_torrent_skips_when_series_download_is_active(
    db_session, tmp_path, monkeypatch
):
    series = await _make_series(db_session, [1])
    series.root_folder = RootFolder(path=str(tmp_path))
    db_session.add(Download(
        series_id=series.id,
        chapter_id=None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        source_name="nyaa",
    ))
    await db_session.commit()
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [object()])

    async def unexpected_select(*args, **kwargs):
        raise AssertionError("selection must not run while a series torrent is active")

    monkeypatch.setattr(tasks, "select_best_torrent", unexpected_select)

    assert await auto_grab_best_torrent(
        db_session, series, {"qbittorrent_enabled": "true"}
    ) is None


async def test_active_direct_download_removed_by_user_is_not_completed(
    db_session, tmp_path, monkeypatch
):
    root = RootFolder(path=str(tmp_path))
    series = Series(
        title="Test Series",
        sort_title="test series",
        root_folder=root,
        folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True)
    series.chapters.append(chapter)
    db_session.add(series)
    await db_session.commit()
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        source_name="fake",
        payload="c1",
    )
    db_session.add(dl)
    await db_session.commit()
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource([1]))

    async def fake_download(source, payload, series, chapter, dest, progress_cb=None,
                            cancel_cb=None, web_url="", optimization=None):
        dl.status = DownloadStatus.FAILED
        dl.error = REMOVED_BY_USER
        await db_session.commit()
        if cancel_cb is not None:
            await cancel_cb()
        raise AssertionError("cancel callback should abort the download")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    await db_session.refresh(chapter)

    assert dl.status == DownloadStatus.FAILED
    assert dl.error == REMOVED_BY_USER
    assert chapter.downloaded is False


async def test_queued_direct_download_is_stopped_after_chapter_exclusion(
    db_session, tmp_path, monkeypatch
):
    root = RootFolder(path=str(tmp_path))
    series = Series(
        title="Test Series",
        sort_title="test series",
        root_folder=root,
        folder_name="Test Series",
    )
    chapter = Chapter(number=1.0, monitored=True, excluded=True)
    series.chapters.append(chapter)
    db_session.add(series)
    await db_session.commit()
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        source_name="fake",
        payload="c1",
    )
    db_session.add(dl)
    await db_session.commit()
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "fake", FakeSource([1]))

    await tasks._run_direct_download(db_session, dl)

    assert dl.status == DownloadStatus.FAILED
    assert "excluded" in dl.error
    assert chapter.downloaded is False


class PageSource(FakeSource):
    def __init__(self, name, page):
        super().__init__([12, 13])
        self.name = name
        self.page = page
        self.releases = [
            SourceChapter(name, "c12", 12),
            SourceChapter(name, "c13", 13),
        ]
        self.reader_calls = []

    async def list_chapters(self, external_id):
        self.list_calls += 1
        return self.releases

    async def get_pages(self, chapter_external_id):
        self.reader_calls.append(chapter_external_id)
        return ["https://fixture.invalid/page.jpg"]

    async def download_page(self, client, url):
        return self.page


@pytest.fixture
async def duplicate_release(db_session, monkeypatch):
    series = await _make_series(db_session, [11, 12, 13])
    existing, chapter, _ = series.chapters
    existing_path = Path(series.root_folder.path) / "chapter-11.cbz"
    original_page = b"\xff\xd8\xff\xe0chapter 11"
    write_cbz(existing_path, [original_page], "<ComicInfo/>")
    existing.downloaded = True
    existing.file_path = str(existing_path)

    def register_source(name, page=original_page, *, linked=True):
        source = PageSource(name, page)
        monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, name, source)
        monkeypatch.setitem(settings_service.DEFAULTS, f"source_{name}_enabled", "true")
        if linked:
            series.source_links.append(SeriesSourceLink(source_name=name, external_id="x"))
        return source

    primary = register_source("fake", linked=False)  # already linked by _make_series
    alternative = register_source("alternative", b"\xff\xd8\xff\xe0chapter 12")
    dl = await tasks.enqueue_direct(db_session, series, chapter, primary.name, "c12")
    return series, chapter, dl, primary, alternative, register_source


@pytest.mark.parametrize("verification_error", [False, True])
async def test_duplicate_release_queues_and_imports_one_verified_fallback(
    db_session, duplicate_release, monkeypatch, verification_error,
):
    series, chapter, dl, primary, alternative, register_source = duplicate_release
    blocked = register_source("blocked")
    unlinked = register_source("unlinked", linked=False)
    catalog = register_source("catalog")
    later = register_source("later")
    series.blocked_sources = blocked.name
    series.source_priority = "blocked,unlinked,catalog,alternative,later"
    monkeypatch.setitem(settings_service.DEFAULTS, "source_priority", "later,alternative")
    series.preferred_groups = "Preferred"
    series.blocked_groups = "Banned"
    chapter.monitored = False  # the original grab can be explicit
    alternative.releases[:1] = [
        SourceChapter(alternative.name, "banned", 12, group="Banned"),
        SourceChapter(alternative.name, "ordinary", 12, group="Ordinary"),
        SourceChapter(alternative.name, "preferred", 12, group="Preferred"),
    ]
    checked = []

    async def unverified(ids):
        checked.append((catalog.name, ids))
        if verification_error:
            raise RuntimeError("reader unavailable")
        return set()

    async def verified(ids):
        checked.append((alternative.name, ids))
        return set(ids)

    catalog.requires_chapter_verification = alternative.requires_chapter_verification = True
    monkeypatch.setattr(catalog, "verify_chapters", unverified)
    monkeypatch.setattr(alternative, "verify_chapters", verified)
    await db_session.commit()

    await tasks._run_direct_download(db_session, dl)

    items = await queue.get_queue(db_session)
    assert [(item.source_name, item.status) for item in items] == [
        (primary.name, DownloadStatus.FAILED), (alternative.name, DownloadStatus.QUEUED),
    ]
    assert "chapter 12 has the same ordered page images as chapter 11" in items[0].error
    fallback = await db_session.get(Download, items[1].id)
    assert (fallback.chapter_id, fallback.payload, fallback.release_group) == (
        chapter.id, "preferred", "Preferred",
    )
    assert checked == [(catalog.name, ["c12"]), (alternative.name, ["preferred"])]
    assert primary.list_calls == blocked.list_calls == unlinked.list_calls == later.list_calls == 0
    events = await queue.get_history(session=db_session)
    assert [(ev.event, ev.source_name) for ev in events] == [
        ("grabbed", alternative.name), ("duplicate_rejected", primary.name),
        ("grabbed", primary.name),
    ]
    assert events[1].detail == items[0].error
    assert not chapter.downloaded and not chapter.excluded and not chapter.monitored

    # Retrying the failed Activity entry while the fallback is queued should
    # record the duplicate again without adding another fallback.
    await queue.retry_failed_download(items[0].id, db_session)
    await tasks._run_direct_download(db_session, dl)
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(tasks, "session_scope", maker)
    await tasks.process_direct_queue()

    await db_session.refresh(fallback)
    await db_session.refresh(chapter)
    assert fallback.status == DownloadStatus.DONE
    assert chapter.downloaded and chapter.file_source == alternative.name
    assert Path(chapter.file_path).is_file()
    assert primary.reader_calls == ["c12", "c12"]
    assert alternative.reader_calls == ["preferred"]
    assert len((await db_session.scalars(select(Download))).all()) == 2


@pytest.mark.parametrize("unusable", ["unlinked", "unverified", "cooldown", "blocked group"])
async def test_exhausted_duplicate_fallback_remains_retryable(
    db_session, duplicate_release, monkeypatch, unusable,
):
    series, chapter, dl, primary, alternative, _ = duplicate_release
    if unusable == "unlinked":
        series.source_links.pop()
    elif unusable == "unverified":
        alternative.requires_chapter_verification = True

        async def verify(ids):
            return set()

        monkeypatch.setattr(alternative, "verify_chapters", verify)
    elif unusable == "cooldown":
        db_session.add(Download(
            series_id=series.id, chapter_id=chapter.id, kind=DownloadKind.DIRECT,
            source_name=alternative.name, status=DownloadStatus.FAILED, error="no pages",
        ))
    else:
        series.blocked_groups = "Banned"
        alternative.releases[0].group = "Banned"
    await db_session.commit()

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)

    assert dl.status == DownloadStatus.FAILED
    assert "duplicate pages rejected" in dl.error
    assert not dl.transient_error
    assert not chapter.excluded and not chapter.downloaded and chapter.monitored
    assert len(list(Path(series.root_folder.path).rglob("*.cbz"))) == 1
    assert all(item.status == DownloadStatus.FAILED for item in await queue.get_queue(db_session))
    assert primary.list_calls == 0
    values = await tasks.registry.apply_settings(db_session)
    assert await grab_missing_chapters(db_session, series, values, exclude_numbers={13}) == 0
    assert (await queue.retry_failed_download(dl.id, db_session)).status == DownloadStatus.QUEUED


async def test_duplicate_fallback_does_not_cycle_between_sources(
    db_session, duplicate_release, monkeypatch,
):
    _, chapter, dl, primary, alternative, _ = duplicate_release
    alternative.page = primary.page
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(tasks, "session_scope", maker)

    await tasks.process_direct_queue()

    await db_session.refresh(dl)
    items = await queue.get_queue(db_session)
    assert [(item.source_name, item.status) for item in items] == [
        (primary.name, DownloadStatus.FAILED), (alternative.name, DownloadStatus.FAILED),
    ]
    assert all("duplicate pages rejected" in item.error for item in items)
    assert primary.reader_calls == alternative.reader_calls == ["c12"]
    await db_session.refresh(chapter)
    assert not chapter.excluded and not chapter.downloaded and chapter.monitored


@pytest.mark.parametrize(
    "interruption", ["excluded", "queued", "downloading", "series torrent", "dismissed"],
)
async def test_duplicate_fallback_rechecks_after_verification(
    db_session, duplicate_release, monkeypatch, interruption,
):
    series, chapter, dl, _, alternative, _ = duplicate_release
    series_id, chapter_id, dl_id = series.id, chapter.id, dl.id
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    alternative.requires_chapter_verification = True

    async def verify(ids):
        async with maker() as other:
            if interruption == "excluded":
                current = await other.get(Chapter, chapter_id)
                current.excluded = True
                await other.commit()
            elif interruption == "dismissed":
                await queue.remove_from_queue(dl_id, other)
            else:
                torrent = interruption == "series torrent"
                other.add(Download(
                    series_id=series_id, chapter_id=None if torrent else chapter_id,
                    kind=DownloadKind.TORRENT if torrent else DownloadKind.DIRECT,
                    source_name="other",
                    status=DownloadStatus.IMPORTING if torrent else DownloadStatus(interruption),
                ))
                await other.commit()
        return set(ids)

    monkeypatch.setattr(alternative, "verify_chapters", verify)
    await tasks._run_direct_download(db_session, dl)

    assert await db_session.scalar(
        select(Download.id).where(Download.source_name == alternative.name)
    ) is None
    await db_session.refresh(chapter)
    await db_session.refresh(dl)
    assert chapter.excluded == (interruption == "excluded")
    if interruption == "dismissed":
        assert dl.error == REMOVED_BY_USER
    assert any(ev.event == "duplicate_rejected" for ev in await queue.get_history(session=db_session))


@pytest.mark.parametrize("failure", ["write", "naming", "cancelled"])
async def test_only_duplicate_rejections_try_another_source(
    db_session, duplicate_release, monkeypatch, failure,
):
    _, chapter, dl, primary, alternative, _ = duplicate_release
    primary.page = alternative.page  # distinct pages reach the archive writer
    dl_id = dl.id
    if failure == "write":
        def fail_write(*args):
            raise OSError("disk full")

        monkeypatch.setattr(direct, "write_cbz", fail_write)
        expected = "disk full"
    elif failure == "naming":
        monkeypatch.setitem(settings_service.DEFAULTS, "naming_template_no_volume", "{invalid}")
        expected = "invalid"
    else:
        maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

        async def cancel_after_fetch(client, url):
            async with maker() as other:
                await queue.remove_from_queue(dl_id, other)
            return primary.page

        monkeypatch.setattr(primary, "download_page", cancel_after_fetch)
        expected = REMOVED_BY_USER

    await tasks._run_direct_download(db_session, dl)
    await db_session.refresh(dl)
    await db_session.refresh(chapter)

    assert dl.status == DownloadStatus.FAILED and expected in dl.error
    assert not chapter.downloaded and not chapter.excluded
    assert alternative.list_calls == 0
    assert (await db_session.scalars(select(Download))).all() == [dl]


async def test_catalog_only_source_does_not_claim_availability_or_queue_unverified_chapters(
    db_session, monkeypatch,
):
    series = await _make_series(db_session, [1])
    series.chapters[0].available_sources = "fake"
    await db_session.commit()
    source = FakeSource([1, 2])
    source.requires_chapter_verification = True
    checked = []

    async def verify(ids):
        checked.extend(ids)
        return {"c2"}

    monkeypatch.setattr(source, "verify_chapters", verify)
    _use_source(monkeypatch, source)
    cache = {}
    await update_chapters(db_session, series, {}, cache)
    assert all(ch.available_sources is None for ch in series.chapters)
    assert not checked

    assert await grab_missing_chapters(db_session, series, {}, chapter_cache=cache) == 1
    assert set(checked) == {"c1", "c2"}
    queued = (await db_session.execute(select(Download))).scalar_one()
    assert queued.payload == "c2"
