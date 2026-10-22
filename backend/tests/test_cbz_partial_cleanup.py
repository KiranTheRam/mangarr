import errno
import zipfile

import pytest

from mangarr.download.cbz import write_cbz

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_failed_write_leaves_no_partial_archive(tmp_path, monkeypatch):
    real_writestr = zipfile.ZipFile.writestr

    def disk_full_after_comicinfo(self, name, data, *args, **kwargs):
        if name != "ComicInfo.xml":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_writestr(self, name, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", disk_full_after_comicinfo)
    dest = tmp_path / "Series" / "Series - Ch. 0001.cbz"

    with pytest.raises(OSError, match="No space left"):
        write_cbz(dest, [PNG, PNG], "<ComicInfo/>")

    assert list(dest.parent.iterdir()) == []


def test_successful_write_leaves_only_the_archive(tmp_path):
    dest = tmp_path / "Series - Ch. 0001.cbz"
    write_cbz(dest, [PNG], "<ComicInfo/>")
    assert list(tmp_path.iterdir()) == [dest]
