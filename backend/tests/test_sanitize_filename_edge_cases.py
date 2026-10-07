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


# a 200-byte series and a 35-byte title run the finished name 4 bytes over;
# cutting its end used to drop the chapter number
TITLED = "{series} {title} - Ch. {chapter:04.1f}"


def fits(name):
    return len(f"{name}.partial".encode()) <= 255


def test_a_long_title_gives_way_before_the_chapter_number():
    one, two = (chapter_filename(TITLED, TITLED, "S" * 200, n, None, "T" * 35) for n in (1, 2))
    assert one.endswith(" - Ch. 0001.cbz") and two.endswith(" - Ch. 0002.cbz")
    assert one.startswith("S" * 200 + " TTT")  # the title is shortened, not dropped
    assert fits(one) and fits(two)


@pytest.mark.parametrize("template", [
    DEFAULT_TEMPLATE, TITLED, "{series} - {title} - Ch. {chapter:04.1f}",
    "{title} - Ch. {chapter:04.1f}", "{series} - Vol. {volume:02d} Ch. {chapter:04.1f} - {title}",
])
def test_long_title_keeps_the_chapter_number(template):
    result = chapter_filename(template, template, "Series", 12.5, 3, LONG_TITLE)
    assert "Ch. 0012.5" in result
    assert result.startswith(("Series", "とても"))
    assert fits(result)


def test_a_template_too_long_by_itself_still_names_chapters_apart():
    template = "{series} " + "x" * 250 + " {chapter:04.1f}"
    one, two = (chapter_filename(template, template, "Series", n) for n in (1, 2))
    assert one.endswith(" 0001.cbz") and two.endswith(" 0002.cbz")
    assert fits(one) and fits(two)


def test_reserved_name_suffix_stays_within_the_cap():
    assert sanitize_filename("aux.xyz", 7) == "aux_.xy"
    result = sanitize_filename("CON." + "a" * 196)
    assert result.startswith("CON_.") and len(result.encode()) == 200
    for reserved in ("CON", "aux", "Com1", "NUL "):
        for max_bytes in range(4, 60):
            name = sanitize_filename(f"{reserved}.{'é' * 40}", max_bytes)
            assert len(name.encode()) <= max_bytes, (reserved, max_bytes, name)
    template = "{series}.{title} - Ch. {chapter:04.1f}"
    name = chapter_filename(template, template, "Nul", 1, None, "x" * 300)
    assert name.startswith("Nul_.x") and name.endswith(" - Ch. 0001.cbz")
    assert fits(name)


@pytest.mark.parametrize("char", ["a", "é", "長", "😀"])
@pytest.mark.parametrize("template", [DEFAULT_TEMPLATE, TITLED,
                                      "{title} {series} - Vol. {volume:02d} Ch. {chapter:04.1f}"])
def test_every_name_fits_and_chapters_never_share_one(char, template):
    width = len(char.encode())
    for series_bytes in range(0, 300, 7):
        for title_bytes in (0, 1, 30, 35, 47, 120, 600):
            series, title = char * (series_bytes // width), char * (title_bytes // width)
            names = [chapter_filename(template, template, series, n, 3, title)
                     for n in (1, 2, 12.5, 100, 1000.25)]
            assert all(fits(name) for name in names), names
            assert len(set(names)) == len(names), names
            volumes = [volume_filename(series, v) for v in (1, 2, 10)]
            assert all(fits(name) for name in volumes)
            assert len(set(volumes)) == len(volumes)


def test_torrent_import_gives_two_chapters_two_files(tmp_path):
    import zipfile

    from mangarr.library.importer import import_torrent_payload
    from mangarr.models import Chapter, Series

    payload = tmp_path / "payload"
    payload.mkdir()
    for number, pages in ((1, 2), (2, 5)):
        with zipfile.ZipFile(payload / f"Series - c00{number}.cbz", "w") as zf:
            for i in range(pages):
                zf.writestr(f"{i:03d}.png", b"\x89PNG\r\n\x1a\n" + bytes([i]))
    series = Series(id=1, title="S" * 200, folder_name="Series")
    chapters = [Chapter(id=n, series_id=1, number=float(n), title="T" * 35) for n in (1, 2)]

    imported = import_torrent_payload(payload, series, chapters, tmp_path / "lib", TITLED, TITLED)

    assert len({dest for dest, _, _ in imported}) == 2
    pages = {chapter.number: len(zipfile.ZipFile(dest).namelist())
             for dest, chapter, _ in imported}
    assert pages == {1.0: 2, 2.0: 5}
