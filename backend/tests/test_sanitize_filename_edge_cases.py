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
    start, _, digest = result.rpartition("~")
    assert len(result.encode()) <= 200
    # 191 bytes of title, then "~" and an 8-digit digest of the whole title
    assert LONG_TITLE.startswith(start) and len(start) == 191 // 3
    assert len(digest) == 8 and digest.isdecimal()


def test_a_cut_does_not_leave_a_trailing_space():
    assert sanitize_filename("a" * 190 + " b" + "c" * 20).startswith("a" * 190 + "~")


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


def reads_back(name, chapter=None, volume=None):
    """The library scanner, reading name, finds this chapter or volume."""
    from pathlib import Path

    from mangarr.library.matcher import _media_of

    media = _media_of(Path("/library/Series") / name, is_dir=False)
    return ((chapter is None or media.chapter_number == chapter)
            and (volume is None or media.volume_number == volume))


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
            assert all(reads_back(name, chapter=n)
                       for name, n in zip(names, (1, 2, 12.5, 100, 1000.25))), names
            volumes = [volume_filename(series, v) for v in (1, 2, 10)]
            assert all(fits(name) for name in volumes)
            assert len(set(volumes)) == len(volumes)
            assert all(reads_back(name, volume=v) for name, v in zip(volumes, (1, 2, 10)))


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


def test_long_titles_that_start_alike_get_their_own_folders_and_files():
    part1, part2 = "A" * 200 + "B", "A" * 200 + "C"
    assert series_folder(part1) != series_folder(part2)
    assert series_folder(part1) == series_folder(part1)  # stable
    assert all(len(series_folder(t).encode()) <= 200 for t in (part1, part2))
    assert (chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, part1, 1)
            != chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, part2, 1))
    assert volume_filename(series_folder(part1), 1) != volume_filename(series_folder(part2), 1)


def test_torrent_import_keeps_series_that_start_alike_apart(tmp_path):
    import zipfile

    from mangarr.library.importer import import_torrent_payload
    from mangarr.models import Chapter, Series

    placed = {}
    for title, pages in (("A" * 200 + "B", 2), ("A" * 200 + "C", 5)):
        payload = tmp_path / f"payload{pages}"
        payload.mkdir()
        with zipfile.ZipFile(payload / "Series - c001.cbz", "w") as zf:
            for i in range(pages):
                zf.writestr(f"{i:03d}.png", b"\x89PNG\r\n\x1a\n" + bytes([i]))
        # create_series names the folder the same way
        series = Series(id=pages, title=title, folder_name=sanitize_filename(title))
        chapter = Chapter(id=pages, series_id=pages, number=1.0, title="")
        [(dest, _, _)] = import_torrent_payload(payload, series, [chapter], tmp_path / "lib",
                                                DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)
        placed[pages] = dest

    assert placed[2] != placed[5]
    assert {n: len(zipfile.ZipFile(dest).namelist()) for n, dest in placed.items()} == {2: 2, 5: 5}


@pytest.mark.parametrize("padding", ["?" * 10, " " * 10, "?" * 300, " ?" * 150, ":*" * 40],
                         ids=["question-marks", "spaces", "300-question-marks", "mixed", "colons"])
def test_a_title_padded_with_dropped_characters_is_shortened_not_dropped(padding):
    padded = chapter_filename(TITLED, TITLED, "S" * 200, 1, None, "T" * 40 + padding)
    assert padded == chapter_filename(TITLED, TITLED, "S" * 200, 1, None, "T" * 40)
    assert padded.endswith(" " + "T" * 31 + " - Ch. 0001.cbz")
    assert fits(padded)


def test_a_cut_digest_is_not_read_as_a_chapter_or_volume_number():
    from mangarr.util import parse_chapter_number, parse_volume_number

    for stem in ("c", "ch", "Vol", "v", "Magic ", "長"):
        series = stem * (300 // len(stem.encode()))
        for n in (1, 12.5, 300):
            name = chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, series, n)
            assert "~" in name and parse_chapter_number(name[:-4]) == n, name
        name = volume_filename(series_folder(series), 7)
        assert parse_volume_number(name[:-4]) == 7, name


