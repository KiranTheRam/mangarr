"""A pinned series folder is the user's explicit choice: scans must not
re-adopt a title-matching existing folder over it (the pre-pin behavior made
"create a new folder for this series" impossible — every scan re-attached
the wrong folder as long as the chosen one didn't exist on disk yet)."""

from mangarr.jobs.tasks import _series_folders
from mangarr.models import RootFolder, Series


def make_series(tmp_path, **kw):
    return Series(
        title="Solo Leveling",
        alt_titles="",
        folder_name="Solo Leveling (2023)",
        root_folder=RootFolder(path=str(tmp_path)),
        **kw,
    )


def test_unpinned_adopts_matching_existing_folder(tmp_path):
    (tmp_path / "Solo Leveling").mkdir()
    series = make_series(tmp_path, folder_pinned=False)

    folders = _series_folders(series)

    assert series.folder_name == "Solo Leveling"
    assert folders[0] == tmp_path / "Solo Leveling"


def test_pinned_folder_is_never_readopted(tmp_path):
    # a title-matching folder exists (belonging to some other series), but
    # the user explicitly asked for a new folder
    (tmp_path / "Solo Leveling").mkdir()
    series = make_series(tmp_path, folder_pinned=True)

    folders = _series_folders(series)

    assert series.folder_name == "Solo Leveling (2023)"
    assert folders[0] == tmp_path / "Solo Leveling (2023)"
