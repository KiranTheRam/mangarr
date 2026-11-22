import pytest
from fastapi import HTTPException

from mangarr.api import issues as issues_api, library as library_api
from mangarr.comicinfo import read_comicinfo
from mangarr.models import Chapter
from mangarr.schemas import ComicInfoUpdateIn, FileMapIn, IssueResolveIn
from tests.test_metadata_issues import archive, repair_db


@pytest.mark.parametrize("filename", ["Tracked Ch. 2.cbz", "Tracked 2.cbz"])
async def test_number_repair_rejects_a_file_named_for_another_tracked_chapter(repair_db, filename):
    session, _, series, old_path = repair_db
    chapter = series.chapters[0]
    old_path.unlink()
    path = old_path.with_name(filename)
    archive(path, "<ComicInfo><Number>2</Number><PageCount>9</PageCount></ComicInfo>")
    await library_api.map_file(series.id, FileMapIn(chapter_id=chapter.id, file_path=str(path)), session)

    await issues_api.scan_issues(session)
    warnings = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    old_warning = next(item for item in warnings if item.field == "number")
    assert old_warning.suggested_value == "1"

    series.chapters.append(Chapter(number=2))
    await session.commit()
    original = path.read_bytes()

    with pytest.raises(HTTPException, match="issue changed") as stale:
        await issues_api.resolve_issue(old_warning.id, IssueResolveIn(revision=old_warning.revision), session)
    assert stale.value.status_code == 409
    assert path.read_bytes() == original

    current = await issues_api.get_chapter_comicinfo(series.id, chapter.id, session)
    with pytest.raises(HTTPException, match="filename identifies a different tracked chapter") as direct:
        await issues_api.update_chapter_comicinfo(
            series.id, chapter.id, ComicInfoUpdateIn(revision=current.revision, number="1"), session,
        )
    assert direct.value.status_code == 409
    assert path.read_bytes() == original

    await issues_api.scan_issues(session)
    [page_count] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert page_count.field == "page_count"
    await issues_api.resolve_issue(page_count.id, IssueResolveIn(revision=page_count.revision), session)
    assert read_comicinfo(path).number == "2"


async def test_number_repair_preserves_rounded_filename_fallback(repair_db):
    session, _, series, old_path = repair_db
    chapter = series.chapters[0]
    chapter.number = 370.371
    old_path.unlink()
    path = old_path.with_name("Tracked Ch. 370.4.cbz")
    archive(path, "<ComicInfo><Number>370.37</Number></ComicInfo>")
    await session.commit()
    await library_api.map_file(series.id, FileMapIn(chapter_id=chapter.id, file_path=str(path)), session)
    await issues_api.scan_issues(session)

    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert issue.suggested_value == "370.371"
    await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), session)
    assert read_comicinfo(path).number == "370.371"
    scan = await library_api.scan(series.id, session)
    assert scan.added_chapters == 0
    assert chapter.file_path == str(path)
    assert [item.number for item in series.chapters] == [370.371]


@pytest.mark.parametrize(
    "volume,source,embedded_volume,expected_kind",
    [
        (1, "viz", 2, "comicinfo_volume_mismatch"),
        (None, "", 2, "comicinfo_volume_unexpected"),
        (1, "disk-inferred", 1, "comicinfo_volume_isolated"),
    ],
)
async def test_mapping_conflict_invalidates_volume_recommendation(
    repair_db, volume, source, embedded_volume, expected_kind,
):
    session, _, series, original_path = repair_db
    chapter = series.chapters[0]
    chapter.volume = volume
    chapter.volume_source = source
    original_path.unlink()
    path = original_path.with_name("Tracked Ch. 2.cbz")
    archive(path, f"<ComicInfo><Number>2</Number><Volume>{embedded_volume}</Volume></ComicInfo>")
    if expected_kind == "comicinfo_volume_isolated":
        peer = path.with_name("Tracked Ch. 3.cbz")
        archive(peer, "<ComicInfo><Number>3</Number></ComicInfo>")
        series.chapters.append(Chapter(number=3, volume=1, downloaded=True, file_path=str(peer)))
    await session.commit()
    await library_api.map_file(series.id, FileMapIn(chapter_id=chapter.id, file_path=str(path)), session)
    await issues_api.scan_issues(session)
    warnings = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    prior = next(warning for warning in warnings if warning.field == "volume")
    assert prior.kind == expected_kind

    series.chapters.append(Chapter(number=2, volume=embedded_volume))
    await session.commit()
    before = path.read_bytes()
    with pytest.raises(HTTPException, match="issue changed") as stale:
        await issues_api.resolve_issue(prior.id, IssueResolveIn(revision=prior.revision), session)
    assert stale.value.status_code == 409
    assert path.read_bytes() == before
    assert await issues_api.list_issues(ignored=False, series_id=series.id, session=session) == []


