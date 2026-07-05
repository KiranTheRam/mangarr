"""Tests for building trustworthy chapter→volume maps (mangarr.volumes)."""

from mangarr.volumes import (
    build_volume_map,
    interpolate_volume_gaps,
    merge_volume_maps,
    sanitize_volume_map,
)


class TestMergeVolumeMaps:
    def test_earlier_source_wins(self):
        merged = merge_volume_maps([{1.0: 1, 2.0: 1}, {2.0: 9, 3.0: 2}])
        assert merged == {1.0: 1, 2.0: 1, 3.0: 2}

    def test_empty(self):
        assert merge_volume_maps([]) == {}


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


class TestInterpolateVolumeGaps:
    def test_fills_single_missing_volume(self):
        # vol 11 known through ch 90, vol 13 known only from ch 107 —
        # chapters 91..106 split across vols 12 and 13
        mapping = {90.0: 11, 107.0: 13}
        chapters = [float(c) for c in range(90, 108)]
        result = interpolate_volume_gaps(mapping, chapters)
        assert result[91.0] == 12
        assert result[98.0] == 12
        assert result[99.0] == 13
        assert result[106.0] == 13

    def test_same_volume_on_both_sides(self):
        mapping = {176.0: 20, 203.0: 20}
        result = interpolate_volume_gaps(mapping, [176.0, 190.0, 203.0])
        assert result[190.0] == 20

    def test_leading_and_trailing_stay_unassigned(self):
        mapping = {5.0: 1, 10.0: 2}
        result = interpolate_volume_gaps(mapping, [0.5, 5.0, 10.0, 11.0, 200.0])
        assert 0.5 not in result  # cover special before any anchor
        assert 11.0 not in result  # ongoing tail not collected yet
        assert 200.0 not in result

    def test_even_split_across_many_volumes(self):
        # 18 unknown chapters between vol 1 (ends ch 9) and a well-entered
        # vol 4 (starts ch 28) → 9 each to vols 2 and 3, none stolen by 4
        mapping = {9.0: 1, 28.0: 4, 29.0: 4, 30.0: 4}
        chapters = [float(c) for c in range(9, 31)]
        result = interpolate_volume_gaps(mapping, chapters)
        assert [result[float(c)] for c in range(10, 28)] == [2] * 9 + [3] * 9

    def test_sparse_next_volume_shares_the_gap(self):
        # vol 13 known only from one special — its front is missing, so the
        # gap splits across vols 12 AND 13
        mapping = {90.0: 11, 89.0: 11, 88.0: 11, 107.0: 13}
        chapters = [float(c) for c in range(88, 108)]
        result = interpolate_volume_gaps(mapping, chapters)
        assert result[91.0] == 12
        assert result[106.0] == 13

    def test_straggler_between_fully_known_volumes(self):
        # a decimal extra between two complete volumes trails the earlier one
        mapping = {29.0: 3, 30.0: 3, 30.5: 3, 31.0: 4, 32.0: 4, 33.0: 4}
        result = interpolate_volume_gaps(mapping, [30.7])
        assert result[30.7] == 3

    def test_no_anchors_is_noop(self):
        assert interpolate_volume_gaps({}, [1.0, 2.0]) == {}


class TestBuildVolumeMap:
    def test_end_to_end(self):
        # source data: vols 1-2 complete, vol 5 known from one special,
        # plus one stray; tracked chapters run past the last anchor
        source = {float(c): 1 for c in range(1, 10)}
        source.update({float(c): 2 for c in range(10, 19)})
        source[40.5] = 5
        source[3.0] = 9  # stray
        tracked = [float(c) for c in range(1, 50)]
        result = build_volume_map([source], tracked)
        assert result[3.0] == 1  # stray dropped, gap-filled consistently
        assert result[18.0] == 2
        # chapters 19..40 spread across vols 3-5
        assert result[19.0] == 3
        assert result[40.0] == 5
        vols = [result[float(c)] for c in range(19, 41)]
        assert vols == sorted(vols)
        assert set(vols) == {3, 4, 5}
        assert 45.0 not in result  # beyond the last anchor

    def test_no_data(self):
        assert build_volume_map([], [1.0, 2.0]) == {}
