import zipfile

import pytest
from fastapi import HTTPException

from mangarr.api.library import _require_series_media_path
from mangarr.models import RootFolder, Series


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


def test_manual_mapping_accepts_only_series_media(tmp_path):
    root = tmp_path / "library"
    folder = root / "Series"
    outside = tmp_path / "outside.cbz"
    folder.mkdir(parents=True)
    inside = folder / "Series - Ch. 0001.cbz"
    make(inside)
    make(outside)
    series = Series(
        id=1,
        title="Series",
        folder_name="Series",
        root_folder=RootFolder(path=str(root)),
    )

    assert _require_series_media_path(series, str(inside)) == inside
    with pytest.raises(HTTPException):
        _require_series_media_path(series, str(outside))
