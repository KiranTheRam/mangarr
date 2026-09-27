"""Per-series automation: monitoring modes, source overrides, scanlation
group preferences, upgrades, volume merging, and finished-series handling —
against a real (in-memory) database where the monitor loop is involved."""

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from mangarr import automation, db as db_module
from mangarr.api.series import update_series
from mangarr.jobs import tasks
from mangarr.library.rename import RenameItem, apply_renames
from mangarr.library.volume_merge import merge_chapter_archives
from mangarr.models import (
    Base,
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    RootFolder,
    Series,
    SeriesSourceLink,
    SeriesStatus,
)
from mangarr.schemas import SeriesUpdateIn
from mangarr.sources import registry
from mangarr.sources.base import DirectSource, SourceChapter

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class ListingSource(DirectSource):
    """Serves a fixed listing: {number: [group, …]} — one copy per group."""

    def __init__(self, name, listing):
        self.name = name
        self.listing = listing

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return [
            SourceChapter(
                source_name=self.name, external_id=f"{self.name}-{n:g}-{group}",
                number=float(n), group=group,
            )
            for n, groups in sorted(self.listing.items())
            for group in groups
        ]

    async def get_pages(self, chapter_external_id):
        return []


def _values(*names):
    values = {"source_priority": ",".join(names)}
    for name in names:
        values[f"source_{name}_enabled"] = "true"
    return values


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _load(session, series_id):
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _series(session, chapters=(), links=(), **fields):
    series = Series(title="Test Series", sort_title="test series", **fields)
    for name in links:
        series.source_links.append(SeriesSourceLink(source_name=name, external_id="x"))
    series.chapters.extend(chapters)
    session.add(series)
    await session.commit()
    return await _load(session, series.id)


async def _queued(session):
    rows = await session.execute(select(Download).order_by(Download.id))
    return [(d.chapter_id, d.source_name, d.release_group, d.title) for d in rows.scalars()]


# ------------------------------------------------------------ monitoring

def _chapter(number, volume=None, downloaded=False, **fields):
    return Chapter(number=float(number), volume=volume, downloaded=downloaded,
                   monitored=True, excluded=False, **fields)


def test_monitor_modes_decide_which_chapters_are_wanted():
    chapters = [_chapter(1, 1, downloaded=True), _chapter(2, 1), _chapter(3, 2), _chapter(4)]

    def monitored(mode, monitor_from=None, monitored=True):
        series = Series(monitored=monitored, monitor_mode=mode, monitor_from=monitor_from)
        automation.apply_monitor_mode(series, chapters)
        return [c.number for c in chapters if c.monitored]

    assert monitored("all") == [1, 2, 3, 4]
    assert monitored("missing") == [2, 3, 4]
    assert monitored("future") == []  # nothing newer than chapter 4 yet
    assert monitored("from_chapter", 3) == [3, 4]
    # latest volume (2) starts at chapter 3; uncollected chapter 4 follows it
    assert monitored("latest_volume") == [3, 4]
    assert monitored("none") == []
    assert monitored("all", monitored=False) == []


def test_future_mode_monitors_only_chapters_after_the_threshold():
    series = Series(monitored=True, monitor_mode="future", monitor_from=10.0)
    assert not automation.chapter_is_wanted(series, 10.0)
    assert automation.chapter_is_wanted(series, 10.5)
    # unresolved threshold: conservative until the chapter list is known
    series.monitor_from = None
    assert not automation.chapter_is_wanted(series, 99.0)


def test_latest_volume_without_volume_data_starts_at_newest_chapter():
    series = Series(monitored=True, monitor_mode="latest_volume")
    chapters = [_chapter(1), _chapter(2), _chapter(3)]
    automation.apply_monitor_mode(series, chapters)
    assert series.monitor_from == 3.0
    assert [c.monitored for c in chapters] == [False, False, True]


async def test_future_mode_chosen_at_add_time_resolves_on_first_sync(
    db_session, monkeypatch
):
    source = ListingSource("alpha", {1: [""], 2: [""], 3: [""]})
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {"alpha": source})
    series = await _series(db_session, links=["alpha"], monitored=True, monitor_mode="future")
    await tasks.update_chapters(db_session, series, _values("alpha"))
    assert series.monitor_from == 3.0
    assert not any(c.monitored for c in series.chapters)
    # a chapter released later is past the threshold
    source.listing[4] = [""]
    await tasks.update_chapters(db_session, series, _values("alpha"))
    assert {c.number: c.monitored for c in series.chapters} == {
        1.0: False, 2.0: False, 3.0: False, 4.0: True,
    }


