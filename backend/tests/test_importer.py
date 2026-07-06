import zipfile

import pytest

from mangarr.library.importer import import_torrent_payload
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def series():
    return Series(id=1, title="Ashita no Joe", folder_name="")


@pytest.fixture
def chapters():
    return [
        Chapter(id=n, series_id=1, number=float(n), volume=1, title="")
        for n in range(1, 6)
    ]


def run_import(content_path, series, chapters, root):
    return import_torrent_payload(
        content_path, series, chapters, root,
        DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
    )


def make_cbz(path, pages=2):
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i:03d}.png", PNG)


class TestImportTorrentPayload:
    def test_single_cbz_matched_to_chapter(self, tmp_path, series, chapters):
        src = tmp_path / "payload" / "Ashita no Joe - c002 (v01).cbz"
        src.parent.mkdir()
        make_cbz(src)

        imported = run_import(src, series, chapters, tmp_path / "lib")

        assert len(imported) == 1
        dest, chapter, volume = imported[0]
        assert dest.name == "Ashita no Joe - Ch. 0002.cbz"
        assert dest.exists()
        assert chapter is chapters[1]
        assert volume is None  # chapter matched, so no volume-level claim

    def test_mixed_archives_and_loose_image_dirs(self, tmp_path, series, chapters):
        payload = tmp_path / "Ashita no Joe (Digital)"
        payload.mkdir()
        make_cbz(payload / "Ashita no Joe - c002.cbz")
        loose = payload / "Ashita no Joe - Chapter 3"
        loose.mkdir()
        (loose / "p01.png").write_bytes(PNG)
        (loose / "p02.png").write_bytes(PNG)

        imported = run_import(payload, series, chapters, tmp_path / "lib")

        by_chapter = {ch.number: dest for dest, ch, _ in imported if ch is not None}
        assert set(by_chapter) == {2.0, 3.0}
        # loose images were zipped into a CBZ
        with zipfile.ZipFile(by_chapter[3.0]) as zf:
            assert zf.namelist() == ["p01.png", "p02.png"]

    def test_volume_archive_without_chapter_match(self, tmp_path, series, chapters):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Ashita no Joe v07.zip")

        imported = run_import(payload, series, chapters, tmp_path / "lib")

        assert len(imported) == 1
        dest, chapter, volume = imported[0]
        assert chapter is None
        assert volume == 7  # caller can mark all chapters of vol 7 downloaded
        assert dest.name == "Ashita no Joe - Vol. 07.cbz"  # .zip renamed to .cbz
        assert dest.exists()

    def test_unmatched_archive_keeps_original_stem(self, tmp_path, series, chapters):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Extras and Omake.cbz")

        imported = run_import(payload, series, chapters, tmp_path / "lib")

        dest, chapter, volume = imported[0]
        assert chapter is None
        assert volume is None
        assert dest.name == "Ashita no Joe - Extras and Omake.cbz"

    def test_scene_style_batch_payload(self, tmp_path, series, chapters):
        # the Kagurabachi case: per-volume archives + per-chapter files with
        # year/quality/group tags after the chapter number
        payload = tmp_path / "Ashita no Joe v01-02 + 003-005 (2026) (Digital) (1r0n)"
        payload.mkdir()
        make_cbz(payload / "Ashita no Joe v01 (2026) (Digital) (1r0n).cbz")
        make_cbz(payload / "Ashita no Joe 004 (2026) (Digital) (1r0n).cbz")

        imported = run_import(payload, series, chapters, tmp_path / "lib")

        by_dest = {dest.name: (ch, vol) for dest, ch, vol in imported}
        assert by_dest["Ashita no Joe - Vol. 01.cbz"] == (None, 1)
        chapter, volume = by_dest["Ashita no Joe - Ch. 0004.cbz"]
        assert chapter is chapters[3]
        assert volume is None

    def test_existing_files_not_overwritten(self, tmp_path, series, chapters):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Ashita no Joe - c002 (v01).cbz")
        lib = tmp_path / "lib"

        first = run_import(payload, series, chapters, lib)
        mtime = first[0][0].stat().st_mtime_ns
        second = run_import(payload, series, chapters, lib)

        assert second[0][0].stat().st_mtime_ns == mtime
