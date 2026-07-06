"""Build a trustworthy chapter→volume map from source metadata.

Volume→chapter data (MangaDex /aggregate is the only structured source) is
community-entered and has two failure modes this module corrects:

1. **Strays** — a lone mislabeled chapter (e.g. one scanlation tagging
   chapter 232 as volume 1). Volume numbers must be non-decreasing in
   chapter order, so we keep the largest internally-consistent subset
   (longest non-decreasing subsequence) and drop the rest.

2. **Gaps** — recent volumes simply never entered (e.g. volumes 12–17
   missing while 18+ exist from a stray special). Chapters between two
   known anchors can only belong to the volumes between them, so we
   distribute them evenly across that range. Volume sizes within one
   series are near-constant in practice, which makes this accurate to
   about ±1 chapter at each inferred boundary — and any assignment the
   source later provides replaces the inference on the next resync.

Chapters after the last anchor stay unassigned: for an ongoing series they
genuinely aren't collected in a volume yet, and guessing volumes that may
not exist would be worse than none. Chapters before the first anchor
(cover specials, oneshots) also stay unassigned.
"""

import bisect
from collections.abc import Iterable


def merge_volume_maps(maps: Iterable[dict[float, int]]) -> dict[float, int]:
    """Union per-source maps; earlier maps (higher-priority sources) win."""
    merged: dict[float, int] = {}
    for m in maps:
        for number, volume in m.items():
            merged.setdefault(number, volume)
    return merged


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


def interpolate_volume_gaps(
    mapping: dict[float, int], chapter_numbers: Iterable[float]
) -> dict[float, int]:
    """Assign unmapped chapters that lie between two known anchors.

    A gap between the last known chapter of volume A and the first known
    chapter of volume B belongs to volumes A+1..B-1, extended to include B
    itself when B's own data is sparse (a volume known only from a stray
    special has its front missing too; a volume with several entered
    chapters starts where its data says it starts). A's span is complete —
    aggregate data is entered per released chapter, so it's whole volumes
    that go missing. The gap is split evenly across the range.

    Chapters before the first anchor get the same treatment against a
    virtual volume-0 boundary — every series starts at volume 1, so the
    leading run spans volumes 1..first-anchored-volume.
    """
    if not mapping:
        return dict(mapping)
    result = dict(mapping)
    anchor_count: dict[int, int] = {}
    for vol in mapping.values():
        anchor_count[vol] = anchor_count.get(vol, 0) + 1
    gap: list[float] = []  # unmapped chapters since the previous anchor
    numbers = sorted(set(chapter_numbers) | set(mapping))
    prev_vol = 0  # virtual boundary: the series starts at volume 1
    for number in numbers:
        if number not in mapping:
            gap.append(number)
            continue
        vol = mapping[number]
        if gap and vol > prev_vol:
            start = prev_vol + 1
            end = vol if anchor_count[vol] < 3 else vol - 1
            if end < start:
                # adjacent fully-known volumes — stragglers (decimal
                # extras between them) trail the earlier volume; at the
                # very front they lead the first volume instead
                for n in gap:
                    result[n] = max(prev_vol, 1)
            else:
                span = end - start + 1
                per = len(gap) / span
                for i, n in enumerate(gap):
                    result[n] = start + min(int(i / per), span - 1)
        elif gap:
            # same volume on both sides — the middle is that volume too
            for n in gap:
                result[n] = vol
        gap = []
        prev_vol = vol
    return result


def build_volume_map(
    maps: Iterable[dict[float, int]], chapter_numbers: Iterable[float]
) -> dict[float, int]:
    """Merge, sanitize, and gap-fill source volume maps into one
    chapter→volume assignment covering every chapter it can justify."""
    merged = sanitize_volume_map(merge_volume_maps(maps))
    if not merged:
        return {}
    return interpolate_volume_gaps(merged, chapter_numbers)


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
