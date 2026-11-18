import asyncio
import errno
import os
import struct
import sys
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from mangarr import comicinfo, db as db_module, metadata_issues
from mangarr.api import issues as issues_api, library as library_api, series as series_api
from mangarr.comicinfo import ArchiveChanged, ComicInfoError, read_comicinfo, update_comicinfo
from mangarr.jobs import tasks
from mangarr.schemas import (
    ChapterMetadataIn, ComicInfoUpdateIn, FileMapIn, FileMapRangeIn, IssueResolveIn, RenameApplyIn,
    SeriesFolderUpdateIn,
)
from mangarr.metadata_issues import ArchiveOwnership, detect_series_issues, find_candidates
from mangarr.models import Base, Chapter, LibraryIssue, RootFolder, Series, SeriesFolder

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


@pytest.mark.skipif(os.geteuid() != 0, reason="requires changing file owner")
def test_comicinfo_update_preserves_file_owner_and_aborts_on_chown_failure(tmp_path, monkeypatch):
    path = tmp_path / "chapter.cbz"
    archive(path, "<ComicInfo><Number>2</Number></ComicInfo>")
    os.chown(path, 1234, 2345)
    os.chmod(path, 0o640)
    update_comicinfo(path, {"number": 1})
    file_stat = path.stat()
    assert (file_stat.st_uid, file_stat.st_gid, file_stat.st_mode & 0o777) == (1234, 2345, 0o640)

    def denied(*_args):
        raise PermissionError("cannot preserve archive ownership")

    before = path.read_bytes()
    monkeypatch.setattr(os, "fchown", denied)
    with pytest.raises(ComicInfoError, match="cannot preserve archive ownership"):
        update_comicinfo(path, {"number": 3})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux stores POSIX ACLs in xattrs")
def test_comicinfo_rewrite_preserves_access_acl_and_aborts_on_acl_failure(tmp_path, monkeypatch):
    path = tmp_path / "chapter.cbz"
    archive(path, "<ComicInfo><Number>2</Number></ComicInfo>")
    acl = struct.pack(
        "<I" + "HHI" * 5,
        2,  # Linux POSIX ACL xattr version
        1, 6, 0xFFFFFFFF,  # owner: read/write
        2, 4, 4567,  # named user: read
        4, 0, 0xFFFFFFFF,  # owning group: no access
        16, 4, 0xFFFFFFFF,  # ACL mask: read
        32, 0, 0xFFFFFFFF,  # others: no access
    )
    access_acl = "system.posix_acl_access"
    try:
        os.setxattr(path, access_acl, acl)
    except OSError as exc:
        if exc.errno == errno.EOPNOTSUPP:
            pytest.skip("test filesystem does not support ACLs")
        raise
    update_comicinfo(path, {"number": 1})
    assert os.getxattr(path, access_acl) == acl
    assert path.stat().st_mode & 0o777 == 0o640

    def denied(*_args):
        raise PermissionError("cannot preserve archive ACL")

    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(os, "setxattr", denied)
        with pytest.raises(ComicInfoError, match="cannot preserve archive ACL"):
            update_comicinfo(path, {"number": 3})
    assert path.read_bytes() == before
    assert os.getxattr(path, access_acl) == acl
    assert list(tmp_path.iterdir()) == [path]

    # A later directory default must not add an ACL to a plain archive.
    os.removexattr(path, access_acl)
    os.setxattr(tmp_path, "system.posix_acl_default", acl)
    update_comicinfo(path, {"number": 4})
    with pytest.raises(OSError) as exc:
        os.getxattr(path, access_acl)
    assert exc.value.errno == errno.ENODATA
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "source, locked, peer_tag, expect_removal",
    [
        ("disk-inferred", False, None, True),
        ("disk-inferred", False, "9", False),
        ("disk-inferred", False, "invalid", False),
        ("disk-inferred", True, None, False),
        ("manual", False, None, False),
        ("manual", False, "9", False),
    ],
)
def test_isolated_volume_requires_an_untrusted_tag_and_untagged_peers(
    tmp_path, source, locked, peer_tag, expect_removal,
):
    tagged = tmp_path / "Kagurabachi - Ch. 0092.1.cbz"
    plain = tmp_path / "Kagurabachi - Ch. 0092.cbz"
    archive(
        tagged,
        "<ComicInfo><Series>Kagurabachi</Series><Number>92.1</Number>"
        "<Volume>10</Volume><PageCount>1</PageCount></ComicInfo>",
    )
    archive(plain, None if peer_tag is None else f"<ComicInfo><Volume>{peer_tag}</Volume></ComicInfo>")
    series = Series(id=1, title="Kagurabachi", provider_title="Kagurabachi", alt_titles="")
    series.chapters = [
        Chapter(id=1, number=92.0, volume=10, volume_source="manual", downloaded=True, file_path=str(plain)),
        Chapter(
            id=2, number=92.1, volume=10, volume_source=source, volume_locked=locked,
            downloaded=True, file_path=str(tagged),
        ),
    ]

    series.root_folder = RootFolder(path=str(tmp_path.parent))
    series.folder_name = tmp_path.name
    ownership = ArchiveOwnership([(ch.id, ch.file_path) for ch in series.chapters])
    issues = find_candidates(series, ownership)

    if peer_tag == "9":
        assert any(item.kind == "comicinfo_volume_mismatch" and item.suggested_value == "10" for item in issues)
    isolated = [item for item in issues if item.kind == "comicinfo_volume_isolated"]
    assert len(isolated) == int(expect_removal)
    if not expect_removal:
        return
    [issue] = isolated
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
    issues = {item.field: item for item in find_candidates(series, ArchiveOwnership([(3, str(path))]))}

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
    lock = asyncio.Lock()
    monkeypatch.setattr(issues_api, "manual_archive_lock", lock)
    monkeypatch.setattr(library_api, "manual_archive_lock", lock)
    monkeypatch.setattr(metadata_issues, "manual_archive_lock", lock)
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