async def test_conflicting_mapping_still_reports_invalid_volume_syntax(repair_db):
    session, _, series, path = repair_db
    series.chapters[0].file_path = str(path.with_name("Tracked Ch. 2.cbz"))
    path = path.rename(series.chapters[0].file_path)
    archive(path, "<ComicInfo><Number>2</Number><Volume>2.0</Volume></ComicInfo>")
    series.chapters.append(Chapter(number=2, volume=2))
    await session.commit()

    await issues_api.scan_issues(session)
    [issue] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert issue.kind == "comicinfo_volume_invalid"
    await issues_api.resolve_issue(issue.id, IssueResolveIn(revision=issue.revision), session)
    info = read_comicinfo(path)
    assert info.number == "2"
    assert info.volume is None


async def test_mapping_conflict_invalidates_series_recommendation(repair_db):
    session, _, series, original_path = repair_db
    chapter = series.chapters[0]
    original_path.unlink()
    path = original_path.with_name("Tracked Ch. 2.cbz")
    archive(path, "<ComicInfo><Series>Another Series</Series><Number>2</Number><PageCount>4</PageCount></ComicInfo>")
    await session.commit()
    await library_api.map_file(series.id, FileMapIn(chapter_id=chapter.id, file_path=str(path)), session)
    await issues_api.scan_issues(session)
    warnings = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    prior = next(warning for warning in warnings if warning.field == "series")

    # Once the filename names another tracked chapter, the archive may belong
    # elsewhere, so its embedded series name is no longer comparable.
    series.chapters.append(Chapter(number=2))
    await session.commit()
    before = path.read_bytes()
    with pytest.raises(HTTPException, match="issue changed") as stale:
        await issues_api.resolve_issue(prior.id, IssueResolveIn(revision=prior.revision), session)
    assert stale.value.status_code == 409
    assert path.read_bytes() == before

    # Archive-intrinsic checks still apply to the disputed file.
    [page_count] = await issues_api.list_issues(ignored=False, series_id=series.id, session=session)
    assert page_count.field == "page_count"
    await issues_api.resolve_issue(page_count.id, IssueResolveIn(revision=page_count.revision), session)
    info = read_comicinfo(path)
    assert (info.series, info.number, info.page_count) == ("Another Series", "2", "1")


@pytest.mark.parametrize("update,allowed", [
    ({"series": "Tracked"}, False),
    ({"volume": 1}, False),
    ({"volume": None}, True),  # removes the invalid tag, an archive-intrinsic repair
    ({"title": "Bonus"}, True),
    ({"series": "Another Series"}, True),  # unchanged value
])
async def test_manual_editor_limits_disputed_archives_to_intrinsic_edits(repair_db, update, allowed):
    session, _, series, original_path = repair_db
    chapter = series.chapters[0]
    original_path.unlink()
    path = original_path.with_name("Tracked Ch. 2.cbz")
    archive(path, "<ComicInfo><Series>Another Series</Series><Number>2</Number><Volume>2.5</Volume></ComicInfo>")
    series.chapters.append(Chapter(number=2))
    await session.commit()
    await library_api.map_file(series.id, FileMapIn(chapter_id=chapter.id, file_path=str(path)), session)
    current = await issues_api.get_chapter_comicinfo(series.id, chapter.id, session)
    before = path.read_bytes()

    body = ComicInfoUpdateIn(revision=current.revision, **update)
    if allowed:
        await issues_api.update_chapter_comicinfo(series.id, chapter.id, body, session)
    else:
        with pytest.raises(HTTPException, match="different tracked chapter") as rejected:
            await issues_api.update_chapter_comicinfo(series.id, chapter.id, body, session)
        assert rejected.value.status_code == 409
        assert path.read_bytes() == before
