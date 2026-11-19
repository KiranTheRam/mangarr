from xml.etree import ElementTree
from zipfile import ZipFile

from mangarr.comicinfo import update_comicinfo


def test_existing_field_keeps_its_comicinfo_schema_position(tmp_path):
    path = tmp_path / "chapter.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Series>Tracked</Series><Number>9</Number>"
            "<PageCount>1</PageCount><LanguageISO>en</LanguageISO>"
            '<Pages><Page Image="0" Type="FrontCover"/></Pages></ComicInfo>',
        )

    update_comicinfo(path, {"number": "1"})

    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("ComicInfo.xml"))
    assert [child.tag for child in root] == ["Series", "Number", "PageCount", "LanguageISO", "Pages"]
    assert root.findtext("Number") == "1"
    assert root.find("Pages/Page").attrib == {"Image": "0", "Type": "FrontCover"}


def test_missing_fields_follow_comicinfo_schema_order_regardless_of_update_order(tmp_path):
    path = tmp_path / "chapter.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Series>Tracked</Series><Count>12</Count>"
            "<AlternateSeries>Other</AlternateSeries><Web>https://example.com</Web>"
            "<LanguageISO>en</LanguageISO></ComicInfo>",
        )

    update_comicinfo(path, {"page_count": 5, "volume": 1, "number": "2", "title": "Bonus"})

    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("ComicInfo.xml"))
    assert [child.tag for child in root] == [
        "Title", "Series", "Number", "Count", "Volume", "AlternateSeries", "Web",
        "PageCount", "LanguageISO",
    ]
