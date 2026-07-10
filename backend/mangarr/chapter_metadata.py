"""Field-level chapter metadata selection and bonus-chapter reconciliation."""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable

from .models import Chapter
from .sources.base import ChapterMetadata
from .util import normalize_title


# Authority is intentionally field-oriented.  A download source may be useful
# for chapter existence while an official catalogue is authoritative for its
# title/volume.  Manual edits always win and are separately lockable.
#
# This table is deliberately fixed and independent of the user-configurable
# source_priority setting: that setting orders sources for chapter downloads
# and enablement, while metadata trust (official catalogue > printed table of
# contents > community aggregate) is not a download preference.  "disk-inferred"
# labels assignments the pipeline inferred (disk distribution, interpolation,
# decimal reconciliation) rather than any source claiming them — kept at the
# bottom so real data can always correct a guess.
SOURCE_AUTHORITY = {
    "manual": 1000,
    "comicinfo": 110,
    "viz": 100,
    "wikipedia": 90,
    "mangaplus": 85,
    "mangadex": 50,
    "tcbscans": 35,
    "asura": 30,
    "mangaupdates": 25,
    "weebcentral": 10,
    "disk-inferred": 5,
    "legacy": 0,
    "": 0,
}

_LEADING_NUMBER = re.compile(
    r"^\s*(?:#|chapter\s*|ch\.?\s*|class\s*)(\d+(?:\.\d+)?)\s*[:.\-–—]?\s*",
    re.IGNORECASE,
)
_GENERIC_NUMBER = re.compile(
    r"^(?:#|chapter\s*|ch\.?\s*|class\s*)?\d+(?:\.\d+)?$", re.IGNORECASE
)


def source_authority(source: str | None) -> int:
    return SOURCE_AUTHORITY.get(source or "", 20)


def clean_title(title: str, number: float | None = None) -> str:
    """Normalize display noise without changing the actual chapter name.

    A leading chapter marker is stripped only when its number is the
    chapter's own — "Chapter 12: Duel" for chapter 12 is noise, but
    "Class 1-A vs. the Villains" for chapter 293 is the real title.
    """
    value = " ".join((title or "").replace("\xa0", " ").split()).strip(' "“”')
    if number is not None:
        match = _LEADING_NUMBER.match(value)
        if match and float(match.group(1)) == number:
            stripped = value[match.end():]
            if stripped:
                value = stripped
    return value.strip(' "“”')


def is_generic_title(title: str, series_title: str, number: float) -> bool:
    value = clean_title(title, number)
    if not value:
        return True
    normalized = normalize_title(value)
    if _GENERIC_NUMBER.fullmatch(value):
        return True
    series = normalize_title(series_title)
    number_text = str(int(number)) if float(number).is_integer() else str(number)
    generic = {
        normalize_title(f"Chapter {number_text}"),
        normalize_title(f"{series_title} Chapter {number_text}"),
        normalize_title(f"{series_title} Ch {number_text}"),
    }
    return normalized in generic or bool(
        series and normalized == normalize_title(f"{series_title} {number_text}")
    )


def title_score(title: str, source: str, series_title: str, number: float) -> int:
    value = clean_title(title, number)
    if not value:
        return -1
    informative = 0 if is_generic_title(value, series_title, number) else 1000
    return informative + source_authority(source) + min(len(value), 80)


def apply_title(
    chapter: Chapter,
    candidate: str,
    source: str,
    series_title: str,
) -> bool:
    """Apply a better title unless the user locked the existing value."""
    if getattr(chapter, "title_locked", False):
        return False
    value = clean_title(candidate, chapter.number)
    if not value or is_generic_title(value, series_title, chapter.number):
        return False
    current_source = getattr(chapter, "title_source", "") or "legacy"
    if title_score(value, source, series_title, chapter.number) <= title_score(
        chapter.title, current_source, series_title, chapter.number
    ):
        return False
    chapter.title = value
    chapter.title_source = source
    return True


