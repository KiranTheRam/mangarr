import pytest

from mangarr.jobs.tasks import (
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
