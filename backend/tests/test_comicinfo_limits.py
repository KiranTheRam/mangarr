import io
import zipfile
from unittest.mock import Mock

import pytest

from mangarr.library import matcher


@pytest.mark.parametrize("reader,expected", [(matcher.comicinfo_title, "A title"), (matcher.comicinfo_number, 12.5)])
def test_ordinary_nested_metadata(tmp_path, reader, expected):
    path = tmp_path / "chapter.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("nested/COMICINFO.XML", "<ComicInfo><Title> A   title </Title><Number>12.5</Number></ComicInfo>")
    assert reader(path) == expected


@pytest.mark.parametrize("reader,fallback", [(matcher.comicinfo_title, ""), (matcher.comicinfo_number, None)])
def test_compressed_oversized_metadata_not_opened(tmp_path, monkeypatch, reader, fallback):
    path = tmp_path / "huge.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ComicInfo.xml", b"x" * (matcher.MAX_COMICINFO_BYTES + 1))
    open_member = Mock(side_effect=AssertionError("oversized entry must not be decompressed"))
    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)
    assert reader(path) == fallback
    open_member.assert_not_called()


def test_bounded_stream_even_if_declared_size_is_small(monkeypatch):
    info = zipfile.ZipInfo("ComicInfo.xml")
    info.file_size = 10
    archive = Mock()
    archive.infolist.return_value = [info]
    stream = Mock(wraps=io.BytesIO(b"x" * (matcher.MAX_COMICINFO_BYTES + 5)))
    archive.open.return_value.__enter__ = Mock(return_value=stream)
    archive.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(zipfile, "ZipFile", Mock(return_value=Mock(__enter__=Mock(return_value=archive), __exit__=Mock(return_value=False))))
    assert matcher._read_comicinfo_title(None) == ""
    stream.read.assert_called_once_with(matcher.MAX_COMICINFO_BYTES + 1)
    archive.open.assert_called_once_with(info)


def test_duplicate_name_opens_checked_entry(tmp_path):
    path = tmp_path / "duplicate.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>first</Title><Number>1</Number></ComicInfo>")
        with pytest.warns(UserWarning):
            archive.writestr("ComicInfo.xml", b"x" * (matcher.MAX_COMICINFO_BYTES + 1))
    assert matcher.comicinfo_title(path) == "first"
    assert matcher.comicinfo_number(path) == 1


@pytest.mark.parametrize("data", [b"bad xml", b"<ComicInfo><Number>bad</Number></ComicInfo>"])
def test_invalid_metadata_falls_back(tmp_path, data):
    path = tmp_path / "invalid.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", data)
    assert matcher.comicinfo_title(path) == ""
    assert matcher.comicinfo_number(path) is None


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
@pytest.mark.parametrize("reader,fallback", [(matcher.comicinfo_title, ""), (matcher.comicinfo_number, None)])
def test_forged_size_cannot_reach_unbounded_decompressors(tmp_path, monkeypatch, compression, reader, fallback):
    import struct

    path = tmp_path / "forged.cbz"
    with zipfile.ZipFile(path, "w", compression) as archive:
        archive.writestr("ComicInfo.xml", b"x" * (2 * matcher.MAX_COMICINFO_BYTES))
    data = bytearray(path.read_bytes())
    # Forge both uncompressed-size fields while keeping the compressed stream.
    struct.pack_into("<I", data, data.index(b"PK\x03\x04") + 22, 10)
    struct.pack_into("<I", data, data.index(b"PK\x01\x02") + 24, 10)
    path.write_bytes(data)
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[0].file_size == 10
    open_member = Mock(side_effect=AssertionError("unsafe decompressor must not be reached"))
    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)
    assert reader(path) == fallback
    open_member.assert_not_called()
