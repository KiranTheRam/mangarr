import pytest

from mangarr.jobs.tasks import (
    _add_volume_map_gap_chapters,
    _has_internal_number_gap,
    disk_volume_numbers,
    reconcile_downloaded_files,
    refine_volume_map_with_disk,
)
from mangarr.models import Chapter, RootFolder, Series, SeriesStatus


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_reconcile_clears_downloaded_when_file_missing(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    missing = tmp_path / "missing.cbz"
    series = Series(id=1, title="Kagurabachi")
    chapters = [
        Chapter(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Chapter(id=2, series_id=1, number=2, downloaded=True, file_path=str(missing)),
        Chapter(id=3, series_id=1, number=3, downloaded=False, file_path=""),
    ]
    series.chapters = chapters
    session = FakeSession()

    changed = await reconcile_downloaded_files(session, series)

    assert changed == 1
    assert session.commits == 1
    assert chapters[0].downloaded is True
    assert chapters[0].file_path == str(present)
    assert chapters[1].downloaded is False
    assert chapters[1].file_path == ""
    assert chapters[2].downloaded is False


@pytest.mark.asyncio
async def test_reconcile_does_not_commit_when_nothing_changed(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    series = Series(id=1, title="Kagurabachi")
    series.chapters = [
        Chapter(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Chapter(id=2, series_id=1, number=2, downloaded=False, file_path=""),
    ]
    session = FakeSession()

    changed = await reconcile_downloaded_files(session, series)

    assert changed == 0
    assert session.commits == 0


def _series_on_disk(tmp_path, volumes: list[int], status=SeriesStatus.RELEASING) -> Series:
    folder = tmp_path / "Kagurabachi"
    folder.mkdir()
    for v in volumes:
        (folder / f"Kagurabachi - Volume {v:02d}.cbz").write_bytes(b"cbz")
    series = Series(id=1, title="Kagurabachi", folder_name="Kagurabachi", status=status)
    series.root_folder = RootFolder(id=1, path=str(tmp_path))
    return series


def test_disk_volume_numbers_reads_volume_archives(tmp_path):
    series = _series_on_disk(tmp_path, [1, 2, 3])
    assert disk_volume_numbers(series) == {1, 2, 3}


def test_refine_volume_map_assigns_unmapped_chapters_to_disk_volumes(tmp_path):
    """The initial-import scenario: the source only knows volume 1, but volume
    archives 1–3 exist on disk — later chapters must be mapped onto them so a
    scan can mark them downloaded instead of the monitor re-grabbing them."""
    series = _series_on_disk(tmp_path, [1, 2, 3])
    series.chapters = [
        Chapter(id=i, series_id=1, number=float(i)) for i in range(1, 25)
    ]
    source_map = {float(i): 1 for i in range(1, 9)}  # only volume 1 is known

    refined = refine_volume_map_with_disk(series, source_map)

    # source anchors survive; the rest spread over the disk volumes at the
    # fallback tankobon rate (9/volume, nothing better is known here)
    assert refined[8.0] == 1
    assert set(refined.values()) <= {1, 2, 3}
    assert refined[18.0] == 2
    assert refined[19.0] == 3
    assert refined[24.0] == 3
    assert all(float(n) in refined for n in range(1, 25))


def test_refine_volume_map_without_disk_volumes_is_unchanged(tmp_path):
    series = Series(id=1, title="Kagurabachi", folder_name="Kagurabachi", alt_titles="")
    series.root_folder = RootFolder(id=1, path=str(tmp_path))
    series.chapters = [Chapter(id=1, series_id=1, number=1.0)]
    source_map = {1.0: 1}
    assert refine_volume_map_with_disk(series, source_map) == source_map


def test_has_internal_number_gap_ignores_small_holes_and_decimals():
    small_gap = {
        1.0: Chapter(number=1.0),
        1.5: Chapter(number=1.5),
        3.0: Chapter(number=3.0),
    }
    large_gap = {
        369.0: Chapter(number=369.0),
        453.0: Chapter(number=453.0),
    }

    assert not _has_internal_number_gap(small_gap)
    assert _has_internal_number_gap(large_gap)


def test_add_volume_map_gap_chapters_only_inside_observed_span():
    series = Series(id=1, title="Dragon Ball", monitored=True)
    series.chapters = [
        Chapter(id=1, series_id=1, number=369.0, volume=31),
        Chapter(id=2, series_id=1, number=453.0, volume=38),
    ]
    existing = {c.number: c for c in series.chapters}
    volume_map = {
        100.0: 9,    # outside the observed span: do not invent it
        369.0: 31,   # already exists
        370.0: 31,
        371.0: 32,
        452.0: 38,
        600.0: 50,   # outside the observed span: do not extend the series
    }

    added = _add_volume_map_gap_chapters(series, existing, volume_map)

    assert added == 3
    assert {370.0, 371.0, 452.0} <= set(existing)
    assert existing[370.0].volume == 31
    assert existing[371.0].volume == 32
    assert existing[452.0].volume == 38
    assert all(existing[n].monitored for n in (370.0, 371.0, 452.0))
    assert 100.0 not in existing
    assert 600.0 not in existing
