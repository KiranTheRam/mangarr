"""Mapping and repair-input changes refresh warnings without waiting for a scan."""

import pytest

from mangarr.api import issues as issues_api, library as library_api
from mangarr.jobs import tasks
from mangarr.models import Chapter, SeriesFolder
from mangarr.schemas import FileMapIn, FileMapRangeIn, SeriesFolderUpdateIn
from tests.test_metadata_issues import archive, repair_db  # noqa: F401


async def _warnings(session, series_id):
    return await issues_api.list_issues(ignored=False, series_id=series_id, session=session)


@pytest.mark.parametrize("mapping", ["single", "range"])
async def test_manual_mapping_drops_the_previous_archive_warning(repair_db, mapping):
    session, _, series, path = repair_db
    await issues_api.scan_issues(session)
    [warning] = await _warnings(session, series.id)
    assert warning.file_path == str(path)
    replacement = path.with_name("Tracked Ch. 1 (fixed).cbz")
    archive(replacement, "<ComicInfo><Series>Tracked</Series><Number>1</Number><Volume>1</Volume></ComicInfo>")

    if mapping == "single":
        body = FileMapIn(chapter_id=series.chapters[0].id, file_path=str(replacement))
        await library_api.map_file(series.id, body, session)
    else:
        body = FileMapRangeIn(file_path=str(replacement), from_number=1, to_number=1)
        await library_api.map_file_range(series.id, body, session)

    assert await _warnings(session, series.id) == []


async def test_extra_folder_changes_refresh_volume_recommendations(repair_db):
    session, _, series, initial = repair_db
    shared = initial.parent.parent / "Shared"
    shared.mkdir()
    path = initial.rename(shared / initial.name)
    series.chapters[0].file_path = str(path)
    folder = SeriesFolder(series_id=series.id, path="Shared", volume_offset=5)
    session.add(folder)
    await session.commit()
    await issues_api.scan_issues(session)
    [warning] = await _warnings(session, series.id)
    assert warning.suggested_value == "6"

    await library_api.update_folder(series.id, folder.id, SeriesFolderUpdateIn(volume_offset=6), session)
    [warning] = await _warnings(session, series.id)
    assert warning.suggested_value == "7"

    # Without the folder, the archive is outside the series and is not editable.
    await library_api.remove_folder(series.id, folder.id, session)
    assert await _warnings(session, series.id) == []


async def test_volume_merge_drops_warnings_for_removed_chapter_archives(repair_db):
    session, _, series, path = repair_db
    second = path.with_name("Tracked Ch. 2.cbz")
    later = path.with_name("Tracked Ch. 3.cbz")
    archive(second, "<ComicInfo><Number>2</Number></ComicInfo>")
    archive(later, "<ComicInfo><Number>3</Number></ComicInfo>")
    for number, volume, file_path in ((2, 1, second), (3, 2, later)):
        series.chapters.append(Chapter(
            number=number, volume=volume, volume_source="viz", downloaded=True, file_path=str(file_path),
        ))
    for chapter in series.chapters:
        chapter.file_source = "alpha"  # merge only packs Mangarr downloads
    await session.commit()
    await issues_api.scan_issues(session)
    [warning] = await _warnings(session, series.id)
    assert warning.file_path == str(path)

    assert await tasks.merge_complete_volumes(session, series, {}, force=True) == [1]

    assert not path.exists()
    assert await _warnings(session, series.id) == []


async def test_title_edit_refreshes_series_recommendations(repair_db):
    from mangarr.api import series as series_api
    from mangarr.schemas import SeriesUpdateIn

    from mangarr.comicinfo import update_comicinfo

    session, _, series, path = repair_db
    update_comicinfo(path, {"series": "Renamed Series"})
    await issues_api.scan_issues(session)
    assert "series" in {w.field for w in await _warnings(session, series.id)}

    await series_api.update_series(series.id, SeriesUpdateIn(title="Renamed Series"), session)

    assert [w.field for w in await _warnings(session, series.id)] == ["volume"]


async def test_title_preference_refreshes_series_recommendations(repair_db, monkeypatch):
    from mangarr.api import series as series_api

    session, _, series, _ = repair_db
    await issues_api.scan_issues(session)

    def retitle(target, _preference):
        target.title = "Preferred Title"
        return True

    monkeypatch.setattr(series_api, "_apply_title_preference", retitle)
    result = await series_api.apply_title_preference(session)

    assert result.updated == 1
    [series_warning] = [w for w in await _warnings(session, series.id) if w.field == "series"]
    assert series_warning.suggested_value == "Preferred Title"


async def test_chapter_volume_edit_refreshes_volume_recommendations(repair_db):
    from mangarr.api import series as series_api
    from mangarr.schemas import ChapterMetadataIn

    session, _, series, _ = repair_db
    await issues_api.scan_issues(session)
    [warning] = await _warnings(session, series.id)
    assert warning.field == "volume" and warning.suggested_value == "1"

    await series_api.update_chapter_metadata(
        series.id, series.chapters[0].id, ChapterMetadataIn(volume=9), session,
    )

    assert await _warnings(session, series.id) == []
