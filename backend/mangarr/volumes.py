"""Select a trustworthy chapter→volume map from source metadata.

Volume→chapter data varies wildly in authority and completeness. Every map is
sanitized (a lone mislabeled chapter — e.g. one scanlation tagging chapter 232
as volume 1 — breaks monotonicity and is dropped), then official and printed
table-of-contents sources rank above community aggregates. Lower-ranked data
extends the primary row by row: a row is merged only when the result doesn't
already cover that chapter and the volume fits monotonically between the
accepted neighbours, and every merged row remembers which source supplied it.

The one exception is the user's own files: volume archives already on disk
prove those volumes exist, and distribute_over_disk_volumes() uses their
numbering to place chapters when adopting an existing library — without it,
owned chapters couldn't be matched to the archives that contain them and
would be re-downloaded.
"""

import bisect
from collections.abc import Iterable

from .chapter_metadata import source_authority


def rank_labeled_volume_maps(
    maps: Iterable[tuple[str, dict[float, int]]],
) -> list[tuple[str, dict[float, int]]]:
    """Sanitize and rank maps by authority, then coverage.

    This prevents a large but sparse/interpolated community map from beating
    an official catalogue or a printed table of contents simply by having
    more rows.
    """
    cleaned = [(name, sanitize_volume_map(mapping)) for name, mapping in maps]
    return sorted(
        ((name, mapping) for name, mapping in cleaned if mapping),
        key=lambda item: (source_authority(item[0]), len(item[1])),
        reverse=True,
    )


def select_labeled_volume_map(
    maps: Iterable[tuple[str, dict[float, int]]],
) -> tuple[str, dict[float, int], dict[float, str]]:
    """Choose the most authoritative map and extend it with non-conflicting rows.

    Lower-ranked maps contribute, in rank order, only chapters the result
    doesn't already cover, and only where the volume fits monotonically
    between the accepted neighbours — a conflicting row loses to the
    higher-ranked value and an out-of-order row is dropped, but nothing
    already accepted is ever displaced.

    Returns (primary source name, mapping, per-chapter source labels).
    """
    return select_ranked_volume_maps(rank_labeled_volume_maps(maps))


def select_ranked_volume_maps(
    ranked: list[tuple[str, dict[float, int]]],
) -> tuple[str, dict[float, int], dict[float, str]]:
    """The merge behind select_labeled_volume_map, for callers that already
    ranked (and thereby sanitized) their maps."""
    if not ranked:
        return "", {}, {}
    primary, result = ranked[0][0], dict(ranked[0][1])
    sources = dict.fromkeys(result, primary)
    ordered = sorted(result)
    for name, candidate in ranked[1:]:
        for number, volume in sorted(candidate.items()):
            if number in result:
                continue
            i = bisect.bisect_left(ordered, number)
            if i > 0 and result[ordered[i - 1]] > volume:
                continue
            if i < len(ordered) and volume > result[ordered[i]]:
                continue
            result[number] = volume
            sources[number] = name
            ordered.insert(i, number)
    return primary, result, sources


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
    if complete and sparse and mapping:
        # Interpolate on both sides of sparse anchors instead of splitting the
        # whole series blindly and then overwriting the anchors. This keeps the
        # result monotonic and prevents overlapping volume ranges.
        available = sorted(set(volumes))
        anchors = [(i, mapping[n]) for i, n in positioned if n in mapping]
        for position, number in positioned:
            if number in mapping:
                continue
            left = max((a for a in anchors if a[0] < position), default=None)
            right = min((a for a in anchors if a[0] > position), default=None)
            lo_pos, lo_vol = left or (0, available[0])
            hi_pos, hi_vol = right or (len(positioned) - 1, available[-1])
            if hi_pos == lo_pos:
                estimate = lo_vol
            else:
                fraction = (position - lo_pos) / (hi_pos - lo_pos)
                estimate = round(lo_vol + (hi_vol - lo_vol) * fraction)
            result[number] = min(available, key=lambda v: abs(v - estimate))
        return sanitize_volume_map(result)
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
