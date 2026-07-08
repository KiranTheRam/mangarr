"""Select a trustworthy chapter→volume map from source metadata.

Volume→chapter data (MangaDex /aggregate is the only structured source;
MangaUpdates contributes sparse per-release volume tags) is community-entered
and varies wildly in completeness per source. mangarr does not guess ranges:
each source's map is sanitized (a lone mislabeled chapter — e.g. one
scanlation tagging chapter 232 as volume 1 — breaks volume monotonicity in
chapter order and is dropped), and then the single most complete source's
assignments are applied verbatim. Explicit chapter numbers from that map may
fill gaps between chapters seen from chapter/release sources; chapters no
source can name stay unassigned rather than being distributed by heuristics.

The one exception is the user's own files: volume archives already on disk
prove those volumes exist, and distribute_over_disk_volumes() uses their
numbering to place chapters when adopting an existing library — without it,
owned chapters couldn't be matched to the archives that contain them and
would be re-downloaded.
"""

import bisect
from collections.abc import Iterable


def select_volume_map(maps: Iterable[dict[float, int]]) -> dict[float, int]:
    """The most complete source's assignments, sanitized. Sources are given
    in priority order; on equal coverage the earlier one wins. Maps are never
    merged — mixing two sources' volume boundaries produces garbage neither
    of them claims."""
    best: dict[float, int] = {}
    for m in maps:
        cleaned = sanitize_volume_map(m)
        if len(cleaned) > len(best):
            best = cleaned
    return best


def sanitize_volume_map(mapping: dict[float, int]) -> dict[float, int]:
    """Drop assignments that break volume monotonicity in chapter order,
    keeping the largest consistent subset."""
    items = sorted(mapping.items())
    keep = _longest_non_decreasing([v for _, v in items])
    return {items[i][0]: items[i][1] for i in keep}


def _longest_non_decreasing(values: list[int]) -> set[int]:
    """Indices of one longest non-decreasing subsequence (O(n log n))."""
    tail_values: list[int] = []  # smallest tail value per subsequence length
    tail_index: list[int] = []
    prev = [-1] * len(values)
    for i, v in enumerate(values):
        j = bisect.bisect_right(tail_values, v)
        if j == len(tail_values):
            tail_values.append(v)
            tail_index.append(i)
        else:
            tail_values[j] = v
            tail_index[j] = i
        prev[i] = tail_index[j - 1] if j > 0 else -1
    keep: set[int] = set()
    i = tail_index[-1] if tail_index else -1
    while i != -1:
        keep.add(i)
        i = prev[i]
    return keep


def distribute_over_disk_volumes(
    mapping: dict[float, int],
    chapter_numbers: Iterable[float],
    disk_volumes: Iterable[int],
    complete: bool,
    fallback_rate: float | None = None,
) -> dict[float, int]:
    """Assign the chapters the source couldn't place using the volume
    archives that exist on disk (their numbering tells us which volumes
    exist even when no metadata source knows their contents).

    When the source knows at least a few volumes, its map is trusted as
    scaffolding and only the chapters after its last anchor are spread
    across the on-disk volumes that follow it. When it knows fewer (one
    stray anchor tells us nothing about where volumes begin), everything
    unassigned is distributed across the whole disk set, keeping the few
    known anchors.

    When `complete` (a finished series whose disk set reaches the final
    volume) every distributed chapter belongs to some disk volume, so the
    span splits evenly. Otherwise volumes fill at the rate observed in the
    mapped region (else `fallback_rate`, else 9 — the prevailing tankobon
    size), and chapters past the last disk volume stay unassigned rather
    than guessed into volumes that may not exist.
    """
    result = dict(mapping)
    sparse = len(set(mapping.values())) < 3
    if sparse:
        # positions count every chapter (anchored ones occupy their slot in
        # a volume too), assignments only fill the unanchored
        ordered = sorted(set(chapter_numbers) | set(mapping))
        positioned = list(enumerate(ordered))
        volumes = sorted(set(disk_volumes))
    else:
        last_ch = max(mapping)
        positioned = list(enumerate(sorted(
            n for n in set(chapter_numbers) if n not in mapping and n > last_ch
        )))
        volumes = sorted(v for v in set(disk_volumes) if v > max(mapping.values()))
    if not positioned or not volumes:
        return result
    if complete:
        per = len(positioned) / len(volumes)
    else:
        per = _typical_volume_size(mapping) or fallback_rate or 9.0
    for position, number in positioned:
        if number in mapping:
            continue
        index = int(position / per)
        if index >= len(volumes):
            if complete:
                index = len(volumes) - 1
            else:
                continue  # past the owned volumes — honestly unknown
        result[number] = volumes[index]
    return result


def _typical_volume_size(mapping: dict[float, int]) -> float | None:
    """Median chapters-per-volume over the mapped region, when it has
    enough volumes to be representative."""
    counts: dict[int, int] = {}
    for vol in mapping.values():
        counts[vol] = counts.get(vol, 0) + 1
    if len(counts) < 3:
        return None
    sizes = sorted(counts.values())
    return float(sizes[len(sizes) // 2])
