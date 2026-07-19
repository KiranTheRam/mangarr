from mangarr.models import Chapter, Series
from mangarr.sources.base import TorrentIndexer, TorrentRelease
from mangarr.torrent_selection import (
    coverage_from_text,
    select_best_torrent,
    torrent_coverage,
    torrent_paths,
)


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            bencode(key) + bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


def torrent_with(*names: str) -> bytes:
    return bencode(
        {
            b"announce": b"https://tracker",
            b"info": {
                b"name": b"release",
                b"files": [
                    {b"length": 123, b"path": [part.encode() for part in name.split("/")]}
                    for name in names
                ],
            },
        }
    )


def chapters():
    return [
        Chapter(number=1, volume=1),
        Chapter(number=2, volume=1),
        Chapter(number=2.5, volume=1),
        Chapter(number=3, volume=2),
        Chapter(number=4, volume=2),
    ]


def test_torrent_paths_reads_multifile_info():
    metadata = torrent_with("Series/Series c002.5.cbz", "Series/cover.jpg")
    assert torrent_paths(metadata) == ["Series/Series c002.5.cbz", "Series/cover.jpg"]


def test_torrent_file_list_covers_chapters_and_whole_volumes():
    metadata = torrent_with("Series/Series v01.cbz", "Series/Series c003.cbz")
    assert torrent_coverage(metadata, "Series release", chapters()) == {1, 2, 2.5, 3}


def test_release_title_ranges_are_a_fallback():
    assert coverage_from_text("Series c001-c003", chapters()) == {1, 2, 2.5, 3}
    assert coverage_from_text("Series v01-v02 (Digital)", chapters()) == {1, 2, 2.5, 3, 4}


def test_file_list_overrides_broad_title_range():
    metadata = torrent_with(
        "Series/Series c001.cbz", "Series/Series c002.cbz", "Series/Series c003.cbz"
    )
    assert torrent_coverage(metadata, "Series c001-c003", chapters()) == {1, 2, 3}


class FakeIndexer(TorrentIndexer):
    name = "fake"

    def __init__(self, releases, metadata):
        self.releases = releases
        self.metadata = metadata

    async def search(self, query: str):
        return self.releases if query == "Series" else []

    async def get_torrent_metadata(self, release: TorrentRelease) -> bytes:
        return self.metadata[release.magnet]


async def test_selects_most_coverage_then_specials():
    smaller = TorrentRelease(
        source_name="fake",
        title="Series c001-c003",
        magnet="magnet:small",
        torrent_url="https://small.torrent",
        size_bytes=100,
        seeders=20,
    )
    better = TorrentRelease(
        source_name="fake",
        title="Series volume pack",
        magnet="magnet:better",
        torrent_url="https://better.torrent",
        size_bytes=200,
        seeders=2,
    )
    indexer = FakeIndexer(
        [smaller, better],
        {
            smaller.magnet: torrent_with("Series c001.cbz", "Series c002.cbz", "Series c003.cbz"),
            better.magnet: torrent_with("Series v01.cbz", "Series v02.cbz"),
        },
    )
    series = Series(title="Series", sort_title="series")
    tracked = chapters()
    for chapter in tracked:
        chapter.monitored = True
        chapter.downloaded = False

    selected = await select_best_torrent(
        series, tracked, [indexer], max_size_bytes=1000, min_seeders=1
    )

    assert selected is not None
    assert selected.release is better
    assert selected.coverage == {1, 2, 2.5, 3, 4}


async def test_selects_from_title_when_metadata_is_unavailable():
    release = TorrentRelease(
        source_name="fake",
        title="Series c001-c003",
        magnet="magnet:title-only",
        size_bytes=100,
        seeders=5,
    )
    indexer = FakeIndexer([release], {release.magnet: b""})

    selected = await select_best_torrent(
        Series(title="Series", sort_title="series"),
        chapters(),
        [indexer],
        max_size_bytes=1000,
        min_seeders=1,
    )

    assert selected is not None
    assert selected.release is release
    assert selected.coverage == {1, 2, 2.5, 3}