@pytest.mark.parametrize("display_title", ["My Custom Title", "海辺の物語"])
async def test_series_repair_accepts_the_custom_display_title(repair_db, display_title):
    session, _, series, path = repair_db
    series.title = display_title
    series.provider_title = "Official Provider Name"
    update_comicinfo(path, {"series": display_title, "volume": 1})
    await session.commit()

    await library_api.scan(series.id, session)
    assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == []

    update_comicinfo(path, {"series": "An Unrelated Series"})
    await library_api.scan(series.id, session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert issue.suggested_value == display_title
    await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), session)
    assert read_comicinfo(path).series == display_title
    assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == []


async def _shared_volume_issue(repair_db):
    session, _, series, initial = repair_db
    shared = initial.parent.parent / "Shared"
    shared.mkdir()
    path = initial.rename(shared / initial.name)
    series.chapters[0].file_path = str(path)
    folder = SeriesFolder(series_id=series.id, path="Shared", volume_offset=5)
    session.add(folder)
    await session.commit()
    await issues_api.scan_issues(session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert issue.suggested_value == "6"
    return path, folder.id, issue


async def test_folder_offset_update_waits_for_archive_repair(repair_db, monkeypatch):
    _, maker, series, _ = repair_db
    path, folder_id, issue = await _shared_volume_issue(repair_db)
    write_started = asyncio.Event()
    offset_requested = asyncio.Event()
    release_write = asyncio.Event()
    original_write = issues_api._write_archive

    async def pause_archive_write(*args, **kwargs):
        write_started.set()
        await release_write.wait()
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(issues_api, "_write_archive", pause_archive_write)

    async def repair():
        async with maker() as session:
            await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), session)

    async def update_offset():
        async with maker() as session:
            offset_requested.set()
            return await library_api.update_folder(
                series.id, folder_id, SeriesFolderUpdateIn(volume_offset=6), session,
            )

    async with asyncio.timeout(10):
        repairing = asyncio.create_task(repair())
        await write_started.wait()
        changing_offset = asyncio.create_task(update_offset())
        await offset_requested.wait()
        try:
            done, _ = await asyncio.wait({changing_offset}, timeout=0.05)
            assert not done
        finally:
            release_write.set()
            _, updated_folder = await asyncio.gather(repairing, changing_offset)

    assert updated_folder.volume_offset == 6
    assert read_comicinfo(path).volume == "6"
    async with maker() as session:
        await issues_api.scan_issues(session)
        [current] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert current.suggested_value == "7"


