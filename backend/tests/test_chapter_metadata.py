from mangarr.chapter_metadata import (
    apply_metadata_rows,
    apply_title,
    apply_volume,
    clean_title,
    is_generic_title,
    reconcile_decimal_volumes,
)
from mangarr.models import Chapter
from mangarr.sources.base import ChapterMetadata


def chapter(number, volume=None, title=""):
    return Chapter(
        id=int(number * 10), series_id=1, number=float(number), volume=volume,
        title=title, title_source="", volume_source="",
        title_locked=False, volume_locked=False,
    )


def test_generic_feed_title_does_not_replace_real_metadata():
    ch = chapter(12)
    assert not apply_title(ch, "Chapter 12", "mangadex", "Example")
    assert apply_title(ch, "A Quiet Morning", "wikipedia", "Example")
    assert ch.title == "A Quiet Morning" and ch.title_source == "wikipedia"


def test_manual_locks_are_never_overwritten():
    ch = chapter(4, 7, "My title")
    ch.title_locked = ch.volume_locked = True
    assert not apply_title(ch, "Official title", "viz", "Example")
    assert not apply_volume(ch, 2, "viz")
    assert (ch.title, ch.volume) == ("My title", 7)


def test_decimal_extras_inherit_floor_chapter_volume():
    result = reconcile_decimal_volumes({180.0: 21, 181.0: 21}, [180.5, 180.9])
    assert result[180.5] == 21 and result[180.9] == 21


def test_unnumbered_extras_pair_only_when_unambiguous():
    chapters = [chapter(10.5, 2), chapter(10.9, 2)]
    rows = [
        ChapterMetadata("wikipedia", None, 2, "Extra A", "extra"),
        ChapterMetadata("wikipedia", None, 2, "Extra B", "extra"),
    ]
    apply_metadata_rows(chapters, rows, "Example")
    assert [ch.title for ch in chapters] == ["Extra A", "Extra B"]


def test_extras_never_pair_with_unplaced_decimals_outside_the_volume_span():
    # volume 2 spans chapters 9-17; the series' only titleless decimal is
    # 25.5 (an unmapped later bonus) — it must not inherit the printed extra
    chapters = [chapter(9, 2), chapter(17, 2), chapter(25.5, None)]
    rows = [
        ChapterMetadata("wikipedia", 9.0, 2, "Nine"),
        ChapterMetadata("wikipedia", 17.0, 2, "Seventeen"),
        ChapterMetadata("wikipedia", None, 2, "A Day Off", "extra"),
    ]
    apply_metadata_rows(chapters, rows, "Example")
    bonus = chapters[2]
    assert bonus.title == "" and bonus.volume is None


def test_extra_within_volume_span_is_paired():
    chapters = [chapter(9, 2), chapter(17, 2), chapter(12.5, None)]
    rows = [
        ChapterMetadata("wikipedia", 9.0, 2, "Nine"),
        ChapterMetadata("wikipedia", 17.0, 2, "Seventeen"),
        ChapterMetadata("wikipedia", None, 2, "A Day Off", "extra"),
    ]
    apply_metadata_rows(chapters, rows, "Example")
    bonus = chapters[2]
    assert bonus.title == "A Day Off" and bonus.volume == 2


def test_clean_title_strips_only_the_chapters_own_number():
    # "Class 1-A ..." is chapter 293's real title, not a number prefix
    assert clean_title("Class 1-A vs. the Villains", 293) == "Class 1-A vs. the Villains"
    assert clean_title("Chapter 2 Trial", 40) == "Chapter 2 Trial"
    assert clean_title("Chapter 12: Duel", 12) == "Duel"
    assert clean_title("Ch. 12.5 - Beach Episode", 12.5) == "Beach Episode"


def test_generic_title_detection_handles_decimal_numbers():
    assert is_generic_title("One Piece 12.5", "One Piece", 12.5)
    assert not is_generic_title("A Quiet Morning", "One Piece", 12.5)


def test_equal_volume_from_stronger_source_upgrades_provenance():
    ch = chapter(45)
    assert apply_volume(ch, 5, "mangadex")
    assert ch.volume_source == "mangadex"
    assert apply_volume(ch, 5, "viz")
    assert (ch.volume, ch.volume_source) == (5, "viz")
    # and a weaker source can neither change the value nor the label
    assert not apply_volume(ch, 6, "mangadex")
    assert (ch.volume, ch.volume_source) == (5, "viz")

