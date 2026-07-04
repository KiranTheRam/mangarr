import zipfile
from xml.etree.ElementTree import fromstring

from mangarr.download.cbz import build_comicinfo, guess_extension, write_cbz

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8


class TestGuessExtension:
    def test_png(self):
        assert guess_extension(PNG) == ".png"

    def test_jpg(self):
        assert guess_extension(JPG) == ".jpg"

    def test_fallback(self):
        assert guess_extension(b"unknown-format") == ".jpg"


class TestBuildComicinfo:
    def test_fields(self):
        xml = build_comicinfo(
            "Ashita no Joe", number=2, volume=1, title="The Man",
            summary="Boxing.", web="https://example.com", page_count=20,
        )
        root = fromstring(xml)
        assert root.tag == "ComicInfo"
        get = lambda tag: root.findtext(tag)
        assert get("Series") == "Ashita no Joe"
        assert get("Number") == "2"
        assert get("Volume") == "1"
        assert get("Title") == "The Man"
        assert get("Summary") == "Boxing."
        assert get("PageCount") == "20"
        assert get("Manga") == "YesAndRightToLeft"

    def test_fractional_number_kept(self):
        root = fromstring(build_comicinfo("X", number=10.5))
        assert root.findtext("Number") == "10.5"

    def test_empty_fields_omitted(self):
        root = fromstring(build_comicinfo("X"))
        assert root.find("Number") is None
        assert root.find("Volume") is None
        assert root.find("Summary") is None


class TestWriteCbz:
    def test_roundtrip(self, tmp_path):
        dest = tmp_path / "sub" / "out.cbz"
        result = write_cbz(dest, [PNG, JPG, PNG], build_comicinfo("X", number=1))
        assert result == dest
        assert dest.exists()
        assert not dest.with_suffix(".cbz.partial").exists()
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
            assert names[0] == "ComicInfo.xml"
            assert names[1:] == ["001.png", "002.jpg", "003.png"]
            assert zf.read("002.jpg") == JPG

    def test_page_name_padding_grows(self, tmp_path):
        dest = tmp_path / "big.cbz"
        write_cbz(dest, [PNG] * 1200, "<ComicInfo/>")
        with zipfile.ZipFile(dest) as zf:
            assert "0001.png" in zf.namelist()
            assert "1200.png" in zf.namelist()
