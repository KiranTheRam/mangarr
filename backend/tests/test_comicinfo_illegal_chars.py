from xml.etree.ElementTree import fromstring

from mangarr.download.cbz import build_comicinfo


def test_xml_illegal_characters_are_dropped():
    xml = build_comicinfo(
        "Ashita\x1b no Joe", title="The\x0c Man", summary="Boxing\x00.￾",
    )
    root = fromstring(xml)
    assert root.findtext("Series") == "Ashita no Joe"
    assert root.findtext("Title") == "The Man"
    assert root.findtext("Summary") == "Boxing."


def test_tab_newline_and_non_bmp_characters_are_kept():
    root = fromstring(build_comicinfo("X", summary="Line one\nLine two\tIndented 🥊"))
    assert root.findtext("Summary") == "Line one\nLine two\tIndented 🥊"
