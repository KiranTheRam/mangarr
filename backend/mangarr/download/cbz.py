"""CBZ packaging with ComicInfo.xml (Anansi/ComicRack schema, as read by
Komga and Kavita)."""

import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

EXT_BY_SIGNATURE = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"GIF8": ".gif",
}


def guess_extension(data: bytes, fallback: str = ".jpg") -> str:
    for sig, ext in EXT_BY_SIGNATURE.items():
        if data.startswith(sig):
            return ext
    # RIFF alone is any RIFF container (wav/avi/…); webp is RIFF????WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    # ISO-BMFF: size + "ftyp" + brand; avif/avis are the AVIF brands
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    return fallback


def build_comicinfo(
    series: str,
    number: float | None = None,
    volume: int | None = None,
    title: str = "",
    summary: str = "",
    web: str = "",
    page_count: int | None = None,
) -> str:
    root = Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def add(tag: str, value) -> None:
        if value is None or value == "":
            return
        SubElement(root, tag).text = str(value)

    add("Series", series)
    if number is not None:
        add("Number", int(number) if float(number).is_integer() else number)
    add("Volume", volume)
    add("Title", title)
    add("Summary", summary)
    add("Web", web)
    add("PageCount", page_count)
    add("Manga", "YesAndRightToLeft")
    rough = tostring(root, encoding="unicode")
    return minidom.parseString(rough).toprettyxml(indent="  ")


def write_cbz(dest: Path, pages: list[bytes], comicinfo_xml: str) -> Path:
    """Writes ordered page images + ComicInfo.xml into a .cbz at dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".cbz.partial")
    width = max(3, len(str(len(pages))))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ComicInfo.xml", comicinfo_xml)
        for i, data in enumerate(pages, start=1):
            zf.writestr(f"{i:0{width}d}{guess_extension(data)}", data)
    tmp.rename(dest)
    return dest
