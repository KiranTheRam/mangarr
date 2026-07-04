from pathlib import Path

from mangarr.library.naming import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_NO_VOLUME,
    chapter_filename,
    chapter_path,
    series_folder,
)


def name(chapter: float, volume: int | None = None, title: str = "") -> str:
    return chapter_filename(
        DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME, "Ashita no Joe", chapter, volume, title
    )


class TestChapterFilename:
    def test_whole_chapter_with_volume(self):
        assert name(21, volume=3) == "Ashita no Joe - Vol. 03 Ch. 0021.cbz"

    def test_fractional_chapter(self):
        assert name(21.5, volume=3) == "Ashita no Joe - Vol. 03 Ch. 0021.5.cbz"

    def test_no_volume_template(self):
        assert name(7) == "Ashita no Joe - Ch. 0007.cbz"

    def test_series_name_sanitized(self):
        result = chapter_filename(
            DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
            "Ashita no Joe: Fighting for Tomorrow", 1, 1,
        )
        assert result == "Ashita no Joe Fighting for Tomorrow - Vol. 01 Ch. 0001.cbz"

    def test_custom_template_with_title(self):
        result = chapter_filename(
            "{series} {chapter:04.1f} - {title}", "{series} {chapter:04.1f} - {title}",
            "Dandadan", 5, None, "Like a Ghost Story",
        )
        assert result == "Dandadan 0005 - Like a Ghost Story.cbz"


class TestSeriesFolder:
    def test_sanitizes(self):
        assert series_folder("Dr. STONE: reboot?") == "Dr. STONE reboot"


class TestChapterPath:
    def test_full_path(self):
        p = chapter_path(
            Path("/library"), DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
            "Ashita no Joe", "", 2, 1,
        )
        assert p == Path("/library/Ashita no Joe/Ashita no Joe - Vol. 01 Ch. 0002.cbz")

    def test_explicit_folder_name_wins(self):
        p = chapter_path(
            Path("/library"), DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
            "Ashita no Joe", "Custom Folder", 2, 1,
        )
        assert p.parent == Path("/library/Custom Folder")
