import pytest

from mangarr.library.naming import (
    DEFAULT_TEMPLATE,
    DEFAULT_TEMPLATE_NO_VOLUME,
    chapter_filename,
    series_folder,
    volume_filename,
)
from mangarr.util import sanitize_filename

LONG_TITLE = "とても長いタイトル" * 20  # 540 bytes of UTF-8


def test_leading_dot_does_not_hide_the_folder():
    assert sanitize_filename(".hack//Legend of the Twilight") == "hackLegend of the Twilight"


def test_no_trailing_space_after_removed_characters():
    assert sanitize_filename("Why Did I ...?") == "Why Did I"
    assert sanitize_filename("Who Are You? ") == "Who Are You"


@pytest.mark.parametrize(
    ("title", "expected"),
    [("CON", "CON_"), ("nul", "nul_"), ("Com1", "Com1_"), ("LPT9", "LPT9_"),
     ("aux.Vol 1", "aux_.Vol 1")],
)
def test_windows_reserved_names_are_suffixed(title, expected):
    assert sanitize_filename(title) == expected


@pytest.mark.parametrize("title", ["Conan", "COM10", "Aux Lab", "Nul - Ch. 0001"])
def test_names_merely_starting_like_reserved_ones_are_untouched(title):
    assert sanitize_filename(title) == title


def test_long_names_are_cut_on_a_character_boundary():
    result = sanitize_filename(LONG_TITLE)
    assert len(result.encode()) <= 200
    assert LONG_TITLE.startswith(result)
    assert len(result) == 200 // 3


def test_a_cut_does_not_leave_a_trailing_space():
    assert sanitize_filename("a" * 199 + " b") == "a" * 199


def test_long_series_keeps_the_chapter_number():
    result = chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME,
                              LONG_TITLE, 12.5, 3)
    assert result.endswith(" - Ch. 0012.5.cbz")
    assert len(f"{result}.partial".encode()) <= 255


def test_long_series_keeps_the_volume_number():
    result = volume_filename(series_folder(LONG_TITLE), 3)
    assert result.endswith(" - Vol. 03.cbz")
    assert len(result.encode()) <= 255