async def test_update_series_applies_mode_and_requires_threshold(db_session):
    series = await _series(
        db_session, chapters=[_chapter(1), _chapter(2), _chapter(3)], monitored=True,
    )
    out = await update_series(
        series.id, SeriesUpdateIn(monitor_mode="from_chapter", monitor_from=2), db_session
    )
    assert out.monitor_mode == "from_chapter"
    assert [c.monitored for c in sorted(out.chapters, key=lambda c: c.number)] == [
        False, True, True,
    ]
    # unmonitoring clears every flag; re-monitoring re-applies the mode
    await update_series(series.id, SeriesUpdateIn(monitored=False), db_session)
    out = await update_series(series.id, SeriesUpdateIn(monitored=True), db_session)
    assert [c.monitored for c in sorted(out.chapters, key=lambda c: c.number)] == [
        False, True, True,
    ]


async def test_from_chapter_mode_needs_a_threshold(db_session):
    series = await _series(db_session, chapters=[_chapter(1)])
    with pytest.raises(HTTPException) as err:
        await update_series(series.id, SeriesUpdateIn(monitor_mode="from_chapter"), db_session)
    assert err.value.status_code == 422


async def test_update_series_validates_source_names(db_session):
    series = await _series(db_session)
    with pytest.raises(HTTPException):
        await update_series(series.id, SeriesUpdateIn(source_priority=["nope"]), db_session)
    out = await update_series(
        series.id,
        SeriesUpdateIn(source_priority=["asura", "mangadex"], blocked_sources=["weebcentral"],
                       preferred_groups=["Group A", " Group  B ", "Group A"]),
        db_session,
    )
    assert out.source_priority == ["asura", "mangadex"]
    assert out.blocked_sources == ["weebcentral"]
    assert out.preferred_groups == ["Group A", "Group B"]
    assert "weebcentral" not in out.effective_source_order


# ---------------------------------------------------------- source order

def test_series_source_override_and_blocking():
    series = Series(source_priority="gamma,alpha", blocked_sources="beta")
    assert automation.order_sources(["alpha", "beta", "gamma", "delta"], series) == [
        "gamma", "alpha", "delta",
    ]
    assert automation.order_sources(["alpha", "beta"], Series()) == ["alpha", "beta"]


async def test_grab_follows_series_order_and_skips_blocked_source(db_session, monkeypatch):
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {
        "alpha": ListingSource("alpha", {1: [""], 2: [""]}),
        "beta": ListingSource("beta", {1: [""], 2: [""], 3: [""]}),
        "gamma": ListingSource("gamma", {3: [""]}),
    })
    series = await _series(
        db_session, chapters=[_chapter(1), _chapter(2), _chapter(3)],
        links=["alpha", "beta", "gamma"], source_priority="beta", blocked_sources="gamma",
    )
    await tasks.grab_missing_chapters(db_session, series, _values("alpha", "beta", "gamma"))
    assert [source for _, source, _, _ in await _queued(db_session)] == ["beta", "beta", "beta"]


# ---------------------------------------------------- scanlation groups

def test_group_selection_prefers_blocks_and_matches_joint_releases():
    listing = [
        SourceChapter("s", "1a", 1.0, group="Alpha"),
        SourceChapter("s", "1b", 1.0, group="Beta & Gamma"),
        SourceChapter("s", "2a", 2.0, group="Alpha"),
        SourceChapter("s", "3a", 3.0, group="Delta"),
    ]
    assert [c.external_id for c in automation.select_group_variants(listing)] == [
        "1a", "2a", "3a",
    ]
    picked = automation.select_group_variants(listing, preferred=["gamma"], blocked=["delta"])
    # joint release matches its member; the blocked group's only copy is dropped
    assert [c.external_id for c in picked] == ["1b", "2a"]


async def test_grab_uses_preferred_group_and_records_it(db_session, monkeypatch):
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {
        "alpha": ListingSource("alpha", {1: ["Fast Scans", "Good Scans"]}),
    })
    series = await _series(db_session, chapters=[_chapter(1)], links=["alpha"],
                           preferred_groups="good scans")
    await tasks.grab_missing_chapters(db_session, series, _values("alpha"))
    [(_, source, group, title)] = await _queued(db_session)
    assert (source, group) == ("alpha", "Good Scans")
    assert "[Good Scans]" in title


