"""Tests for selecting trustworthy chapter→volume maps (mangarr.volumes)."""

from mangarr.volumes import (
    distribute_over_disk_volumes,
    sanitize_volume_map,
    select_volume_map,
)


class TestSelectVolumeMap:
    def test_most_complete_source_wins_verbatim(self):
        # Seihantai na Kimi to Boku: MangaDex aggregate knows vols 1-2
        # (ch 1-14), MangaUpdates has zero volume tags — apply MangaDex
        # as-is and leave everything else honestly unassigned
        mangadex = {float(n): 1 for n in range(1, 7)} | {float(n): 2 for n in range(7, 15)}
        result = select_volume_map([mangadex, {}])
        assert result == mangadex
        assert 15.0 not in result

    def test_richer_later_source_beats_sparse_earlier_one(self):
        sparse = {100.0: 11}
        rich = {float(c): (c - 1) // 9 + 1 for c in range(1, 100)}
        assert select_volume_map([sparse, rich]) == rich

    def test_earlier_source_wins_ties(self):
        a = {1.0: 1, 2.0: 1}
        b = {1.0: 5, 2.0: 5}
        assert select_volume_map([a, b]) == a

    def test_maps_are_never_merged(self):
        a = {1.0: 1, 2.0: 1, 3.0: 2}
        b = {4.0: 9, 5.0: 9}
        result = select_volume_map([a, b])
        assert result == a
        assert 4.0 not in result

    def test_candidates_are_sanitized_before_comparison(self):
        # a source whose entries mostly contradict each other offers less
        # usable data than its raw size suggests
        contradictory = {1.0: 5, 2.0: 1, 3.0: 4, 4.0: 2}  # best subset: 2
        consistent = {1.0: 1, 2.0: 1, 3.0: 2}
        assert select_volume_map([contradictory, consistent]) == consistent

    def test_empty(self):
        assert select_volume_map([]) == {}


class TestSanitizeVolumeMap:
    def test_drops_stray_low_volume_at_end(self):
        # a lone mislabeled chapter (e.g. Chainsaw Man ch 232 tagged vol 1)
        mapping = {float(c): (c - 1) // 9 + 1 for c in range(1, 100)}
        mapping[232.0] = 1
        cleaned = sanitize_volume_map(mapping)
        assert 232.0 not in cleaned
        assert len(cleaned) == 99

    def test_keeps_monotone_data_untouched(self):
        mapping = {1.0: 1, 5.0: 1, 6.0: 2, 14.0: 2, 15.0: 3}
        assert sanitize_volume_map(mapping) == mapping

    def test_drops_minority_not_majority(self):
        mapping = {1.0: 1, 2.0: 7, 3.0: 1, 4.0: 2, 5.0: 2}
        cleaned = sanitize_volume_map(mapping)
        assert 2.0 not in cleaned
        assert cleaned[4.0] == 2

    def test_empty(self):
        assert sanitize_volume_map({}) == {}


class TestDistributeOverDiskVolumes:
    def test_sparse_complete_set_splits_evenly(self):
        # Naruto-style: 698 chapters, one stray source anchor, finished
        # series with all 72 volume archives on disk
        mapping = {700.0: 72}
        chapters = [float(c) for c in range(1, 699)] + [700.0]
        result = distribute_over_disk_volumes(
            mapping, chapters, range(1, 73), complete=True
        )
        assert result[700.0] == 72  # anchor kept
        assert result[1.0] == 1
        assert result[698.0] == 72
        vols = [result[float(c)] for c in range(1, 699)]
        assert vols == sorted(vols)
        assert set(vols) == set(range(1, 73))

    def test_dense_map_only_extends_the_tail(self):
        # source knows vols 1-3 exactly; disk has vols 1-6; finished set
        mapping = {}
        for c in range(1, 10):
            mapping[float(c)] = (c - 1) // 3 + 1  # vols 1-3, 3 chs each
        chapters = [float(c) for c in range(1, 19)]
        result = distribute_over_disk_volumes(
            mapping, chapters, range(1, 7), complete=True
        )
        assert result[9.0] == 3  # source data untouched
        assert [result[float(c)] for c in range(10, 19)] == [4, 4, 4, 5, 5, 5, 6, 6, 6]

    def test_ongoing_fills_at_observed_rate_and_stops(self):
        # 3 known volumes of 3 chapters → rate 3; disk has vol 4 only, so
        # chapters 10-12 go there and 13+ stay honestly unassigned
        mapping = {float(c): (c - 1) // 3 + 1 for c in range(1, 10)}
        chapters = [float(c) for c in range(1, 30)]
        result = distribute_over_disk_volumes(
            mapping, chapters, [1, 2, 3, 4], complete=False
        )
        assert [result[float(c)] for c in (10, 11, 12)] == [4, 4, 4]
        assert 13.0 not in result

    def test_sparse_ongoing_uses_fallback_rate(self):
        mapping = {1.0: 1}
        chapters = [float(c) for c in range(1, 25)]
        result = distribute_over_disk_volumes(
            mapping, chapters, [1, 2], complete=False, fallback_rate=10.0
        )
        assert result[10.0] == 1
        assert result[11.0] == 2
        assert result[20.0] == 2
        assert 21.0 not in result  # past the owned volumes

    def test_no_disk_volumes_is_noop(self):
        mapping = {1.0: 1}
        assert distribute_over_disk_volumes(mapping, [1.0, 2.0], [], True) == mapping
