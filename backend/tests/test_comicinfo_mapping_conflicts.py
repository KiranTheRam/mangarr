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