async def test_repair_rejects_recommendation_from_previous_folder_offset(repair_db):
    _, maker, series, _ = repair_db
    path, folder_id, issue = await _shared_volume_issue(repair_db)
    async with maker() as session:
        await library_api.update_folder(
            series.id, folder_id, SeriesFolderUpdateIn(volume_offset=6), session,
        )
        with pytest.raises(HTTPException) as exc:
            await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), session)
        assert exc.value.status_code == 409
        [current] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert read_comicinfo(path).volume == "9"
    assert current.suggested_value == "7"


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


async def test_changed_issue_kind_starts_active_and_rejects_old_dismissal(repair_db):
    session, _, series, path = repair_db
    [issue] = await detect_series_issues(session, series)
    await session.commit()
    old_id = issue.public_id
    await issues_api.ignore_issue(old_id, session)

    update_comicinfo(path, {"volume": 8})
    await detect_series_issues(session, series)
    await session.commit()
    assert issue.kind == "comicinfo_volume_mismatch" and issue.ignored and issue.public_id == old_id

    update_comicinfo(path, {"volume": "unknown"})
    result = await issues_api.scan_issues(session)
    assert result.active == 1 and result.ignored == 0
    assert issue.kind == "comicinfo_volume_invalid" and issue.public_id != old_id
    with pytest.raises(HTTPException) as exc:
        await issues_api.ignore_issue(old_id, session)
    assert exc.value.status_code == 404
    assert not issue.ignored


async def test_comicinfo_update_distinguishes_omission_from_removal(repair_db):
    session, _, series, path = repair_db
    chapter_id = series.chapters[0].id
    update_comicinfo(path, {"volume": "1.5", "page_count": "unknown"})
    before = read_comicinfo(path)
    with pytest.raises(HTTPException) as exc:
        await issues_api.update_chapter_comicinfo(
            series.id, chapter_id, ComicInfoUpdateIn(revision=before.revision), session,
        )
    assert exc.value.status_code == 422 and read_comicinfo(path) == before

    result = await issues_api.update_chapter_comicinfo(
        series.id, chapter_id, ComicInfoUpdateIn(revision=before.revision, title="New title"), session,
    )
    assert result.title == "New title" and result.volume == "1.5" and result.page_count == "unknown"
    result = await issues_api.update_chapter_comicinfo(
        series.id, chapter_id, ComicInfoUpdateIn(revision=result.revision, page_count=None), session,
    )
    assert result.page_count is None and result.volume == "1.5"


@pytest.mark.parametrize("number", ["2", None])
async def test_comicinfo_editor_protects_number_used_for_library_matching(repair_db, number):
    session, _, series, path = repair_db
    first = series.chapters[0]
    second = Chapter(number=2)
    first.downloaded, first.file_path = False, ""
    series.chapters.append(second)
    path = path.rename(path.with_name("Bonus Story.cbz"))
    await session.commit()

    await library_api.scan(series.id, session)
    before = await issues_api.get_chapter_comicinfo(series.id, first.id, session)
    original = path.read_bytes()
    with pytest.raises(HTTPException, match="uses ComicInfo Number") as exc:
        await issues_api.update_chapter_comicinfo(
            series.id, first.id, ComicInfoUpdateIn(revision=before.revision, number=number), session,
        )
    assert exc.value.status_code == 409
    assert path.read_bytes() == original

    edited = await issues_api.update_chapter_comicinfo(
        series.id, first.id,
        ComicInfoUpdateIn(revision=before.revision, title="Bonus", number="1.0"), session,
    )
    assert edited.title == "Bonus"
    assert edited.number == "1.0"
    await library_api.scan(series.id, session)
    assert first.downloaded and first.file_path == str(path)
    assert not second.downloaded and not second.file_path


async def test_comicinfo_number_can_be_removed_when_filename_matches_chapter(repair_db):
    session, _, series, path = repair_db
    chapter = series.chapters[0]
    before = await issues_api.get_chapter_comicinfo(series.id, chapter.id, session)
    edited = await issues_api.update_chapter_comicinfo(
        series.id, chapter.id, ComicInfoUpdateIn(revision=before.revision, number=None), session,
    )
    assert edited.number is None
    await library_api.scan(series.id, session)
    assert chapter.downloaded and chapter.file_path == str(path)


