import zipfile

from mangarr.library.cleanup import analyze, apply_cleanup
from mangarr.library.naming import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME
from mangarr.models import Chapter, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make(path, size=1):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG * size)


def plan(series, chapters, folder):
    return analyze(series, chapters, [folder], DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)


class TestAnalyze:
    def test_duplicate_volume_keeps_the_in_use_copy(self, tmp_path):
        # the Chained Soldier case: original "v01" (referenced) + torrent "Vol. 01"
        make(tmp_path / "Series v01.cbz")
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        chs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                       file_path=str(tmp_path / "Series v01.cbz")) for i in (1, 2, 3)]
        p = plan(series, chs, tmp_path)

        assert len(p.groups) == 1
        g = p.groups[0]
        assert g.label == "Volume 1"
        keep = [f for f in g.files if f.keep]
        assert len(keep) == 1
        # keep the file already in use; the unreferenced duplicate is removed
        assert keep[0].name == "Series v01.cbz" and keep[0].referenced

    def test_redundant_orphan_default_delete(self, tmp_path):
        # a stray volume file whose chapters are all downloaded elsewhere
        make(tmp_path / "Series - Vol. 05.cbz")
        make(tmp_path / "Series - Ch. 0050.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=50.0, volume=5, downloaded=True,
                     file_path=str(tmp_path / "Series - Ch. 0050.cbz"))
        p = plan(series, [ch], tmp_path)
        orphan = next(o for o in p.orphans if o.name == "Series - Vol. 05.cbz")
        assert orphan.keep is False  # redundant → default delete

    def test_unknown_extra_default_keep(self, tmp_path):
        make(tmp_path / "Bonus Artbook.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=1.0, volume=1, downloaded=True,
                     file_path=str(tmp_path / "x"))
        p = plan(series, [ch], tmp_path)
        art = next(o for o in p.orphans if o.name == "Bonus Artbook.cbz")
        assert art.keep is True  # unknown → keep by default


class TestApply:
    def test_deletes_and_repoints_to_survivor(self, tmp_path):
        make(tmp_path / "Series v01.cbz")
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        chs = [Chapter(id=i, series_id=1, number=float(i), volume=1, downloaded=True,
                       file_path=str(tmp_path / "Series v01.cbz")) for i in (1, 2, 3)]
        # delete the referenced original, keep the canonical → chapters re-point
        res = apply_cleanup(series, chs, [tmp_path], [str(tmp_path / "Series v01.cbz")])
        assert res.deleted == 1
        assert res.repointed == 3
        assert not (tmp_path / "Series v01.cbz").exists()
        assert all(c.file_path.endswith("Series - Vol. 01.cbz") for c in chs)

    def test_refuses_to_delete_last_copy(self, tmp_path):
        make(tmp_path / "Series - Vol. 01.cbz")
        series = Series(id=1, title="Series", folder_name="")
        ch = Chapter(id=1, series_id=1, number=1.0, volume=1, downloaded=True,
                     file_path=str(tmp_path / "Series - Vol. 01.cbz"))
        res = apply_cleanup(series, [ch], [tmp_path], [str(tmp_path / "Series - Vol. 01.cbz")])
        assert res.deleted == 0 and res.skipped == 1
        assert (tmp_path / "Series - Vol. 01.cbz").exists()  # not deleted

    def test_refuses_to_delete_path_outside_series_media(self, tmp_path):
        folder = tmp_path / "Series"
        outside = tmp_path / "outside.cbz"
        folder.mkdir()
        make(folder / "Series - Ch. 0001.cbz")
        make(outside)
        series = Series(id=1, title="Series", folder_name="Series")
        ch = Chapter(id=1, series_id=1, number=1.0, downloaded=True,
                     file_path=str(folder / "Series - Ch. 0001.cbz"))

        res = apply_cleanup(series, [ch], [folder], [str(outside)])

        assert res.deleted == 0 and res.skipped == 1
        assert outside.exists()


def make_labeled(path, *numbers):
    with zipfile.ZipFile(path, 'w') as archive:
        for number in numbers:
            archive.writestr(f'Series - c{number} (v01) - p001.jpg', PNG)