# --------------------------------------------------------------- upgrades

async def test_upgrade_replaces_lower_priority_download(db_session, monkeypatch):
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {
        "alpha": ListingSource("alpha", {1: [""], 2: [""], 3: [""]}),
        "beta": ListingSource("beta", {1: [""], 2: [""], 3: [""]}),
    })
    chapters = [
        # mangarr's own download from the lower-priority source: upgraded
        _chapter(1, downloaded=True, file_path="/lib/c1.cbz", file_source="beta"),
        # already from the best source: left alone
        _chapter(2, downloaded=True, file_path="/lib/c2.cbz", file_source="alpha"),
        # adopted from disk (no provenance): never replaced
        _chapter(3, downloaded=True, file_path="/lib/c3.cbz"),
    ]
    series = await _series(db_session, chapters=chapters, links=["alpha", "beta"],
                           upgrades_enabled=True)
    await tasks.grab_missing_chapters(db_session, series, _values("alpha", "beta"))
    queued = await _queued(db_session)
    c1 = next(c for c in series.chapters if c.number == 1.0)
    assert [(chapter_id, source) for chapter_id, source, _, _ in queued] == [(c1.id, "alpha")]
    assert queued[0][3].endswith("(upgrade)")


async def test_upgrades_respect_cutoff_and_toggle(db_session, monkeypatch):
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {
        name: ListingSource(name, {1: [""]}) for name in ("alpha", "beta", "gamma")
    })
    chapter = _chapter(1, downloaded=True, file_path="/lib/c1.cbz", file_source="beta")
    series = await _series(db_session, chapters=[chapter], links=["alpha", "beta", "gamma"],
                           upgrades_enabled=True, upgrade_cutoff="beta")
    values = _values("alpha", "beta", "gamma")
    await tasks.grab_missing_chapters(db_session, series, values)
    assert await _queued(db_session) == []  # beta already meets the cutoff

    series.upgrade_cutoff = ""
    series.upgrades_enabled = False
    await tasks.grab_missing_chapters(db_session, series, values)
    assert await _queued(db_session) == []  # upgrades off


async def test_blocked_group_file_is_upgraded_even_from_same_source(db_session, monkeypatch):
    monkeypatch.setattr(registry, "DIRECT_SOURCES", {
        "alpha": ListingSource("alpha", {1: ["Bad Scans", "Fine Scans"]}),
    })
    chapter = _chapter(1, downloaded=True, file_path="/lib/c1.cbz",
                       file_source="alpha", file_group="Bad Scans")
    series = await _series(db_session, chapters=[chapter], links=["alpha"],
                           upgrades_enabled=True, blocked_groups="bad scans")
    await tasks.grab_missing_chapters(db_session, series, _values("alpha"))
    [(_, source, group, _)] = await _queued(db_session)
    assert (source, group) == ("alpha", "Fine Scans")


async def test_upgraded_download_removes_replaced_file(db_session, tmp_path, monkeypatch):
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "alpha", ListingSource("alpha", {}))
    old = tmp_path / "Test Series" / "old name.cbz"
    old.parent.mkdir()
    old.write_bytes(b"old")
    chapter = _chapter(1, downloaded=True, file_path=str(old), file_source="beta")
    monkeypatch.setitem(tasks.registry.DIRECT_SOURCES, "beta", ListingSource("beta", {}))
    series = await _series(db_session, chapters=[chapter],
                           root_folder=RootFolder(path=str(tmp_path)), folder_name="Test Series")
    dl = Download(series_id=series.id, chapter_id=series.chapters[0].id,
                  kind=DownloadKind.DIRECT, status=DownloadStatus.QUEUED,
                  source_name="alpha", payload="p", release_group="Good Scans")
    db_session.add(dl)
    await db_session.commit()

    async def fake_download(source, payload, series, chapter, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"new")

    monkeypatch.setattr(tasks, "download_chapter_to_cbz", fake_download)
    monkeypatch.setattr(tasks.registry, "apply_settings", _settings_with("alpha", "beta"))
    await tasks._run_direct_download(db_session, dl)

    chapter = (await _load(db_session, series.id)).chapters[0]
    assert dl.status == DownloadStatus.DONE
    assert (chapter.file_source, chapter.file_group) == ("alpha", "Good Scans")
    assert Path(chapter.file_path).read_bytes() == b"new"
    assert not old.exists()
    events = (await db_session.execute(select(HistoryEvent.event))).scalars().all()
    assert "upgraded" in events