async def test_rename_waits_for_comicinfo_replacement(repair_db, monkeypatch):
    session, maker, series, path = repair_db
    series_id, chapter_id = series.id, series.chapters[0].id
    before = await issues_api.get_chapter_comicinfo(series_id, chapter_id, session)
    loop = asyncio.get_running_loop()
    checked = asyncio.Event()
    release = threading.Event()
    require_revision = comicinfo._require_revision

    def pause_replacement(candidate_path, revision):
        require_revision(candidate_path, revision)
        if any(path.parent.glob(f".{path.name}.*.tmp")):
            loop.call_soon_threadsafe(checked.set)
            assert release.wait(timeout=5)

    monkeypatch.setattr(comicinfo, "_require_revision", pause_replacement)

    async def repair():
        async with maker() as other:
            return await issues_api.update_chapter_comicinfo(
                series_id, chapter_id,
                ComicInfoUpdateIn(revision=before.revision, volume=1), other,
            )

    async def rename():
        async with maker() as other:
            return await library_api.rename_apply(series_id, RenameApplyIn(), other)

    async with asyncio.timeout(10):
        repair_task = asyncio.create_task(repair())
        rename_task = None
        try:
            await checked.wait()
            rename_task = asyncio.create_task(rename())
            _, pending = await asyncio.wait([rename_task], timeout=0.05)
            assert rename_task in pending and path.exists()
        finally:
            release.set()
            results = await asyncio.gather(repair_task, *([rename_task] if rename_task else []))
    assert results[1][0].status == "renamed"
    async with maker() as other:
        renamed = Path((await other.get(Chapter, chapter_id)).file_path)
    assert not path.exists()
    assert read_comicinfo(renamed).volume == "1"


@pytest.mark.parametrize("destination_state", ["empty", "stale_warning", "collision"])
async def test_rename_preserves_ignored_warning_for_same_archive(repair_db, destination_state):
    session, _, series, path = repair_db
    series_id = series.id
    await library_api.scan(series_id, session)
    [warning] = await issues_api.list_issues(ignored=False, series_id=series_id, session=session)
    await issues_api.ignore_issue(warning.id, session)
    [preview] = await library_api.rename_preview(series_id, session)
    destination = Path(preview.new_path)

    if destination_state == "stale_warning":
        session.add(LibraryIssue(
            key=metadata_issues._key(series_id, str(destination), warning.field),
            series_id=series_id, chapter_id=warning.chapter_id,
            kind=warning.kind, file_path=str(destination), field=warning.field,
            title="Old warning at destination", detail="The original archive was deleted.",
        ))
        await session.commit()
    elif destination_state == "collision":
        archive(destination, "<ComicInfo><Number>1</Number></ComicInfo>")

    [outcome] = await library_api.rename_apply(series_id, RenameApplyIn(), session)
    if destination_state == "collision":
        assert outcome.status == "skipped-collision"
        [unchanged] = await issues_api.list_issues(ignored=True, series_id=series_id, session=session)
        assert unchanged.id == warning.id and unchanged.file_path == str(path)
        return

    assert outcome.status == "renamed"
    [relocated] = await issues_api.list_issues(ignored=True, series_id=series_id, session=session)
    assert relocated.id == warning.id
    assert relocated.file_path == str(destination)
    assert relocated.revision != warning.revision
    await library_api.scan(series_id, session)
    assert await issues_api.list_issues(ignored=True, series_id=series_id, session=session) == [relocated]
    assert await issues_api.list_issues(ignored=False, series_id=series_id, session=session) == []


@pytest.mark.parametrize("rebuilt_number", [1, 99])
async def test_chapter_resync_detaches_then_reconciles_issues(repair_db, monkeypatch, rebuilt_number):
    session, maker, series, _ = repair_db
    series_id = series.id
    await library_api.scan(series_id, session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series_id, session=session)
    await issues_api.ignore_issue(issue.id, session)

    async def rebuild(rebuild_session, rebuilding_series, _values):
        async with maker() as other:
            [detached] = await issues_api.list_issues(ignored=True, series_id=series_id, session=other)
            assert detached.chapter_id is None and detached.chapter_number is None
        rebuilding_series.chapters = [Chapter(number=rebuilt_number, volume=1, volume_source="viz")]
        await rebuild_session.commit()

    monkeypatch.setattr(tasks, "update_chapters", rebuild)
    await library_api.resync_chapters(series_id, session)
    ignored = await issues_api.list_issues(ignored=True, series_id=series_id, session=session)
    assert await issues_api.list_issues(ignored=False, series_id=series_id, session=session) == []
    if rebuilt_number == 1:
        [restored] = ignored
        assert restored.id == issue.id and restored.chapter_number == 1
    else:
        assert ignored == []


