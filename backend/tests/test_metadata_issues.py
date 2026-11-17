import asyncio
import os
import threading
import zipfile
from pathlib import Path

from fastapi import HTTPException

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr import comicinfo, db as db_module, metadata_issues
from mangarr.api import issues as issues_api, library as library_api, series as series_api
from mangarr.comicinfo import ArchiveChanged, ComicInfoError, read_comicinfo, update_comicinfo
from mangarr.jobs import tasks
from mangarr.schemas import ChapterMetadataIn, ComicInfoUpdateIn, IssueResolveIn
from mangarr.metadata_issues import detect_series_issues, find_candidates
from mangarr.models import Base, Chapter, LibraryIssue, RootFolder, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def archive(path: Path, xml: str | None, pages: int = 1):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pages/", b"")
        if xml is not None:
            zf.writestr("ComicInfo.xml", xml)
        for number in range(1, pages + 1):
            zf.writestr(f"{number:03}.png", PNG + bytes([number]))


def test_comicinfo_editor_preserves_pages_and_unknown_metadata(tmp_path):
    path = tmp_path / "chapter.cbz"
    archive(
        path,
        "<ComicInfo><Series>Old</Series><Number>92.1</Number><Volume>10</Volume>"
        "<Writer>Someone</Writer><PageCount>1</PageCount></ComicInfo>",
        pages=2,
    )
    os.chmod(path, 0o640)

    update_comicinfo(path, {"series": "Kagurabachi", "volume": None, "page_count": 2})

    info = read_comicinfo(path)
    assert info.series == "Kagurabachi"
    assert info.number == "92.1"
    assert info.volume is None
    assert info.page_count == "2" and info.actual_page_count == 2
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    with zipfile.ZipFile(path) as zf:
        assert sorted(name for name in zf.namelist() if name.endswith(".png")) == [
            "001.png", "002.png"
        ]
        assert b"<Writer>Someone</Writer>" in zf.read("ComicInfo.xml")


def test_detects_isolated_volume_tag_and_recommends_removing_it(tmp_path):
    tagged = tmp_path / "Kagurabachi - Ch. 0092.1.cbz"
    plain = tmp_path / "Kagurabachi - Ch. 0092.cbz"
    archive(
        tagged,
        "<ComicInfo><Series>Kagurabachi</Series><Number>92.1</Number>"
        "<Volume>10</Volume><PageCount>1</PageCount></ComicInfo>",
    )
    archive(plain, None)
    series = Series(id=1, title="Kagurabachi", provider_title="Kagurabachi", alt_titles="")
    series.chapters = [
        Chapter(id=1, number=92.0, volume=10, downloaded=True, file_path=str(plain)),
        Chapter(
            id=2, number=92.1, volume=10, volume_source="disk-inferred",
            downloaded=True, file_path=str(tagged),
        ),
    ]

    series.root_folder = RootFolder(path=str(tmp_path.parent))
    series.folder_name = tmp_path.name
    issues = find_candidates(series, [(ch.id, ch.file_path) for ch in series.chapters])

    issue = next(item for item in issues if item.field == "volume")
    assert issue.kind == "comicinfo_volume_isolated"
    assert issue.current_value == "10" and issue.suggested_value is None
    assert "Kavita" in issue.detail


def test_detects_wrong_number_page_count_and_authoritative_volume(tmp_path):
    path = tmp_path / "Series Ch. 3.cbz"
    archive(
        path,
        "<ComicInfo><Series>Wrong Series</Series><Number>4</Number>"
        "<Volume>9</Volume><PageCount>99</PageCount></ComicInfo>",
        pages=2,
    )
    series = Series(id=1, title="Series", provider_title="Series", alt_titles="")
    series.chapters = [Chapter(
        id=3, number=3.0, volume=1, volume_source="viz",
        downloaded=True, file_path=str(path),
    )]

    series.root_folder = RootFolder(path=str(tmp_path.parent))
    series.folder_name = tmp_path.name
    issues = {item.field: item for item in find_candidates(series, [(3, str(path))])}

    assert issues["number"].suggested_value == "3"
    assert issues["page_count"].suggested_value == "2"
    assert issues["series"].suggested_value == "Series"
    assert issues["volume"].suggested_value == "1"