@pytest.mark.parametrize("char", ["a", "é", "長", "😀"])
def test_series_sharing_a_long_start_never_share_a_folder_or_file(char):
    width = len(char.encode())
    for series_bytes in range(150, 300, 11):
        start = char * (series_bytes // width)
        titles = [start + end for end in ("1", "2", "B", " Part 2", "é")]
        folders = [series_folder(title) for title in titles]
        assert len(set(folders)) == len(folders), folders
        assert all(len(folder.encode()) <= 200 for folder in folders)
        for template in (DEFAULT_TEMPLATE, TITLED):
            names = [chapter_filename(template, template, title, n, None, start)
                     for title in titles for n in (1, 2)]
            assert all(fits(name) for name in names), names
            assert all(reads_back(name, chapter=n) for name, n in zip(names, (1, 2) * 5)), names
            assert len(set(names)) == len(names), names
            # a chapter title padded with characters the name drops anyway
            for pad in ("  ", "??", " ?:*", "?" * 300):
                for title_bytes in (10, 35, 47):
                    chapter_title = char * (title_bytes // width)
                    assert (chapter_filename(template, template, titles[0], 1, None,
                                             chapter_title + pad)
                            == chapter_filename(template, template, titles[0], 1, None,
                                                chapter_title))


# templates whose own text doesn't fit in a file name, with the chapter marker
# at the end, in the middle, at the start, and one that fits only short numbers
OVERSIZED_TEMPLATES = [
    "x" * 220 + " Ch. {chapter:04.1f}" + "x" * 30,
    "x" * 235 + " Ch. {chapter:04.1f}",
    "x" * 120 + " Ch. {chapter:04.1f} {title} " + "x" * 120,
    "Ch. {chapter:04.1f} " + "x" * 240,
    "{series} - Vol. {volume:02d} " + "y" * 230 + " c{chapter:04.1f}",
    "{series} " + "x" * 230 + " - Ch. {chapter:04.1f}",
]


@pytest.mark.parametrize("template", OVERSIZED_TEMPLATES, ids=[
    "marker-before-text", "marker-at-end", "marker-in-middle", "marker-at-start",
    "c-marker-at-end", "fits-short-numbers",
])
def test_a_template_too_long_by_itself_never_hides_the_chapter_number(template):
    numbers = (0.5, 1, 2, 7, 12.5, 100, 1000.25, 12.21, 12.24)
    for series in ("Series", "S" * 200, LONG_TITLE):
        names = [chapter_filename(template, template, series, n, 3, "Title") for n in numbers]
        assert all(fits(name) for name in names), names
        assert len(set(names)) == len(names), names
        assert all(reads_back(name, chapter=n) for name, n in zip(names, numbers)), names


def test_a_template_too_long_by_itself_names_files_like_the_default(monkeypatch, caplog):
    from mangarr.library import naming

    monkeypatch.setattr(naming, "_TOO_LONG_TEMPLATES", set())
    template = OVERSIZED_TEMPLATES[0]
    with caplog.at_level("WARNING", logger="mangarr.library.naming"):
        for n in (1000.25, 1, 12.5):
            assert (chapter_filename(template, template, "Series", n)
                    == chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, "Series", n))
    assert sum("too long" in record.getMessage() for record in caplog.records) == 1


def test_a_number_too_long_for_any_name_still_fits():
    huge = [float("1" * 300), float("2" * 300)]
    names = [chapter_filename(DEFAULT_TEMPLATE, DEFAULT_TEMPLATE, "Series", n) for n in huge]
    assert all(fits(name) for name in names) and names[0] != names[1]
    volumes = [volume_filename("Series", v) for v in (10**300, 2 * 10**300)]
    assert all(fits(name) for name in volumes) and volumes[0] != volumes[1]