async def test_chapter_resync_preserves_mappings_when_extra_folder_is_unavailable(repair_db):
    session, _, series, path = repair_db
    chapter = series.chapters[0]
    extra = path.parent.parent.parent / "extra-mount"
    session.add(SeriesFolder(series_id=series.id, path=str(extra)))
    chapter.file_path = str(extra / path.name)
    await session.commit()

    with pytest.raises(HTTPException, match="extra folder") as exc:
        await library_api.resync_chapters(series.id, session)
    assert exc.value.status_code == 409
    assert chapter.downloaded and chapter.file_path == str(extra / path.name)
    assert await session.get(Chapter, chapter.id) is chapter


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


async def test_unsupported_xml_encoding_does_not_abort_scans(repair_db):
    session, _, series, path = repair_db
    archive(path, '<?xml version="1.0" encoding="bogus"?><ComicInfo/>')
    other_folder = path.parent.parent / "Other"
    other_folder.mkdir()
    other = other_folder / "Other Ch. 1.cbz"
    archive(other, "<ComicInfo><Number>2</Number></ComicInfo>")
    session.add(Series(
        title="Other", folder_name="Other", root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(other))],
    ))
    await session.commit()

    await library_api.scan(series.id, session)
    result = await issues_api.scan_issues(session)
    assert result.active == 1
    issue = (await session.scalars(select(LibraryIssue))).one()
    assert issue.file_path == str(other) and issue.field == "number"
    with pytest.raises(HTTPException) as exc:
        await issues_api.get_chapter_comicinfo(series.id, series.chapters[0].id, session)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("number", ["1-2", "1 – 2", "1, 2"])
def test_comicinfo_edit_rejects_chapter_spans(number):
    with pytest.raises(ValidationError, match="Number must describe a single chapter"):
        ComicInfoUpdateIn(
            revision="revision", series="Tracked", number=number,
            title=None, volume=None, page_count=None,
        )