def _settings_with(*names):
    async def apply_settings(session):
        from mangarr import settings_service

        values = await settings_service.get_all(session)
        values.update(_values(*names))
        return values
    return apply_settings


def test_changing_file_path_forgets_provenance_but_rename_keeps_it(tmp_path):
    chapter = Chapter(number=1.0, downloaded=True, file_path="/a.cbz")
    chapter.file_source, chapter.file_group = "alpha", "Group"
    chapter.file_path = "/a.cbz"  # same file: kept
    assert chapter.file_source == "alpha"
    chapter.file_path = "/b.cbz"  # another file: forgotten
    assert (chapter.file_source, chapter.file_group) == ("", "")

    src = tmp_path / "a.cbz"
    src.write_bytes(b"x")
    chapter = Chapter(id=1, number=1.0, downloaded=True, file_path=str(src))
    chapter.file_source = "alpha"
    apply_renames(
        [RenameItem(current_path=str(src), new_path=str(tmp_path / "b.cbz"), chapter_ids=[1])],
        {1: chapter},
    )
    assert chapter.file_path == str(tmp_path / "b.cbz")
    assert chapter.file_source == "alpha"


async def test_backfill_recovers_provenance_from_import_history(db_session):
    series = await _series(db_session, chapters=[
        _chapter(1, downloaded=True, file_path="/lib/c1.cbz"),
        _chapter(2, downloaded=True, file_path="/lib/c2.cbz"),
    ])
    c1, c2 = sorted(series.chapters, key=lambda c: c.number)
    db_session.add_all([
        HistoryEvent(series_id=series.id, chapter_id=c1.id, event="imported",
                     source_name="mangadex", detail="/lib/c1.cbz"),
        # an import whose file has since been replaced by something else
        HistoryEvent(series_id=series.id, chapter_id=c2.id, event="imported",
                     source_name="mangadex", detail="/lib/elsewhere.cbz"),
    ])
    await db_session.commit()
    await db_session.execute(text(db_module._BACKFILL_FILE_SOURCE))
    await db_session.commit()
    rows = dict((await db_session.execute(
        select(Chapter.number, Chapter.file_source)
    )).all())
    assert rows == {1.0: "mangadex", 2.0: ""}


# ---------------------------------------------------------- volume merges