def default_deletions(p):
    return [f.path for g in p.groups for f in g.files if not f.keep] + [
        f.path for f in [*p.orphans, *p.overlaps] if not f.keep
    ]


def test_in_use_volume_overlaps_chapters_and_keeps_unique_bonus(tmp_path):
    volume = tmp_path / 'Series - Vol. 01.cbz'
    make(volume)
    chs = []
    for i, number in enumerate((1., 2., 2.5), 1):
        path = volume if number == 2.5 else tmp_path / f'Series - Ch. {number:04g}.cbz'
        if path != volume:
            make(path)
        chs.append(Chapter(id=i, series_id=1, number=number, volume=1,
                           downloaded=True, file_path=str(path)))
    series = Series(id=1, title='Series')
    p = plan(series, chs, tmp_path)
    assert len(p.overlaps) == 2
    assert all(f.referenced and not f.keep for f in p.overlaps)
    assert str(volume) not in default_deletions(p)
    result = apply_cleanup(series, chs, [tmp_path], default_deletions(p))
    assert (result.deleted, result.repointed, result.skipped) == (2, 2, 0)
    assert all(c.file_path == str(volume) and c.downloaded for c in chs)
    assert volume.exists()


def test_omnibus_uses_content_instead_of_outer_volume_number(tmp_path):
    volume = tmp_path / 'Series - Vol. 02.cbz'
    make_labeled(volume, '017', '028x5')  # original volumes 3 and 4
    loose = tmp_path / 'Series - Ch. 0017.cbz'
    make(loose)
    chs = [Chapter(id=1, series_id=1, number=17., volume=3, downloaded=True,
                   file_path=str(loose)),
           Chapter(id=2, series_id=1, number=28.5, volume=4, downloaded=True,
                   file_path=str(volume)),
           Chapter(id=3, series_id=1, number=16., volume=2, downloaded=False)]
    series = Series(id=1, title='Series')
    p = plan(series, chs, tmp_path)
    assert default_deletions(p) == [str(loose)]
    result = apply_cleanup(series, chs, [tmp_path], default_deletions(p))
    assert result.deleted == 1 and result.repointed == 1
    assert chs[0].file_path == str(volume)
    assert not chs[2].downloaded


def test_stale_reference_cannot_prove_disproved_coverage(tmp_path):
    volume = tmp_path / 'Series - Vol. 02.cbz'
    make_labeled(volume, '017', '028x5')
    loose = tmp_path / 'Series - Ch. 0016.cbz'
    make(loose)
    chs = [Chapter(id=1, series_id=1, number=16., volume=2, downloaded=True,
                   file_path=str(volume)),
           Chapter(id=2, series_id=1, number=17., volume=3, downloaded=True,
                   file_path=str(volume)),
           Chapter(id=3, series_id=1, number=28.5, volume=4, downloaded=True,
                   file_path=str(volume))]
    series = Series(id=1, title='Series')
    p = plan(series, chs, tmp_path)
    assert default_deletions(p) == []  # loose 16 is its only actual copy
    result = apply_cleanup(series, chs, [tmp_path], [str(loose)])
    assert result.deleted == 0 and result.skipped == 1 and loose.exists()


def test_volume_can_repoint_each_chapter_to_a_different_survivor(tmp_path):
    volume = tmp_path / 'Series v01.cbz'
    make(volume)
    chs = []
    for n in (1, 2):
        make(tmp_path / f'Series ch{n}.cbz')
        chs.append(Chapter(id=n, series_id=1, number=float(n), volume=1,
                           downloaded=True, file_path=str(volume)))
    result = apply_cleanup(Series(id=1, title='Series'), chs, [tmp_path], [str(volume)])
    assert (result.deleted, result.repointed, result.skipped) == (1, 2, 0)
    assert [c.file_path for c in chs] == [str(tmp_path / f'Series ch{n}.cbz') for n in (1, 2)]


def test_conflicting_batch_never_deletes_both_sides(tmp_path):
    volume = tmp_path / 'Series v01.cbz'
    loose = tmp_path / 'Series ch1.cbz'
    make(volume)
    make(loose)
    ch = Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                 file_path=str(loose))
    result = apply_cleanup(Series(id=1, title='Series'), [ch], [tmp_path], [str(loose), str(volume)])
    assert result.deleted == 0 and result.skipped == 2
    assert ch.file_path == str(loose) and loose.exists() and volume.exists()