@pytest.mark.asyncio
async def test_ignore_survives_redetection_and_resolved_issue_is_deleted(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    path = tmp_path / "Series Ch. 1.cbz"
    archive(path, "<ComicInfo><Series>Series</Series><Number>2</Number></ComicInfo>")
    async with maker() as session:
        series = Series(
            title="Series", provider_title="Series", alt_titles="",
            root_folder=RootFolder(path=str(tmp_path.parent)), folder_name=tmp_path.name,
        )
        series.chapters = [Chapter(number=1.0, downloaded=True, file_path=str(path))]
        session.add(series)
        await session.commit()

        found = await detect_series_issues(session, series)
        found[0].ignored = True
        await session.commit()
        found_again = await detect_series_issues(session, series)
        assert found_again[0].id == found[0].id and found_again[0].ignored

        update_comicinfo(path, {"number": 1})
        await detect_series_issues(session, series)
        await session.commit()
        assert (await session.execute(select(LibraryIssue))).scalars().all() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_api_accepts_recommendation_and_removes_resolved_issue(tmp_path, monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    series_dir = tmp_path / "Kagurabachi"
    series_dir.mkdir()
    plain = series_dir / "Kagurabachi - Ch. 0092.cbz"
    tagged = series_dir / "Kagurabachi - Ch. 0092.1.cbz"
    archive(plain, None)
    archive(tagged, "<ComicInfo><Series>Kagurabachi</Series><Number>92.1</Number>"
                    "<Volume>10</Volume><PageCount>1</PageCount></ComicInfo>")

    async def no_notify(*_args):
        return None

    monkeypatch.setattr(issues_api, "_notify_reader", no_notify)
    async with maker() as session:
        series = Series(
            title="Kagurabachi", provider_title="Kagurabachi", alt_titles="",
            folder_name="Kagurabachi", root_folder=RootFolder(path=str(tmp_path)),
        )
        series.chapters = [
            Chapter(number=92.0, volume=10, downloaded=True, file_path=str(plain)),
            Chapter(number=92.1, volume=10, volume_source="disk-inferred",
                    downloaded=True, file_path=str(tagged)),
        ]
        session.add(series)
        await session.commit()
        detected = await detect_series_issues(session, series)
        await session.commit()
        issue = next(item for item in detected if item.field == "volume")

        await issues_api.resolve_issue(issue.public_id, IssueResolveIn(revision=issue.revision), session)

        assert read_comicinfo(tagged).volume is None
        assert (await session.execute(select(LibraryIssue))).scalars().all() == []
    await engine.dispose()


@pytest.mark.parametrize(
    "compression, limits, error",
    [
        (zipfile.ZIP_BZIP2, {}, "compression"),
        (zipfile.ZIP_LZMA, {}, "compression"),
        (zipfile.ZIP_STORED, {"MAX_MEMBER_BYTES": 25}, "member size"),
        (zipfile.ZIP_DEFLATED, {"MAX_EXPANDED_BYTES": 45}, "total expanded"),
        (zipfile.ZIP_STORED, {"MAX_ZIP_ENTRIES": 1}, "entry count"),
        (zipfile.ZIP_STORED, {"MAX_ZIP_DIRECTORY_BYTES": 80}, "directory"),
    ],
)
def test_unsafe_zip_is_rejected_without_changing_any_bytes(tmp_path, monkeypatch, compression, limits, error):
    path = tmp_path / "chapter.cbz"
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name in ("001.png", "002.png"):
            zf.writestr(name, PNG + b"abcd")
    original = path.read_bytes()
    for name, value in limits.items():
        monkeypatch.setattr(comicinfo, name, value)

    for operation in (read_comicinfo, lambda path: update_comicinfo(path, {"title": "New"})):
        with pytest.raises(ComicInfoError, match=error):
            operation(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_nested_comicinfo_is_updated_in_place_and_ambiguity_is_rejected(tmp_path):
    path = tmp_path / "chapter.cbz"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Nested/cOmicInfo.xml", "<ComicInfo><Writer>Chris</Writer></ComicInfo>")
        zf.writestr("Nested/001.png", PNG)
    info = update_comicinfo(path, {"title": "New"})
    assert info.title == "New"
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["Nested/cOmicInfo.xml", "Nested/001.png"]
        assert b"<Writer>Chris</Writer>" in zf.read("Nested/cOmicInfo.xml")
        assert zf.testzip() is None
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo/>")
    original = path.read_bytes()

    with pytest.raises(ComicInfoError, match="multiple ComicInfo.xml"):
        update_comicinfo(path, {"title": "Another"})
    assert path.read_bytes() == original


@pytest.fixture
async def repair_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'issues.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    root = tmp_path / "library"
    folder = root / "Tracked"
    folder.mkdir(parents=True)
    path = folder / "Tracked Ch. 1.cbz"
    archive(path, "<ComicInfo><Series>Tracked</Series><Number>1</Number><Volume>9</Volume></ComicInfo>")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def no_notify(*_args):
        return None

    monkeypatch.setattr(issues_api, "_notify_reader", no_notify)
    monkeypatch.setattr(tasks, "_SERIES_LOCKS", {})
    monkeypatch.setattr(db_module, "SessionLocal", maker)
    async with maker() as session:
        series = Series(title="Tracked", folder_name="Tracked", root_folder=RootFolder(path=str(root)))
        series.chapters = [Chapter(
            number=1, volume=1, volume_source="viz", downloaded=True, file_path=str(path),
        )]
        session.add(series)
        await session.commit()
        yield session, maker, series, path
    await engine.dispose()


async def test_deleted_issue_identity_cannot_target_another_archive(repair_db):
    session, _, series, first = repair_db
    [old] = await detect_series_issues(session, series)
    old_id, old_revision, old_row_id = old.public_id, old.revision, old.id
    await session.commit()
    update_comicinfo(first, {"volume": 1})
    await detect_series_issues(session, series)
    await session.commit()

    second = first.with_name("Tracked Ch. 2.cbz")
    archive(second, "<ComicInfo><Number>2</Number><Volume>3</Volume></ComicInfo>")
    series.chapters.append(Chapter(number=2, downloaded=True, file_path=str(second)))
    [new] = await detect_series_issues(session, series)
    await session.commit()

    assert old_row_id == new.id  # SQLite may still reuse its internal rowid.
    assert old_id != new.public_id
    for stale_id in (old_id, str(old_row_id)):
        with pytest.raises(HTTPException) as exc:
            await issues_api.resolve_issue(stale_id, IssueResolveIn(revision=old_revision), session)
        assert exc.value.status_code == 404
    assert read_comicinfo(second).volume == "3"


async def test_recomputes_current_recommendation_before_repair(repair_db):
    session, _, series, path = repair_db
    [issue] = await detect_series_issues(session, series)
    await session.commit()
    reviewed = IssueResolveIn(revision=issue.revision)
    original = path.read_bytes()
    [chapter] = series.chapters
    await series_api.update_chapter_metadata(
        series.id, chapter.id, ChapterMetadataIn(title="", volume=2, title_locked=False, volume_locked=True),
        session,
    )

    with pytest.raises(HTTPException) as exc:
        await issues_api.resolve_issue(issue.public_id, reviewed, session)
    assert exc.value.status_code == 409
    assert path.read_bytes() == original
    assert issue.suggested_value == "2" and issue.revision != reviewed.revision
    with pytest.raises(HTTPException) as exc:
        await issues_api.resolve_issue(issue.public_id, reviewed, session)
    assert exc.value.status_code == 409
    await issues_api.resolve_issue(issue.public_id, IssueResolveIn(revision=issue.revision), session)
    assert read_comicinfo(path).volume == "2"


async def test_changed_archive_requires_a_fresh_review_even_if_issue_values_match(repair_db):
    session, _, series, path = repair_db
    [issue] = await detect_series_issues(session, series)
    await session.commit()
    reviewed = IssueResolveIn(revision=issue.revision)
    update_comicinfo(path, {"title": "External edit"})

    with pytest.raises(HTTPException) as exc:
        await issues_api.resolve_issue(issue.public_id, reviewed, session)
    assert exc.value.status_code == 409
    assert read_comicinfo(path).volume == "9"
    assert issue.revision != reviewed.revision


async def test_invalid_xml_keeps_archive_and_valid_write_runs_off_event_loop(repair_db, monkeypatch):
    session, _, series, path = repair_db
    chapter_id = series.chapters[0].id
    info = await issues_api.get_chapter_comicinfo(series.id, chapter_id, session)
    body = ComicInfoUpdateIn(
        revision=info.revision, title="Invalid\0title", series="Tracked",
        number="1", volume=1, page_count=1,
    )
    original = path.read_bytes()
    loop_thread = threading.get_ident()

    def write(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return update_comicinfo(*args, **kwargs)

    monkeypatch.setattr(issues_api, "update_comicinfo", write)
    with pytest.raises(HTTPException) as exc:
        await issues_api.update_chapter_comicinfo(series.id, chapter_id, body, session)
    assert exc.value.status_code == 422
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]

    result = await issues_api.update_chapter_comicinfo(
        series.id, chapter_id, body.model_copy(update={"title": "Valid"}), session,
    )
    assert read_comicinfo(path).title == "Valid"
    assert result.revision == read_comicinfo(path).revision
    with pytest.raises(HTTPException) as exc:
        await issues_api.update_chapter_comicinfo(series.id, chapter_id, body, session)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("evidence", ["volume filename", "title only", "chapter span", "XML span", "page labels"])
async def test_single_tracked_row_does_not_make_a_pack_editable(repair_db, evidence):
    session, _, series, path = repair_db
    [chapter] = series.chapters
    if evidence == "XML span":
        update_comicinfo(path, {"number": "1–3"})
    elif evidence == "page labels":
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("Chapter 1/001.png", PNG)
            zf.writestr("Chapter 2/001.png", PNG)
    else:
        names = {
            "volume filename": "Tracked Vol. 1.cbz",
            "title only": "Tracked.cbz",
            "chapter span": "Tracked Ch. 1-2.cbz",
        }
        if evidence == "title only":
            series.total_volumes = 1
        dest = path.with_name(names[evidence])
        path.rename(dest)
        chapter.file_path = str(dest)
    await session.commit()
    assert await detect_series_issues(session, series) == []
    with pytest.raises(HTTPException) as exc:
        await issues_api.get_chapter_comicinfo(series.id, chapter.id, session)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("kind", ["same-series symlink", "cross-series alias", "cross-series hardlink"])
async def test_shared_archive_ownership_blocks_detection_and_editing(repair_db, kind):
    session, _, series, path = repair_db
    alias = path.with_name("Tracked Ch. 2.cbz")
    if kind == "cross-series hardlink":
        os.link(path, alias)
    else:
        alias.symlink_to(path)
    other = Chapter(number=2, downloaded=True, file_path=str(alias), excluded=True)
    if kind == "same-series symlink":
        series.chapters.append(other)
    else:
        session.add(Series(title="Other", folder_name="Tracked", root_folder=series.root_folder, chapters=[other]))
    await session.commit()
    assert await detect_series_issues(session, series) == []
    with pytest.raises(HTTPException) as exc:
        await issues_api.get_chapter_comicinfo(series.id, series.chapters[0].id, session)
    assert exc.value.status_code == 400
    assert "shared" in exc.value.detail


async def test_outside_symlink_is_never_opened_for_detection_or_editing(repair_db, monkeypatch):
    session, _, series, path = repair_db
    outside = path.parents[2] / "Private.cbz"
    path.rename(outside)
    path.symlink_to(outside)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Opened an external archive")

    monkeypatch.setattr(metadata_issues, "read_comicinfo", unexpected)
    assert await detect_series_issues(session, series) == []
    with pytest.raises(HTTPException) as exc:
        await issues_api.get_chapter_comicinfo(series.id, series.chapters[0].id, session)
    assert exc.value.status_code == 400


async def test_unavailable_root_preserves_ignored_issue_through_all_scans(repair_db):
    session, _, series, path = repair_db
    [issue] = await detect_series_issues(session, series)
    issue.ignored = True
    original_id = issue.public_id
    await session.commit()

    root = path.parent.parent
    unmounted = root.with_name("unmounted")
    root.rename(unmounted)
    for missing in (True, False):
        if not missing:
            root.mkdir()
        await tasks.scan_all_series()
        result = await issues_api.scan_issues(session)
        assert result.ignored == 1 and result.active == 0
    root.rmdir()
    unmounted.rename(root)

    await issues_api.scan_issues(session)
    kept = await session.scalar(select(LibraryIssue))
    assert kept.public_id == original_id and kept.ignored


@pytest.mark.parametrize("scanner", ["library API", "background"])
async def test_concurrent_global_and_library_scans_keep_chapter_changes(repair_db, monkeypatch, scanner):
    session, maker, series, _ = repair_db
    chapter_id = series.chapters[0].id
    # Arrange for the folder scan to adopt a second file while the first issue
    # scan is still inside reconciliation.
    second = Path(series.chapters[0].file_path).with_name("Tracked Ch. 2.cbz")
    archive(second, None)
    series.chapters.append(Chapter(number=2, downloaded=False))
    await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = metadata_issues.reconcile_candidates

    async def pause_first(*args):
        if not entered.is_set():
            entered.set()
            await release.wait()
        else:
            assert release.is_set()
        return await original(*args)

    monkeypatch.setattr(metadata_issues, "reconcile_candidates", pause_first)

    async def global_scan():
        async with maker() as other:
            await issues_api.scan_issues(other)

    async def folder_scan():
        if scanner == "background":
            await tasks.scan_all_series()
        else:
            async with maker() as other:
                await library_api.scan(series.id, other)

    async with asyncio.timeout(10):
        first = asyncio.create_task(global_scan())
        await entered.wait()
        second_scan = asyncio.create_task(folder_scan())
        await asyncio.sleep(0.05)
        assert not second_scan.done()
        release.set()
        await asyncio.gather(first, second_scan)
    async with maker() as check:
        issue = (await check.scalars(select(LibraryIssue))).one()
        adopted = await check.scalar(select(Chapter).where(Chapter.number == 2))
        assert issue.chapter_id == chapter_id
        assert adopted.downloaded and adopted.file_path == str(second)


async def test_issue_identity_migration_preserves_ignored_rows(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE library_issues (id INTEGER PRIMARY KEY, ignored BOOLEAN)")
            await conn.exec_driver_sql("INSERT INTO library_issues VALUES (1, 1), (2, 0)")
        await db_module.init_db()
        async with engine.connect() as conn:
            rows = (await conn.exec_driver_sql(
                "SELECT id, ignored, public_id FROM library_issues ORDER BY id"
            )).all()
        await db_module.init_db()
        async with engine.connect() as conn:
            assert rows == (await conn.exec_driver_sql(
                "SELECT id, ignored, public_id FROM library_issues ORDER BY id"
            )).all()
        assert [(id_, ignored) for id_, ignored, _ in rows] == [(1, 1), (2, 0)]
        assert len({public_id for _, _, public_id in rows}) == 2
        assert all(len(public_id) == 32 for _, _, public_id in rows)
    finally:
        await engine.dispose()


def test_concurrent_archive_replacement_keeps_newer_file(tmp_path, monkeypatch):
    path = tmp_path / "Tracked Ch. 1.cbz"
    updated = tmp_path / "replacement.cbz"
    archive(path, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(updated, "<ComicInfo><Number>99</Number></ComicInfo>")
    original_revision = read_comicinfo(path).revision
    require_revision = comicinfo._require_revision

    def external_replace(path, revision):
        updated.replace(path)
        require_revision(path, revision)

    monkeypatch.setattr(comicinfo, "_require_revision", external_replace)
    with pytest.raises(ArchiveChanged):
        update_comicinfo(path, {"number": 1}, expected_revision=original_revision)
    monkeypatch.setattr(comicinfo, "_require_revision", require_revision)
    assert read_comicinfo(path).number == "99"
