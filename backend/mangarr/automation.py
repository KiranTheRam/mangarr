"""Per-series automation rules: which chapters are monitored, which sources
and scanlation groups grabs prefer, when a file is worth upgrading, which
volumes can be merged into one archive, and when a finished series is done.

Everything here is pure decision logic over model objects; the monitor loop
(mangarr.jobs.tasks) does the I/O.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import Chapter, Series, SeriesStatus
from .sources.base import SourceChapter

# all          every chapter
# missing      chapters without a file when the mode is applied, plus new ones
#              (existing files are left alone, so they are never upgraded)
# future       only chapters newer than the latest one known when applied
# from_chapter chapters numbered monitor_from and up
# latest_volume the latest volume onward, including uncollected chapters
# none         nothing (the series still refreshes, so new chapters show up)
MONITOR_MODES = ("all", "missing", "future", "from_chapter", "latest_volume", "none")
_THRESHOLD_MODES = {"future", "from_chapter", "latest_volume"}
# modes whose threshold is derived from the chapter list rather than given
RESOLVED_MODES = {"future", "latest_volume"}

FINISHED_STATUSES = (SeriesStatus.FINISHED, SeriesStatus.CANCELLED)

_LIST_SPLIT = re.compile(r"[\n,]")


def split_names(value: str) -> list[str]:
    """Names stored as a comma- or newline-separated string, deduplicated in
    order. Source lists use commas; group lists use newlines because group
    names can contain commas — both are read with this."""
    out: list[str] = []
    for part in _LIST_SPLIT.split(value or ""):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def split_lines(value: str) -> list[str]:
    out: list[str] = []
    for part in (value or "").split("\n"):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


# ------------------------------------------------------------ monitoring

def chapter_is_wanted(series: Series, number: float, downloaded: bool = False) -> bool:
    """Whether a chapter should be monitored under the series' mode."""
    if not series.monitored:
        return False
    mode = series.monitor_mode or "all"
    if mode == "none":
        return False
    if mode == "missing":
        return not downloaded
    if mode in _THRESHOLD_MODES:
        threshold = series.monitor_from
        if threshold is None:
            # unresolved: stay conservative until the chapter list is known
            return False
        return number > threshold if mode == "future" else number >= threshold
    return True


def resolve_monitor_from(series: Series, chapters: Iterable[Chapter]) -> float | None:
    """The threshold a derived mode means for the current chapter list, or
    None while there are no chapters to derive it from."""
    active = [c for c in chapters if not c.excluded]
    if not active:
        return None
    if series.monitor_mode == "future":
        return max(c.number for c in active)
    if series.monitor_mode == "latest_volume":
        volumes = [c.volume for c in active if c.volume is not None]
        if not volumes:
            # no volume data: the newest chapter is the best "latest" we know
            return max(c.number for c in active)
        latest = max(volumes)
        return min(c.number for c in active if c.volume == latest)
    return series.monitor_from


def apply_monitor_mode(series: Series, chapters: Iterable[Chapter]) -> int:
    """Rewrite every chapter's monitored flag from the series' mode (explicit
    per-chapter toggles are replaced — applying a mode is a reset). Resolves
    a derived threshold first. Returns the number of flags changed."""
    chapters = list(chapters)
    if series.monitor_mode in RESOLVED_MODES:
        series.monitor_from = resolve_monitor_from(series, chapters)
    changed = 0
    for chapter in chapters:
        if chapter.excluded:
            continue
        wanted = chapter_is_wanted(series, chapter.number, chapter.downloaded)
        if chapter.monitored != wanted:
            chapter.monitored = wanted
            changed += 1
    return changed


def needs_threshold_resolution(series: Series) -> bool:
    return series.monitor_mode in RESOLVED_MODES and series.monitor_from is None


# ---------------------------------------------------------- source order

def order_sources(names: Sequence[str], series: Series) -> list[str]:
    """Apply the series' source override to a globally ordered name list:
    listed sources first (in the series' order), the rest in global order,
    blocked sources removed."""
    override = {name: i for i, name in enumerate(split_names(series.source_priority))}
    blocked = set(split_names(series.blocked_sources))
    ordered = sorted(names, key=lambda name: override.get(name, len(override)))
    return [name for name in ordered if name not in blocked]


# ---------------------------------------------------- scanlation groups

def _group_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _group_parts(group: str) -> list[str]:
    """A joint release ("Group A & Group B") matches either member."""
    parts = [_group_key(p) for p in re.split(r"\s+&\s+|\s*\|\s*", group or "")]
    return [p for p in parts if p]


def group_is_blocked(group: str, blocked: Iterable[str]) -> bool:
    blocked_keys = {_group_key(b) for b in blocked}
    return any(part in blocked_keys for part in _group_parts(group))