def test_stale_downloaded_flag_and_missing_survivor_do_not_allow_deletion(tmp_path):
    volume = tmp_path / 'Series v01.cbz'
    make(volume)
    ch = Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                 file_path=str(tmp_path / 'Series ch1.cbz'))
    series = Series(id=1, title='Series')
    assert default_deletions(plan(series, [ch], tmp_path)) == []
    result = apply_cleanup(series, [ch], [tmp_path], [str(volume)])
    assert result.deleted == 0 and result.skipped == 1 and volume.exists()


def test_failed_unlink_leaves_chapter_pointers_unchanged(tmp_path, monkeypatch):
    from mangarr.library import cleanup
    volume = tmp_path / 'Series v01.cbz'
    loose = tmp_path / 'Series ch1.cbz'
    make(volume)
    make(loose)
    ch = Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                 file_path=str(loose))
    def fail(path):
        raise PermissionError('read-only library')
    monkeypatch.setattr(cleanup.os, 'remove', fail)
    result = apply_cleanup(Series(id=1, title='Series'), [ch], [tmp_path], [str(loose)])
    assert (result.deleted, result.repointed, result.skipped) == (0, 0, 1)
    assert ch.file_path == str(loose) and loose.exists()


def test_nested_folders_and_path_aliases_do_not_create_duplicates(tmp_path):
    folder = tmp_path / 'Series'
    folder.mkdir()
    loose = folder / 'Series ch1.cbz'
    make(loose)
    ch = Chapter(id=1, series_id=1, number=1., downloaded=True,
                 file_path=str(folder / '..' / 'Series' / loose.name))
    p = analyze(Series(id=1, title='Series'), [ch], [tmp_path, folder],
                DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NO_VOLUME)
    assert not p.groups and not p.orphans and not p.overlaps


def test_duplicate_group_and_overlap_defaults_work_as_one_batch(tmp_path):
    original = tmp_path / 'Series v01.cbz'
    duplicate = tmp_path / 'Series - Vol. 01.cbz'
    loose = tmp_path / 'Series ch1.cbz'
    for p in (original, duplicate, loose):
        make(p)
    chs = [Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                   file_path=str(loose)),
           Chapter(id=2, series_id=1, number=2., volume=1, downloaded=True,
                   file_path=str(original))]
    series = Series(id=1, title='Series')
    p = plan(series, chs, tmp_path)
    assert len(p.groups) == 1 and len(p.overlaps) == 1
    result = apply_cleanup(series, chs, [tmp_path], default_deletions(p))
    assert (result.deleted, result.repointed, result.skipped) == (2, 1, 0)
    assert original.exists() and all(c.file_path == str(original) for c in chs)


def test_loose_image_directory_is_never_recommended_or_deleted(tmp_path):
    folder = tmp_path / 'Series ch1'
    folder.mkdir()
    (folder / '001.png').write_bytes(PNG)
    make(tmp_path / 'Series v01.cbz')
    ch = Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                 file_path=str(tmp_path / 'Series v01.cbz'))
    series = Series(id=1, title='Series')
    assert str(folder) not in default_deletions(plan(series, [ch], tmp_path))
    result = apply_cleanup(series, [ch], [tmp_path], [str(folder)])
    assert result.deleted == 0 and result.skipped == 1 and folder.exists()


def test_untracked_archive_bonus_is_not_deleted_with_tracked_overlap(tmp_path):
    volume = tmp_path / 'Series v01.cbz'
    make_labeled(volume, '001', '001x5')
    loose = tmp_path / 'Series ch1.cbz'
    make(loose)
    ch = Chapter(id=1, series_id=1, number=1., volume=1, downloaded=True,
                 file_path=str(loose))
    series = Series(id=1, title='Series')
    p = plan(series, [ch], tmp_path)
    assert str(volume) not in default_deletions(p)
    result = apply_cleanup(series, [ch], [tmp_path], [str(volume)])
    assert result.deleted == 0 and result.skipped == 1 and volume.exists()