def _cbz(path: Path, pages: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo/>")
        for page in range(1, pages + 1):
            archive.writestr(f"{page:03d}.png", PNG)
    return path


def test_merge_writes_pages_in_chapter_order(tmp_path):
    c2 = _cbz(tmp_path / "c2.cbz", 1)
    c1 = _cbz(tmp_path / "c1.cbz", 2)
    dest = tmp_path / "Series - Vol. 01.cbz"
    assert merge_chapter_archives([(2.0, c2), (1.0, c1)], dest, "Series", 1) == 3
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
        assert names[:3] == ["0001-0001.png", "0001-0002.png", "0002-0001.png"]
        assert b"<Volume>1</Volume>" in archive.read("ComicInfo.xml")
    with pytest.raises(FileExistsError):
        merge_chapter_archives([(1.0, c1)], dest, "Series", 1)


def test_mergeable_volumes_need_closed_complete_mangarr_volumes():
    def ch(n, volume, source="alpha", downloaded=True, cid=None):
        return Chapter(id=cid or int(n * 10), number=float(n), volume=volume, excluded=False,
                       downloaded=downloaded, file_path=f"/lib/{n}.cbz" if downloaded else "",
                       file_source=source)

    series = Series(status=SeriesStatus.RELEASING)
    chapters = [
        ch(1, 1), ch(2, 1),                    # complete, closed → merge
        ch(3, 2), ch(4, 2, source=""),         # has a disk-adopted file → skip
        ch(5, 3), ch(6, 3, downloaded=False),  # incomplete → skip
        ch(7, 4),                              # latest volume, still open → skip
    ]
    assert list(automation.mergeable_volumes(series, chapters)) == [1]
    # a chapter still downloading holds its volume back
    assert automation.mergeable_volumes(series, chapters, busy_chapter_ids={10}) == {}
    series.status = SeriesStatus.FINISHED
    assert list(automation.mergeable_volumes(series, chapters)) == [1, 4]


async def test_merge_complete_volumes_packs_and_repoints(db_session, tmp_path):
    folder = tmp_path / "Test Series"
    files = [_cbz(folder / f"Test Series - Ch. {n:04d}.cbz", 1) for n in (1, 2, 3)]
    chapters = []
    for n, (volume, path) in enumerate(zip((1, 1, 2), files), start=1):
        c = _chapter(n, volume, downloaded=True, file_path=str(path))
        c.file_source = "alpha"
        chapters.append(c)
    series = await _series(db_session, chapters=chapters, merge_volumes=True,
                           root_folder=RootFolder(path=str(tmp_path)), folder_name="Test Series")

    merged = await tasks.merge_complete_volumes(db_session, series, {})
    assert merged == [1]
    series = await _load(db_session, series.id)
    by_number = {c.number: c for c in series.chapters}
    dest = folder / "Test Series - Vol. 01.cbz"
    assert dest.exists()
    assert by_number[1.0].file_path == by_number[2.0].file_path == str(dest)
    assert by_number[1.0].file_source == automation.MERGED_FILE_SOURCE
    assert not files[0].exists() and not files[1].exists()
    assert files[2].exists()  # volume 2 is the latest and still open
    # merged chapters share a file, so they are never upgrade candidates
    series.upgrades_enabled = True
    assert automation.upgrade_candidates(series, series.chapters, ["alpha"], ["alpha"]) == {}


# --------------------------------------------------------- finished series

def test_finished_and_complete():
    series = Series(status=SeriesStatus.FINISHED)
    done = _chapter(1, downloaded=True)
    missing_unmonitored = _chapter(2)
    missing_unmonitored.monitored = False
    assert automation.finished_and_complete(series, [done, missing_unmonitored])
    assert not automation.finished_and_complete(series, [done, _chapter(3)])
    assert not automation.finished_and_complete(Series(status=SeriesStatus.RELEASING), [done])
    assert not automation.finished_and_complete(series, [missing_unmonitored])


async def _run_monitor(db_session, monkeypatch, settings):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        yield db_session

    calls = []

    async def fake_update(session, series, values, cache=None):
        calls.append(series.id)
        return 0

    async def no_op(*args, **kwargs):
        return 0

    async def apply_settings(session):
        from mangarr import settings_service

        values = await settings_service.get_all(session)
        values.update(settings)
        return values

    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "update_chapters", fake_update)
    monkeypatch.setattr(tasks, "link_sources", no_op)
    monkeypatch.setattr(tasks, "scan_series_folder", no_op)
    monkeypatch.setattr(tasks, "grab_missing_chapters", no_op)
    monkeypatch.setattr(tasks.registry, "apply_settings", apply_settings)
    await tasks.monitor_all()
    return calls


async def test_slow_mode_skips_recently_checked_complete_series(db_session, monkeypatch):
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    now = datetime.now(timezone.utc)
    done = await _series(db_session, chapters=[_chapter(1, downloaded=True)],
                         status=SeriesStatus.FINISHED, last_monitored_at=recent,
                         metadata_refreshed_at=now)
    ongoing = await _series(db_session, chapters=[_chapter(1, downloaded=True)],
                            status=SeriesStatus.RELEASING, last_monitored_at=recent,
                            metadata_refreshed_at=now)
    calls = await _run_monitor(db_session, monkeypatch, {"finished_series_mode": "slow"})
    assert calls == [ongoing.id]

    done.last_monitored_at = now - timedelta(days=8)
    await db_session.commit()
    calls = await _run_monitor(db_session, monkeypatch, {"finished_series_mode": "slow"})
    assert sorted(calls) == sorted([done.id, ongoing.id])


async def test_unmonitor_mode_unmonitors_finished_complete_series(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    series = await _series(db_session, chapters=[_chapter(1, downloaded=True)],
                           status=SeriesStatus.FINISHED, metadata_refreshed_at=now)
    await _run_monitor(db_session, monkeypatch, {"finished_series_mode": "unmonitor"})
    series = await _load(db_session, series.id)
    assert series.monitored is False
    assert series.last_monitored_at is not None
    events = (await db_session.execute(select(HistoryEvent.event))).scalars().all()
    assert events == ["unmonitored"]
