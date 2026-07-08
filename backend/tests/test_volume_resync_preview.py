"""_run_resync powers both the volume resync and its dry-run preview: it must
report the same counts either way, and the dry run (on detached chapter
copies) must leave the real chapters untouched."""

import zipfile

from mangarr.api.library import _chapter_copies, _run_resync
from mangarr.models import Chapter, RootFolder, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make_cbz(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


def make_series(tmp_path):
    folder = tmp_path / "Dandadan"
    make_cbz(folder / "Dandadan v01.cbz")
    series = Series(
        id=1, title="Dandadan", alt_titles="", folder_name="Dandadan",
        root_folder=RootFolder(path=str(tmp_path)), folder_pinned=True,
    )
    archive = str(folder / "Dandadan v01.cbz")
    chapters = [
        Chapter(id=1, series_id=1, number=1.0, volume=1, downloaded=True, file_path=archive),
        Chapter(id=2, series_id=1, number=2.0, volume=1, downloaded=True, file_path=archive),
        Chapter(id=3, series_id=1, number=3.0, volume=None, downloaded=False, file_path=""),
    ]
    return series, chapters


def test_run_resync_counts_and_diff(tmp_path):
    series, chapters = make_series(tmp_path)
    # the corrected map moves ch2 into volume 2 (whose archive doesn't exist)
    # and places ch3
    new_map = {1.0: 1, 2.0: 2, 3.0: 2}

    assigned, changed, repointed, cleared, diff = _run_resync(series, chapters, new_map)

    assert (assigned, changed, repointed, cleared) == (3, 2, 0, 1)
    assert diff == [(2.0, 1, 2), (3.0, None, 2)]
    assert chapters[0].downloaded and chapters[0].file_path.endswith("v01.cbz")
    assert not chapters[1].downloaded  # un-pointed from the v01 archive, no v02 file
    assert chapters[1].volume == 2


def test_dry_run_copies_carry_the_full_resulting_mapping(tmp_path):
    """The preview's full-mapping view is read off the dry-run copies after
    _run_resync — they must hold every chapter's post-apply volume, including
    ones the map doesn't place (shown as unassigned)."""
    series, chapters = make_series(tmp_path)
    new_map = {1.0: 1, 2.0: 2}  # ch3 deliberately unplaced

    copies = _chapter_copies(chapters)
    _run_resync(series, copies, new_map)

    mapping = sorted((c.number, c.volume) for c in copies)
    assert mapping == [(1.0, 1), (2.0, 2), (3.0, None)]


def test_dry_run_on_copies_leaves_real_chapters_untouched(tmp_path):
    series, chapters = make_series(tmp_path)
    new_map = {1.0: 1, 2.0: 2, 3.0: 2}

    copies = _chapter_copies(chapters)
    assigned, changed, repointed, cleared, diff = _run_resync(series, copies, new_map)

    assert (assigned, changed, cleared) == (3, 2, 1)
    assert len(diff) == 2
    # the real chapters still carry their pre-resync state
    assert [c.volume for c in chapters] == [1, 1, None]
    assert chapters[1].downloaded and chapters[1].file_path.endswith("v01.cbz")
