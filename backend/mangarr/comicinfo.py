"""Safe, bounded ComicInfo.xml inspection and in-place archive editing."""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .library.matcher import IMAGE_EXTS, MAX_COMICINFO_BYTES


class ComicInfoError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComicInfoMetadata:
    exists: bool
    series: str | None
    number: str | None
    volume: str | None
    title: str | None
    page_count: str | None
    actual_page_count: int

    def value(self, field: str) -> str | None:
        return {
            "series": self.series,
            "number": self.number,
            "volume": self.volume,
            "title": self.title,
            "page_count": self.page_count,
        }[field]


def _comicinfo_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    return next(
        (
            info
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).name.lower() == "comicinfo.xml"
        ),
        None,
    )


def _text(root: ElementTree.Element | None, tag: str) -> str | None:
    if root is None:
        return None
    value = " ".join((root.findtext(tag) or "").split())
    return value or None


def read_comicinfo(path: Path) -> ComicInfoMetadata:
    if path.suffix.lower() not in {".cbz", ".zip"}:
        raise ComicInfoError("ComicInfo editing is supported for CBZ and ZIP files")
    try:
        with zipfile.ZipFile(path) as archive:
            images = sum(
                1
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).suffix.lower() in IMAGE_EXTS
            )
            member = _comicinfo_member(archive)
            if member is None:
                return ComicInfoMetadata(False, None, None, None, None, None, images)
            if member.file_size > MAX_COMICINFO_BYTES or member.compress_type not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            }:
                raise ComicInfoError("ComicInfo.xml is too large or uses unsupported compression")
            with archive.open(member) as stream:
                data = stream.read(MAX_COMICINFO_BYTES + 1)
            if len(data) > MAX_COMICINFO_BYTES:
                raise ComicInfoError("ComicInfo.xml is too large")
            root = ElementTree.fromstring(data)
    except ComicInfoError:
        raise
    except (OSError, KeyError, ValueError, RuntimeError, EOFError,
            zipfile.BadZipFile, zlib.error, ElementTree.ParseError) as exc:
        raise ComicInfoError(f"Cannot read ComicInfo.xml: {exc}") from exc
    return ComicInfoMetadata(
        True,
        _text(root, "Series"),
        _text(root, "Number"),
        _text(root, "Volume"),
        _text(root, "Title"),
        _text(root, "PageCount"),
        images,
    )


_TAGS = {
    "series": "Series",
    "number": "Number",
    "volume": "Volume",
    "title": "Title",
    "page_count": "PageCount",
}


def update_comicinfo(path: Path, updates: dict[str, str | int | None]) -> None:
    """Atomically replace selected ComicInfo fields while preserving the archive.

    ``None`` or an empty string removes the field. Unknown ComicInfo fields and
    every non-metadata member are retained.
    """
    unknown = set(updates) - set(_TAGS)
    if unknown:
        raise ComicInfoError(f"Unsupported ComicInfo field: {sorted(unknown)[0]}")
    try:
        source_stat = path.stat()
        with zipfile.ZipFile(path) as source:
            member = _comicinfo_member(source)
            if member is None:
                root = ElementTree.Element("ComicInfo")
            else:
                if member.file_size > MAX_COMICINFO_BYTES or member.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise ComicInfoError(
                        "ComicInfo.xml is too large or uses unsupported compression"
                    )
                with source.open(member) as stream:
                    data = stream.read(MAX_COMICINFO_BYTES + 1)
                if len(data) > MAX_COMICINFO_BYTES:
                    raise ComicInfoError("ComicInfo.xml is too large")
                root = ElementTree.fromstring(data)

            for field, raw in updates.items():
                tag = _TAGS[field]
                for existing in list(root.findall(tag)):
                    root.remove(existing)
                value = " ".join(str(raw).split()) if raw is not None else ""
                if value:
                    ElementTree.SubElement(root, tag).text = value

            payload = ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True, short_empty_elements=True
            )
            if len(payload) > MAX_COMICINFO_BYTES:
                raise ComicInfoError("Updated ComicInfo.xml is too large")

            handle = tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                delete=False,
            )
            temp_path = Path(handle.name)
            handle.close()
            try:
                with zipfile.ZipFile(temp_path, "w", allowZip64=True) as target:
                    target.comment = source.comment
                    for info in source.infolist():
                        if Path(info.filename).name.lower() == "comicinfo.xml":
                            continue
                        if info.is_dir():
                            target.writestr(info, b"")
                            continue
                        with source.open(info) as incoming, target.open(info, "w") as outgoing:
                            while chunk := incoming.read(1024 * 1024):
                                outgoing.write(chunk)
                    target.writestr("ComicInfo.xml", payload, compress_type=zipfile.ZIP_DEFLATED)
                os.chmod(temp_path, stat.S_IMODE(source_stat.st_mode))
                os.replace(temp_path, path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
    except ComicInfoError:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile,
            zlib.error, ElementTree.ParseError) as exc:
        raise ComicInfoError(f"Cannot update ComicInfo.xml: {exc}") from exc