def select_group_variants(
    chapters: Sequence[SourceChapter],
    preferred: Sequence[str] = (),
    blocked: Sequence[str] = (),
) -> list[SourceChapter]:
    """One release per chapter number from a source listing that may carry
    several scanlation groups' copies. Sources list variants in their own
    preference order, so without preferences the first variant wins (the
    pre-group behaviour). Preferred groups win in the order given; blocked
    groups are dropped even when that leaves a chapter unavailable."""
    rank = {_group_key(name): i for i, name in enumerate(preferred)}
    blocked_keys = {_group_key(name) for name in blocked}
    best: dict[float, tuple[tuple[int, int], SourceChapter]] = {}
    for index, sc in enumerate(chapters):
        parts = _group_parts(sc.group)
        if any(part in blocked_keys for part in parts):
            continue
        group_rank = min((rank[p] for p in parts if p in rank), default=len(rank))
        key = (group_rank, index)
        current = best.get(sc.number)
        if current is None or key < current[0]:
            best[sc.number] = (key, sc)
    return sorted((sc for _, sc in best.values()), key=lambda sc: sc.number)


def select_for_series(chapters: Sequence[SourceChapter], series: Series) -> list[SourceChapter]:
    return select_group_variants(
        chapters, split_lines(series.preferred_groups), split_lines(series.blocked_groups)
    )


# --------------------------------------------------------------- upgrades

def shared_file_paths(chapters: Iterable[Chapter]) -> set[str]:
    """Files backing more than one chapter (volume archives)."""
    counts = Counter(c.file_path for c in chapters if c.downloaded and c.file_path)
    return {path for path, count in counts.items() if count > 1}


def upgrade_candidates(
    series: Series,
    chapters: Iterable[Chapter],
    source_order: Sequence[str],
    direct_source_names: Iterable[str],
    skip_ids: set[int] = frozenset(),
) -> dict[float, Chapter]:
    """Downloaded chapters whose file could be replaced by a better release.

    Only single-chapter files mangarr itself fetched from a direct source are
    considered — files adopted from disk, torrent imports and volume archives
    are the user's or shared, and are never replaced. A file qualifies when
    its source ranks below the cutoff (a blocked or disabled source ranks
    last) or when its scanlation group has since been blocked."""
    if not series.upgrades_enabled:
        return {}
    chapters = list(chapters)
    rank = {name: i for i, name in enumerate(source_order)}
    cutoff_rank = rank.get(series.upgrade_cutoff, 0)
    direct = set(direct_source_names)
    shared = shared_file_paths(chapters)
    blocked_groups = split_lines(series.blocked_groups)
    out: dict[float, Chapter] = {}
    for c in chapters:
        if (
            c.excluded or not c.monitored or not c.downloaded or not c.file_path
            or c.id in skip_ids or c.file_path in shared
            or c.file_source not in direct
        ):
            continue
        below_cutoff = rank.get(c.file_source, len(rank)) > cutoff_rank
        if below_cutoff or group_is_blocked(c.file_group, blocked_groups):
            out[c.number] = c
    return out


def is_upgrade(chapter: Chapter, source_name: str, source_order: Sequence[str],
               blocked_groups: Sequence[str]) -> bool:
    """Whether a (group-filtered) release from source_name improves on the
    chapter's current file. Any unblocked release beats a blocked group's."""
    if group_is_blocked(chapter.file_group, blocked_groups):
        return True
    rank = {name: i for i, name in enumerate(source_order)}
    return rank.get(source_name, len(rank)) < rank.get(chapter.file_source, len(rank))


# ---------------------------------------------------------- volume merges

MERGED_FILE_SOURCE = "volume-merge"


def mergeable_volumes(
    series: Series, chapters: Iterable[Chapter], busy_chapter_ids: set[int] = frozenset(),
) -> dict[int, list[Chapter]]:
    """Volumes whose chapters can be packed into a single archive.

    A volume qualifies when it is closed (a later volume has started, or the
    series has finished) and every chapter in it has its own .cbz that
    mangarr downloaded — disk-adopted files belong to the user and are never
    consolidated. Chapters still downloading hold their volume back."""
    active = [c for c in chapters if not c.excluded]
    by_volume: dict[int, list[Chapter]] = {}
    for c in active:
        if c.volume is not None:
            by_volume.setdefault(c.volume, []).append(c)
    if not by_volume:
        return {}
    latest = max(by_volume)
    finished = series.status in FINISHED_STATUSES
    shared = shared_file_paths(active)
    out: dict[int, list[Chapter]] = {}
    for volume, members in sorted(by_volume.items()):
        if volume == latest and not finished:
            continue  # the newest volume may still be growing
        if all(
            c.downloaded and c.file_path and c.id not in busy_chapter_ids
            and c.file_path not in shared
            and c.file_source and c.file_source != MERGED_FILE_SOURCE
            and Path(c.file_path).suffix.lower() == ".cbz"
            for c in members
        ):
            out[volume] = sorted(members, key=lambda c: c.number)
    return out


# --------------------------------------------------------- finished series

FINISHED_SERIES_MODES = ("keep", "slow", "unmonitor")


def finished_and_complete(series: Series, chapters: Iterable[Chapter]) -> bool:
    """A finished (or cancelled) series with every monitored chapter on disk."""
    if series.status not in FINISHED_STATUSES:
        return False
    active = [c for c in chapters if not c.excluded]
    if not any(c.downloaded for c in active):
        return False
    return all(c.downloaded for c in active if c.monitored)
