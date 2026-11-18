"""Safe, bounded ComicInfo.xml inspection and in-place archive editing."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .filename import parse_filename
from .library.matcher import IMAGE_EXTS, MAX_COMICINFO_BYTES
from .library.zip_limits import (
    BoundedZipReader, COPY_CHUNK_BYTES, MAX_EXPANDED_BYTES, MAX_MEMBER_BYTES,
    MAX_ZIP_DIRECTORY_BYTES, MAX_ZIP_ENTRIES, SAFE_ZIP_COMPRESSION,
)


class ComicInfoError(RuntimeError):
    pass


class ArchiveChanged(ComicInfoError):
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
    revision: str
    labels_multiple_chapters: bool


_TAGS = {
    "series": "Series",
    "number": "Number",
    "volume": "Volume",
    "title": "Title",
    "page_count": "PageCount",
}
# ComicInfo 2.0 defines an ordered sequence. Index all standard fields so
# sparse documents can accept missing tags without moving existing metadata.
_TAG_ORDER = {
    tag: index
    for index, tag in enumerate((
        "Title", "Series", "Number", "Count", "Volume", "AlternateSeries",
        "AlternateNumber", "AlternateCount", "Summary", "Notes", "Year",
        "Month", "Day", "Writer", "Penciller", "Inker", "Colorist", "Letterer",
        "CoverArtist", "Editor", "Publisher", "Imprint", "Genre", "Web",
        "PageCount", "LanguageISO", "Format", "BlackAndWhite", "Manga",
        "Characters", "Teams", "Locations", "ScanInformation", "StoryArc",
        "SeriesGroup", "AgeRating", "Pages", "CommunityRating",
        "MainCharacterOrTeam", "Review",
    ))
}
_NUMBER_SPAN = re.compile(r"\d+(?:\.\d+)?(?:\s*[-–—,&/]\s*\d+(?:\.\d+)?)+")
_XML_WHITESPACE = " \t\r\n"
_XML_INTEGER = re.compile(r"[+-]?[0-9]+")
COMICINFO_INT_MAX = 2**31 - 1
_POSIX_ACCESS_ACL = "system.posix_acl_access"
_ARCHIVE_ERRORS = (
    OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile,
    zlib.error, ElementTree.ParseError,
)


def _archive_reader(path: Path, *, check_suffix: bool = True) -> BoundedZipReader:
    if check_suffix and path.suffix.lower() not in {".cbz", ".zip"}:
        raise ComicInfoError("ComicInfo editing is supported for CBZ and ZIP files")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    raw = os.fdopen(fd, "rb", buffering=0)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raw.close()
        raise ComicInfoError("Chapter archive must be a regular file")
    return BoundedZipReader(raw, MAX_ZIP_DIRECTORY_BYTES)


def _revision(file_stat: os.stat_result) -> str:
    identity = (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


def _copy_posix_access_acl(source_fd: int, target_fd: int) -> None:
    if sys.platform != "linux":
        return
    try:
        acl = os.getxattr(source_fd, _POSIX_ACCESS_ACL)
    except OSError as exc:
        if exc.errno == errno.EOPNOTSUPP:
            return
        if exc.errno != errno.ENODATA:
            raise
        # The temporary file can inherit a default ACL even when the
        # original archive has none.
        try:
            os.removexattr(target_fd, _POSIX_ACCESS_ACL)
        except OSError as remove_exc:
            if remove_exc.errno != errno.ENODATA:
                raise
    else:
        os.setxattr(target_fd, _POSIX_ACCESS_ACL, acl)


def _members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo | None, list[Path]]:
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise ComicInfoError("Archive exceeds the ZIP entry count limit")
    names = set()
    member = None
    pages = []
    for info in entries:
        if info.filename in names:
            raise ComicInfoError("Archive contains duplicate ZIP member names")
        names.add(info.filename)
        if info.compress_type not in SAFE_ZIP_COMPRESSION or info.flag_bits & 1:
            raise ComicInfoError("Archive uses unsupported ZIP compression or encryption")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ComicInfoError("Archive exceeds the expanded member size limit")
        if info.is_dir():
            if info.file_size:
                raise ComicInfoError("Archive directory contains unexpected data")
            continue
        name = Path(info.filename)
        if name.name.lower() == "comicinfo.xml":
            if member is not None:
                raise ComicInfoError("Archive contains multiple ComicInfo.xml files")
            member = info
        elif name.suffix.lower() in IMAGE_EXTS:
            pages.append(name)
    if sum(info.file_size for info in entries) > MAX_EXPANDED_BYTES:
        raise ComicInfoError("Archive exceeds the total expanded size limit")
    return member, pages


def _read_root(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> ElementTree.Element:
    if member.file_size > MAX_COMICINFO_BYTES:
        raise ComicInfoError("ComicInfo.xml is too large")
    with archive.open(member) as stream:
        data = stream.read(MAX_COMICINFO_BYTES + 1)
    if len(data) > MAX_COMICINFO_BYTES or len(data) != member.file_size:
        raise ComicInfoError("ComicInfo.xml has an invalid size")
    try:
        root = ElementTree.fromstring(data)
    except LookupError as exc:
        raise ComicInfoError(f"Cannot read ComicInfo.xml: {exc}") from exc
    if root.tag != "ComicInfo":
        raise ComicInfoError("ComicInfo.xml has an unexpected root element")
    return root


def _chapter_labels_span(pages: list[Path]) -> bool:
    labels = set()
    for page in pages:
        for part in (*page.parts[:-1], page.stem):
            coverage = parse_filename(part)
            if coverage.explicit_chapter:
                if coverage.ambiguous or coverage.is_range:
                    return True
                labels.add(coverage.chapter_number)
                if len(labels) > 1:
                    return True
    return False


def number_spans_chapters(number: str | None) -> bool:
    return bool(_NUMBER_SPAN.fullmatch(number or ""))


def parse_comicinfo_int(value: str | None) -> int | None:
    """Parse an XML Schema xs:int, allowing signed and zero-padded integers."""
    value = (value or "").strip(_XML_WHITESPACE)
    if not _XML_INTEGER.fullmatch(value):
        return None
    digits = value.lstrip("+-").lstrip("0")
    if len(digits) > 10:
        return None
    number = int(digits or "0")
    if value.startswith("-"):
        number = -number
    return number if -COMICINFO_INT_MAX - 1 <= number <= COMICINFO_INT_MAX else None


def _metadata(
    root: ElementTree.Element | None, pages: list[Path], revision: str
) -> ComicInfoMetadata:
    def text(tag: str, *, integer: bool = False) -> str | None:
        if root is None:
            return None
        value = root.findtext(tag) or None
        if value is None:
            return None
        if integer:
            return value.strip(_XML_WHITESPACE)
        return " ".join(value.split()) or None

    return ComicInfoMetadata(
        root is not None, text("Series"), text("Number"), text("Volume", integer=True),
        text("Title"), text("PageCount", integer=True), len(pages), revision,
        number_spans_chapters(text("Number")) or _chapter_labels_span(pages),
    )


def _require_revision(path: Path, expected: str) -> None:
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or _revision(current) != expected:
        raise ArchiveChanged("The chapter archive changed; reopen it and review the metadata")


def read_comicinfo(path: Path) -> ComicInfoMetadata:
    try:
        with _archive_reader(path) as handle, zipfile.ZipFile(handle) as archive:
            revision = _revision(os.fstat(handle.fileno()))
            member, pages = _members(archive)
            root = _read_root(archive, member) if member is not None else None
            _require_revision(path, revision)
        return _metadata(root, pages, revision)
    except ComicInfoError:
        raise
    except _ARCHIVE_ERRORS as exc:
        raise ComicInfoError(f"Cannot read ComicInfo.xml: {exc}") from exc


def _update_field(root: ElementTree.Element, tag: str, raw: str | int | None) -> None:
    value = " ".join(str(raw).split()) if raw is not None else ""
    matches = root.findall(tag)
    if matches or not value:
        if matches and value:
            matches[0].text = value
            matches = matches[1:]
        for duplicate in matches:
            root.remove(duplicate)
        return

    element = ElementTree.Element(tag)
    element.text = value
    rank = _TAG_ORDER[tag]
    for index, child in enumerate(root):
        if _TAG_ORDER.get(child.tag, -1) > rank:
            root.insert(index, element)
            return
    root.append(element)


def update_comicinfo(
    path: Path, updates: dict[str, str | int | None], *, expected_revision: str | None = None
) -> ComicInfoMetadata:
    """Atomically update selected fields and retain the member's original path."""
    unknown = set(updates) - set(_TAGS)
    if unknown:
        raise ComicInfoError(f"Unsupported ComicInfo field: {sorted(unknown)[0]}")
    try:
        with _archive_reader(path) as original, zipfile.ZipFile(original) as source:
            source_fd = original.fileno()
            source_stat = os.fstat(source_fd)
            revision = _revision(source_stat)
            if expected_revision is not None and revision != expected_revision:
                raise ArchiveChanged("The chapter archive changed; reopen it and review the metadata")
            member, pages = _members(source)
            root = _read_root(source, member) if member is not None else ElementTree.Element("ComicInfo")
            for field, raw in updates.items():
                _update_field(root, _TAGS[field], raw)

            payload = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            if len(payload) > min(MAX_COMICINFO_BYTES, MAX_MEMBER_BYTES):
                raise ComicInfoError("Updated ComicInfo.xml is too large")
            validated = ElementTree.fromstring(payload)
            expanded = sum(info.file_size for info in source.infolist() if info is not member)
            if expanded + len(payload) > MAX_EXPANDED_BYTES:
                raise ComicInfoError("Updated archive exceeds the total expanded size limit")
            if member is None and len(source.infolist()) == MAX_ZIP_ENTRIES:
                raise ComicInfoError("Updated archive exceeds the ZIP entry count limit")

            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp_path = Path(handle.name)
            try:
                with zipfile.ZipFile(temp_path, "w") as target:
                    target.comment = source.comment
                    for info in source.infolist():
                        if info is member:
                            target.writestr(info, payload)
                        elif info.is_dir():
                            target.writestr(info, b"")
                        else:
                            with source.open(info) as incoming, target.open(info, "w") as outgoing:
                                remaining = info.file_size
                                while chunk := incoming.read(COPY_CHUNK_BYTES):
                                    remaining -= len(chunk)
                                    if remaining < 0:
                                        raise ComicInfoError("ZIP member exceeds its declared size")
                                    outgoing.write(chunk)
                                if remaining:
                                    raise ComicInfoError("ZIP member is shorter than its declared size")
                    if member is None:
                        target.writestr("ComicInfo.xml", payload, compress_type=zipfile.ZIP_DEFLATED)
                # Adding a metadata entry can push a ZIP directory past the
                # reader's limit even when its expanded content still fits.
                with _archive_reader(temp_path, check_suffix=False) as completed, zipfile.ZipFile(completed):
                    fd = completed.fileno()
                    temp_stat = os.fstat(fd)
                    if (temp_stat.st_uid, temp_stat.st_gid) != (source_stat.st_uid, source_stat.st_gid):
                        os.fchown(fd, source_stat.st_uid, source_stat.st_gid)
                    # chown can clear mode bits, so restore the mode afterward.
                    os.fchmod(fd, stat.S_IMODE(source_stat.st_mode))
                    _copy_posix_access_acl(source_fd, fd)
                # Renaming changes the inode, so a second writer holding the old
                # revision can never replace this result after its copy finishes.
                result = _metadata(validated, pages, _revision(temp_path.stat()))
                if os.fstat(source_fd).st_ctime_ns != source_stat.st_ctime_ns:
                    raise ArchiveChanged("The chapter archive changed; reopen it and review the metadata")
                _require_revision(path, revision)
                os.replace(temp_path, path)
                return result
            finally:
                temp_path.unlink(missing_ok=True)
    except ComicInfoError:
        raise
    except _ARCHIVE_ERRORS as exc:
        raise ComicInfoError(f"Cannot update ComicInfo.xml: {exc}") from exc
