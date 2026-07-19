"""Inspect torrent metadata and choose the release with the best coverage."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Chapter, Series
from .sources.base import TorrentIndexer, TorrentRelease
from .titles import split_alt_titles, title_queries
from .util import (
    has_chapter_marker,
    is_special_chapter,
    normalize_title,
    parse_chapter_number,
    parse_volume_number,
)

INSPECTION_LIMIT = 20
MAX_BENCODE_DEPTH = 100

_CHAPTER_RANGE = re.compile(
    r"(?<![a-z])c(?:h(?:apter)?)?[ ._-]{0,2}(\d+(?:\.\d+)?)\s*[-–—]\s*"
    r"(?:c(?:h(?:apter)?)?[ ._-]{0,2})?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_VOLUME_RANGE = re.compile(
    r"(?<![a-z])v(?:ol(?:ume)?)?[ ._-]{0,2}(\d+)\s*[-–—]\s*"
    r"(?:v(?:ol(?:ume)?)?[ ._-]{0,2})?(\d+)",
    re.IGNORECASE,
)
_ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z", ".pdf"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
_CANONICAL_INTEGER = re.compile(rb"(?:0|-[1-9]\d*|[1-9]\d*)\Z")
_CANONICAL_LENGTH = re.compile(rb"(?:0|[1-9]\d*)\Z")


class BencodeError(ValueError):
    pass


def bdecode(data: bytes):
    """Minimal, strict bencode decoder sufficient for torrent file lists."""

    def parse(offset: int, depth: int = 0):
        if depth > MAX_BENCODE_DEPTH:
            raise BencodeError("bencode nesting exceeds safety limit")
        if offset >= len(data):
            raise BencodeError("unexpected end of bencode data")
        token = data[offset : offset + 1]
        if token == b"i":
            end = data.find(b"e", offset + 1)
            if end < 0:
                raise BencodeError("unterminated integer")
            raw = data[offset + 1 : end]
            if not _CANONICAL_INTEGER.fullmatch(raw):
                raise BencodeError("non-canonical integer")
            return int(raw), end + 1
        if token in (b"l", b"d"):
            collection = [] if token == b"l" else {}
            cursor = offset + 1
            previous_key: bytes | None = None
            while cursor < len(data) and data[cursor : cursor + 1] != b"e":
                key, cursor = parse(cursor, depth + 1)
                if token == b"l":
                    collection.append(key)
                    continue
                if not isinstance(key, bytes):
                    raise BencodeError("dictionary key is not bytes")
                if previous_key is not None and key <= previous_key:
                    raise BencodeError("dictionary keys are not strictly sorted")
                previous_key = key
                value, cursor = parse(cursor, depth + 1)
                collection[key] = value
            if cursor >= len(data):
                raise BencodeError("unterminated collection")
            return collection, cursor + 1
        colon = data.find(b":", offset)
        if colon < 0:
            raise BencodeError("invalid byte string")
        raw_length = data[offset:colon]
        if not _CANONICAL_LENGTH.fullmatch(raw_length):
            raise BencodeError("non-canonical byte string length")
        length = int(raw_length)
        start, end = colon + 1, colon + 1 + length
        if end > len(data):
            raise BencodeError("byte string exceeds input")
        return data[start:end], end

    value, end = parse(0)
    if end != len(data):
        raise BencodeError("trailing bencode data")
    return value


def torrent_paths(metadata: bytes) -> list[str]:
    decoded = bdecode(metadata)
    if not isinstance(decoded, dict) or not isinstance(decoded.get(b"info"), dict):
        raise BencodeError("torrent has no info dictionary")
    info = decoded[b"info"]

    def text(value) -> str:
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""

    files = info.get(b"files")
    if isinstance(files, list):
        paths = []
        for item in files:
            components = item.get(b"path") if isinstance(item, dict) else None
            if isinstance(components, list):
                value = "/".join(filter(None, (text(part) for part in components)))
                if value:
                    paths.append(value)
        return paths
    name = text(info.get(b"name"))
    return [name] if name else []


def _numbers_in_range(low: float, high: float, known: set[float]) -> set[float]:
    if high < low:
        low, high = high, low
    return {number for number in known if low <= number <= high}


def coverage_from_text(
    text: str,
    chapters: list[Chapter],
    *,
    allow_bare_chapter: bool = True,
) -> set[float]:
    known = {chapter.number for chapter in chapters}
    by_volume: dict[int, set[float]] = {}
    for chapter in chapters:
        if chapter.volume is not None:
            by_volume.setdefault(chapter.volume, set()).add(chapter.number)
    covered: set[float] = set()
    for match in _CHAPTER_RANGE.finditer(text):
        covered.update(_numbers_in_range(float(match.group(1)), float(match.group(2)), known))
    for match in _VOLUME_RANGE.finditer(text):
        low, high = sorted((int(match.group(1)), int(match.group(2))))
        for volume in range(low, high + 1):
            covered.update(by_volume.get(volume, set()))
    chapter = parse_chapter_number(text)
    volume = parse_volume_number(text)
    explicit_chapter = has_chapter_marker(text)
    if chapter in known and (
        explicit_chapter or (allow_bare_chapter and volume is None)
    ):
        covered.add(float(chapter))
    elif volume is not None:
        covered.update(by_volume.get(volume, set()))
    return covered


def torrent_coverage(
    metadata: bytes,
    release_title: str,
    chapters: list[Chapter],
    series: Series | None = None,
) -> set[float]:
    """Chapter numbers represented by files, falling back to the release title."""
    paths = torrent_paths(metadata)
    covered: set[float] = set()
    image_parents: set[str] = set()
    for raw in paths:
        item = PurePosixPath(raw)
        suffix = item.suffix.lower()
        if suffix in _ARCHIVE_EXTS:
            covered.update(
                coverage_from_text(
                    item.stem,
                    chapters,
                    allow_bare_chapter=(
                        series is not None and release_matches_series(item.stem, series)
                    ),
                )
            )
        elif suffix in _IMAGE_EXTS:
            image_parents.add(str(item.parent))
    for parent in image_parents:
        name = PurePosixPath(parent).name
        covered.update(
            coverage_from_text(
                name,
                chapters,
                allow_bare_chapter=(
                    series is not None and release_matches_series(name, series)
                ),
            )
        )
    # A broad title such as "c001-c100" may omit decimal extras in the actual
    # file list. Once parseable files are available, treat them as authoritative.
    return covered or coverage_from_text(release_title, chapters)


def release_matches_series(release_title: str, series: Series) -> bool:
    release = normalize_title(release_title)
    if not release:
        return False
    aliases = title_queries(series.title, split_alt_titles(series.alt_titles))
    normalized = [normalize_title(alias) for alias in aliases]
    for alias in normalized:
        compact_length = len(alias.replace(" ", ""))
        if compact_length < 3:
            continue
        if compact_length == 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", release):
                return True
        elif alias in release:
            return True
    return False


@dataclass
class TorrentSelection:
    release: TorrentRelease
    coverage: set[float]


async def select_best_torrent(
    series: Series,
    chapters: list[Chapter],
    indexers: list[TorrentIndexer],
    *,
    max_size_bytes: int,
    min_seeders: int,
) -> TorrentSelection | None:
    # This selector is used by the explicit add-time "Search now" flow, which
    # intentionally searches even when the new series itself is unmonitored.
    wanted = {c.number for c in chapters if not c.downloaded}
    if not wanted or not indexers:
        return None
    queries = title_queries(series.title, split_alt_titles(series.alt_titles))
    candidates: list[tuple[TorrentIndexer, TorrentRelease]] = []
    seen: set[str] = set()
    for indexer in indexers:
        for query in queries:
            try:
                releases = await indexer.search(query)
            except Exception:
                continue
            for release in releases:
                key = release.magnet
                if (
                    not key
                    or key in seen
                    or release.seeders < min_seeders
                    or release.size_bytes <= 0
                    or release.size_bytes > max_size_bytes
                    or not release_matches_series(release.title, series)
                ):
                    continue
                seen.add(key)
                candidates.append((indexer, release))
            if len(candidates) >= INSPECTION_LIMIT:
                # A productive title query already supplied enough
                # releases to inspect; avoid spending rate-limit slots on up
                # to five redundant alternate-title searches.
                break
    # Seeder order puts viable metadata first and bounds work against broad
    # title queries without making popularity outrank actual file coverage.
    candidates.sort(key=lambda item: item[1].seeders, reverse=True)
    candidates = candidates[:INSPECTION_LIMIT]

    async def inspect(item: tuple[TorrentIndexer, TorrentRelease]):
        indexer, release = item
        try:
            metadata = await indexer.get_torrent_metadata(release)
            coverage = (
                torrent_coverage(metadata, release.title, chapters, series)
                if metadata
                else coverage_from_text(release.title, chapters)
            )
        except Exception:
            coverage = coverage_from_text(release.title, chapters)
        return TorrentSelection(release=release, coverage=coverage & wanted)

    inspected = await asyncio.gather(*(inspect(item) for item in candidates))
    useful = [selection for selection in inspected if selection.coverage]
    if not useful:
        return None
    return max(
        useful,
        key=lambda selection: (
            len(selection.coverage),
            sum(is_special_chapter(number) for number in selection.coverage),
            selection.release.seeders,
            -selection.release.size_bytes,
        ),
    )
