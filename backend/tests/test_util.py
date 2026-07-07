from mangarr.util import (
    normalize_title,
    parse_chapter_number,
    parse_volume_number,
    sanitize_filename,
)


class TestParseChapterNumber:
    def test_chapter_word(self):
        assert parse_chapter_number("Ashita no Joe - Chapter 3") == 3.0

    def test_ch_prefix(self):
        assert parse_chapter_number("Dandadan Ch 172") == 172.0

    def test_dotted_ch_prefix(self):
        # our own default naming format must round-trip
        assert parse_chapter_number("Ashita no Joe - Vol. 01 Ch. 0021.cbz") == 21.0

    def test_c_prefix(self):
        assert parse_chapter_number("Ashita no Joe - c002 (v01)") == 2.0

    def test_decimal(self):
        assert parse_chapter_number("One Piece ch10.5 extras") == 10.5

    def test_trailing_number(self):
        assert parse_chapter_number("Grand Blue 042") == 42.0

    def test_no_match(self):
        assert parse_chapter_number("Complete Series Batch") is None

    def test_underscore_separated_chapter(self):
        # "[0001]_Chapter_1_-_Her name" — underscore before "Chapter"
        assert parse_chapter_number("[0001]_Chapter_1_-_Her name is Urumin") == 1.0
        assert parse_chapter_number("Series_Chapter_42") == 42.0

    def test_scene_style_tags_after_number(self):
        # number followed by (year) (quality) (group) tags
        assert parse_chapter_number("Kagurabachi 057 (2024) (Digital) (1r0n)") == 57.0
        assert parse_chapter_number("One Piece - 1044 [Viz]") == 1044.0

    def test_year_alone_is_not_a_chapter(self):
        assert parse_chapter_number("Kagurabachi (2024) (Digital)") is None

    def test_scene_style_volume_is_not_chapter(self):
        assert parse_chapter_number("Kagurabachi v01 (2023) (Digital) (1r0n)") is None
        assert parse_volume_number("Kagurabachi v01 (2023) (Digital) (1r0n)") == 1

    def test_volume_only_is_not_chapter(self):
        # "v01"/"v03" must not be read as a chapter number
        assert parse_chapter_number("Berserk v01") is None
        assert parse_chapter_number("Berserk (v03)") is None


class TestParseVolumeNumber:
    def test_vol(self):
        assert parse_volume_number("Berserk Vol. 3") == 3

    def test_v_short(self):
        assert parse_volume_number("Berserk v03") == 3

    def test_volume_word(self):
        assert parse_volume_number("Berserk Volume 12") == 12

    def test_none(self):
        assert parse_volume_number("Berserk Chapter 4") is None


class TestSanitizeFilename:
    def test_illegal_chars_removed(self):
        assert sanitize_filename('A:B/C\\D?E*F"G<H>I|J') == "ABCDEFGHIJ"

    def test_trailing_dot_stripped(self):
        assert sanitize_filename("Dr. Stone Vol. 1.") == "Dr. Stone Vol. 1"

    def test_whitespace_collapsed(self):
        assert sanitize_filename("A   B  C") == "A B C"

    def test_control_chars_stripped(self):
        assert sanitize_filename("A\tB\x00C") == "ABC"

    def test_empty_becomes_unknown(self):
        assert sanitize_filename("???") == "Unknown"


class TestNormalizeTitle:
    def test_case_and_punctuation(self):
        assert normalize_title("Ashita no Joe: Fighting for Tomorrow!") == (
            "ashita no joe fighting for tomorrow"
        )

    def test_matches_across_variants(self):
        assert normalize_title("KAGUYA-SAMA: Love is War") == normalize_title(
            "Kaguya sama   love is war"
        )

    def test_multiplication_sign_matches_x(self):
        assert normalize_title("Spy × Family") == normalize_title("Spy x Family")