@pytest.mark.parametrize("mapping", ["chapter", "range"])
async def test_archive_edit_serializes_cross_series_manual_mapping(repair_db, monkeypatch, mapping):
    session, maker, series, path = repair_db
    sid, chapter_id = series.id, series.chapters[0].id
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1), Chapter(number=2)],
    )
    session.add(other)
    await session.commit()
    other_id, other_chapter_id = other.id, other.chapters[0].id
    entered, release, mapping_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = issues_api._write_archive

    async def pause_write(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(issues_api, "_write_archive", pause_write)

    async def edit():
        async with maker() as db:
            body = ComicInfoUpdateIn(
                revision=read_comicinfo(path).revision, series="Tracked", number="1",
                title="New title", volume=None, page_count=None,
            )
            return await issues_api.update_chapter_comicinfo(sid, chapter_id, body, db)

    async def map_archive():
        async with maker() as db:
            mapping_started.set()
            if mapping == "chapter":
                await library_api.map_file(
                    other_id, FileMapIn(chapter_id=other_chapter_id, file_path=str(path)), db,
                )
            else:
                await library_api.map_file_range(
                    other_id, FileMapRangeIn(from_number=1, to_number=2, file_path=str(path)), db,
                )

    async with asyncio.timeout(10):
        saving = asyncio.create_task(edit())
        await entered.wait()
        mapping_task = asyncio.create_task(map_archive())
        await mapping_started.wait()
        try:
            assert not mapping_task.done()
            async with maker() as db:
                assert not (await db.get(Chapter, other_chapter_id)).downloaded
        finally:
            release.set()
            await asyncio.gather(saving, mapping_task)
    assert read_comicinfo(path).title == "New title"
    async with maker() as db:
        with pytest.raises(HTTPException, match="shared"):
            await issues_api.get_chapter_comicinfo(sid, chapter_id, db)


@pytest.mark.parametrize("background", [False, True])
async def test_cross_series_scan_waits_for_comicinfo_replacement(repair_db, monkeypatch, background):
    session, maker, series, path = repair_db
    other = Series(
        title="Other", folder_name=series.folder_name, root_folder=series.root_folder,
        chapters=[Chapter(number=1)],
    )
    session.add(other)
    await session.commit()
    series_id, chapter_id = series.id, series.chapters[0].id
    other_id, other_chapter_id = other.id, other.chapters[0].id

    app = FastAPI()
    app.include_router(issues_api.router)
    app.include_router(library_api.router)
    loop = asyncio.get_running_loop()
    checked = asyncio.Event()
    release = threading.Event()
    require_revision = comicinfo._require_revision

    def pause_replacement(candidate_path, revision):
        require_revision(candidate_path, revision)
        if any(path.parent.glob(f".{path.name}.*.tmp")):
            loop.call_soon_threadsafe(checked.set)
            assert release.wait(timeout=5)

    monkeypatch.setattr(comicinfo, "_require_revision", pause_replacement)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def adopt():
            if background:
                async with tasks.series_lock(other_id), maker() as db:
                    assert await tasks.scan_series_folder(db, await tasks._load_series(db, other_id))
            else:
                response = await client.post(f"/series/{other_id}/scan")
                assert response.status_code == 200, response.text
                assert response.json()["matched_chapters"] == 1

        endpoint = f"/series/{series_id}/chapters/{chapter_id}/comicinfo"
        before = await client.get(endpoint)
        assert before.status_code == 200, before.text
        async with asyncio.timeout(10):
            repair = asyncio.create_task(client.put(endpoint, json={
                "revision": before.json()["revision"], "title": "Edited title",
            }))
            adoption = None
            try:
                await checked.wait()
                adoption = asyncio.create_task(adopt())
                _, pending = await asyncio.wait([adoption], timeout=0.05)
                assert adoption in pending
                async with maker() as db:
                    assert not (await db.get(Chapter, other_chapter_id)).downloaded
            finally:
                release.set()
                repaired = await repair
                if adoption is not None:
                    await adoption

        assert repaired.status_code == 200, repaired.text
        assert (await client.get(endpoint)).status_code == 400
        async with maker() as db:
            assert (await db.get(Chapter, other_chapter_id)).file_path == str(path)
    assert read_comicinfo(path).title == "Edited title"


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


@pytest.mark.parametrize("adopt_files", [False, True])
async def test_bulk_scans_build_one_ownership_index(repair_db, monkeypatch, adopt_files):
    session, _, series, path = repair_db
    other_folder = path.parent.with_name("Other")
    other_folder.mkdir()
    other_path = other_folder / "Other Ch. 1.cbz"
    archive(other_path, "<ComicInfo><Number>2</Number></ComicInfo>")
    session.add(Series(
        title="Other", folder_name="Other", root_folder=series.root_folder,
        chapters=[Chapter(
            number=1, downloaded=not adopt_files, file_path="" if adopt_files else str(other_path),
        )],
    ))
    await session.commit()

    snapshots = []
    original = metadata_issues.chapter_paths

    async def record_paths(session):
        rows = await original(session)
        snapshots.append({path for _, path in rows})
        return rows

    monkeypatch.setattr(metadata_issues, "chapter_paths", record_paths)
    if adopt_files:
        await tasks.scan_all_series()
    else:
        await issues_api.scan_issues(session)
    assert snapshots == [{str(path), str(other_path)}]
    assert {issue.file_path for issue in await session.scalars(select(LibraryIssue))} == snapshots[0]


async def test_repair_rechecks_ownership_after_a_bulk_scan(repair_db):
    session, _, series, path = repair_db
    await issues_api.scan_issues(session)
    issue = (await session.scalars(select(LibraryIssue))).one()
    alias = path.with_name("Shared.cbz")
    os.link(path, alias)
    session.add(Series(
        title="Other", folder_name="Tracked", root_folder=series.root_folder,
        chapters=[Chapter(number=1, downloaded=True, file_path=str(alias))],
    ))
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await issues_api.resolve_issue(issue.public_id, IssueResolveIn(revision=issue.revision), session)
    assert exc.value.status_code == 409
    assert read_comicinfo(path).volume == "9"


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