def apply_volume(chapter: Chapter, volume: int | None, source: str) -> bool:
    """Fill or improve a source-owned volume without touching manual locks.

    Normal refreshes do not replace legacy non-null values.  Destructive
    corrections remain behind the existing volume-resync preview/confirmation.
    """
    if volume is None or getattr(chapter, "volume_locked", False):
        return False
    current_source = getattr(chapter, "volume_source", "") or "legacy"
    if chapter.volume is not None and current_source in ("legacy", "manual", "comicinfo"):
        return False
    if chapter.volume is not None and source_authority(source) <= source_authority(current_source):
        return False
    if chapter.volume == volume:
        # same value from a stronger source — upgrade the provenance only
        chapter.volume_source = source
        return True
    chapter.volume = volume
    chapter.volume_source = source
    return True


def reconcile_decimal_volumes(
    mapping: dict[float, int], chapter_numbers: Iterable[float]
) -> dict[float, int]:
    """Place locally-numbered extras using established main-chapter bounds.

    Exact source assignments win.  Otherwise an ``x.y`` chapter inherits
    chapter ``x``'s volume.  If ``x`` is absent, both surrounding mapped
    chapters must agree.  This deliberately avoids guessing whole-number gaps.
    """
    result = dict(mapping)
    ordered = sorted(mapping)
    for raw in sorted(set(chapter_numbers)):
        number = float(raw)
        if number in result or number == math.floor(number):
            continue
        floor = float(math.floor(number))
        if floor in result:
            result[number] = result[floor]
            continue
        i = bisect.bisect_left(ordered, number)
        if 0 < i < len(ordered) and result[ordered[i - 1]] == result[ordered[i]]:
            result[number] = result[ordered[i - 1]]
    return result


def apply_metadata_rows(
    chapters: Iterable[Chapter], rows: Iterable[ChapterMetadata], series_title: str
) -> int:
    """Merge exact metadata and safely pair unnumbered printed extras.

    Unnumbered extras are paired only when a volume has exactly the same
    number of titleless decimal candidates and extra rows, and only among
    decimals inside that volume's own numbered chapter span — an unplaced
    decimal from elsewhere in the series must not inherit a printed
    identity; ambiguity stays unresolved rather than fabricated.
    """
    chapter_list = sorted(chapters, key=lambda c: c.number)
    by_number = {c.number: c for c in chapter_list}
    changed = 0
    extras_by_volume: dict[int, list[ChapterMetadata]] = {}
    numbers_by_volume: dict[int, list[float]] = {}
    for row in rows:
        if row.number is None:
            if row.volume is not None and row.title:
                extras_by_volume.setdefault(row.volume, []).append(row)
            continue
        if row.volume is not None:
            numbers_by_volume.setdefault(row.volume, []).append(float(row.number))
        chapter = by_number.get(float(row.number))
        if chapter is None:
            continue
        changed += int(apply_title(chapter, row.title, row.source_name, series_title))
        changed += int(apply_volume(chapter, row.volume, row.source_name))

    for volume, extras in extras_by_volume.items():
        numbered = numbers_by_volume.get(volume)
        if numbered:
            # an extra can trail the volume's last chapter (17.5 after 17);
            # an unplaced decimal from elsewhere in the series must not
            # inherit this volume's printed identity
            low, high = min(numbered), max(numbered) + 1
            candidates = [
                c for c in chapter_list
                if c.number != int(c.number)
                and low <= c.number <= high
                and not c.title
                and (c.volume == volume or c.volume is None)
            ]
        else:
            # no numbered rows to bound the span — only chapters already
            # assigned to this volume are safe to pair
            candidates = [
                c for c in chapter_list
                if c.number != int(c.number) and not c.title and c.volume == volume
            ]
        if len(candidates) != len(extras):
            continue
        for chapter, row in zip(candidates, extras):
            changed += int(apply_title(chapter, row.title, row.source_name, series_title))
            changed += int(apply_volume(chapter, volume, row.source_name))
    return changed
