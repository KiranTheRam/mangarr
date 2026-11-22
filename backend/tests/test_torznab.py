"""Torznab contracts, torrent transport, and integration with Mangarr's pipeline."""
import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api import queue, search, settings, series as series_api
from mangarr.download import torrent
from mangarr.jobs import tasks
from mangarr.models import Base, Chapter, Download, DownloadKind, DownloadStatus, HistoryEvent, RootFolder, Series
from mangarr.schemas import GrabIn, SeriesUpdateIn
from mangarr.sources import registry
from mangarr.sources.queries import search_queries, validate_templates
from mangarr.sources.torznab import MASK, TorznabIndexer, configured_indexers, mask_indexers, restore_keys, validate_indexers
from mangarr.torrent_selection import select_best_torrent
from tests.test_torrent_selection import bencode, torrent_with

INDEXER = dict(id="one", name="Prowlarr", url="http://prowlarr:9696/1/api", api_key="test-secret", categories="7030,7020", search_type="search", enabled=True)
HASH = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}"
RSS = '''<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
<title>Series c001-c003</title><comments>https://tracker/details/1</comments>
<enclosure url="http://prowlarr:9696/download/1?apikey=test-secret" length="1234" type="application/x-bittorrent"/>
<torznab:attr name="seeders" value="5"/><torznab:attr name="peers" value="7"/>
<prowlarrindexer>Tracker</prowlarrindexer></item></channel></rss>'''


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        values = dict(settings_service.DEFAULTS)
        for name in registry.DIRECT_SOURCES:
            values[f"source_{name}_enabled"] = "false"
        values.update(qbittorrent_enabled="true", qbittorrent_url="http://qbt:8080", downloads_dir="/downloads", torznab_indexers=json.dumps([INDEXER]))
        await settings_service.set_many(session, values)
        root = RootFolder(path=str(tmp_path))
        tracked = Series(title="Series", sort_title="series", root_folder=root, chapters=[Chapter(number=1, volume=1), Chapter(number=2.5, volume=1), Chapter(number=3, volume=2)])
        session.add(tracked)
        await session.commit()
        yield session, tracked, values
    await engine.dispose()


def test_feed_preserves_file_links_peer_counts_and_tracker_name():
    releases = TorznabIndexer(INDEXER).parse(RSS.encode())
    assert len(releases) == 1
    release = releases[0]
    assert release.source_name == "Torznab / Prowlarr / Tracker"
    assert release.magnet == release.torrent_url == "http://prowlarr:9696/download/1?apikey=test-secret"
    assert (release.size_bytes, release.seeders, release.leechers) == (1234, 5, 2)
    assert release.url == "https://tracker/details/1"


def test_magnet_preferred_but_metadata_url_retained():
    rss = RSS.replace('<torznab:attr name="seeders"', f'<torznab:attr name="magneturl" value="{MAGNET}"/><torznab:attr name="seeders"')
    release = TorznabIndexer(INDEXER).parse(rss.encode())[0]
    assert release.magnet == MAGNET
    assert release.torrent_url.startswith("http://prowlarr")


def test_jackett_link_and_explicit_leechers():
    rss = f'''<rss xmlns:t="http://torznab.com/schemas/2015/feed"><channel><item><title>Series v01</title><link>{MAGNET}</link><jackettindexer>Books</jackettindexer><t:attr name="seeders" value="-1"/><t:attr name="leechers" value="4"/></item></channel></rss>'''
    release = TorznabIndexer(INDEXER).parse(rss.encode())[0]
    assert (release.seeders, release.leechers, release.size_bytes) == (0, 4, 0)
    assert release.source_name.endswith(" / Books")


@pytest.mark.parametrize("xml", ['<error code="100" description="secret"/>', '<html>login</html>', '<rss/>', '<broken'])
def test_rejects_error_and_non_feed_responses(xml):
    with pytest.raises(ValueError):
        TorznabIndexer(INDEXER).parse(xml.encode())


def test_unsafe_links_and_details_are_not_exposed():
    rss = '<rss><channel><item><title>Series</title><link>file:///etc/passwd</link></item><item><title>Series</title><link>' + MAGNET + '</link><comments>javascript:alert(1)</comments></item></channel></rss>'
    releases = TorznabIndexer(INDEXER).parse(rss.encode())
    assert len(releases) == 1
    assert releases[0].url == ""


@pytest.mark.parametrize("patch", [dict(url="file:///a"), dict(url="http://host/api?apikey=secret"), dict(url="http://user:pass@host/api"), dict(url="http://host:bad/api"), dict(categories="7030,abc"), dict(enabled="true"), dict(search_type="tvsearch"), dict(name="")])
def test_invalid_config_rejected(patch):
    with pytest.raises(ValueError):
        validate_indexers(json.dumps([{**INDEXER, **patch}]))


def test_unique_indexers_and_mask_destination_binding():
    raw = json.dumps([INDEXER])
    masked = mask_indexers(raw)
    assert "test-secret" not in masked
    assert json.loads(restore_keys(masked, raw))[0]["api_key"] == "test-secret"
    changed = json.loads(masked)
    changed[0]["url"] = "https://other.example/api"
    with pytest.raises(ValueError, match="re-enter"):
        restore_keys(json.dumps(changed), raw)
    with pytest.raises(ValueError, match="API key"):
        restore_keys(masked, "[]")
    with pytest.raises(ValueError, match="unique"):
        validate_indexers(json.dumps([INDEXER, INDEXER]))
    assert configured_indexers(json.dumps([{**INDEXER, "enabled": False}])) == []


@respx.mock
async def test_search_sends_categories_key_and_book_search():
    route = respx.get(INDEXER["url"]).respond(text=RSS)
    releases = await TorznabIndexer({**INDEXER, "search_type": "book"}).search("Series v01")
    params = route.calls.last.request.url.params
    assert dict(params) == dict(t="book", q="Series v01", apikey="test-secret", cat="7030,7020", extended="1", limit="100")
    assert len(releases) == 1


@pytest.mark.parametrize("template", ["{unknown}", "{series.__class__}", "{series!r}", "{chapter:999999d}", "{chapter:.0f}", "{volume:{chapter}}", "{"])
def test_invalid_templates_rejected(template):
    with pytest.raises(ValueError):
        validate_templates(template)


def test_queries_preserve_decimal_chapters_and_skip_unknown_volumes():
    tracked = Series(title="Series", year=2020, search_title="French Title", search_templates="{series} c{chapter:03d}\n{series} v{volume:02d} {year}")
    validate_templates(tracked.search_templates)
    queries = search_queries(tracked, [Chapter(number=2.5), Chapter(number=3, volume=1)], {})
    assert queries == ["French Title c002.5", "French Title c003", "French Title v01 2020"]
    assert search_queries(tracked, [], {}) == ["French Title"]
    assert len(search_queries(tracked, [Chapter(number=i) for i in range(100)], {})) == 20


def test_info_hash_uses_original_info_dictionary():
    info = {b"length": 12, b"name": b"Series c001.cbz"}
    content = bencode({b"announce": b"https://tracker", b"info": info})
    assert torrent.torrent_info_hash(content) == hashlib.sha1(bencode(info)).hexdigest()
    for invalid in [b"html", b"d4:infoi1ee", b"d4:infode4:infodee", content + b"junk"]:
        with pytest.raises(ValueError):
            torrent.torrent_info_hash(invalid)


@respx.mock
async def test_fetch_follows_relative_redirects_and_magnet_redirect():
    content = torrent_with("Series c001.cbz")
    respx.get("http://indexer/start").respond(302, headers={"location": "/file"})
    respx.get("http://indexer/file").respond(content=content)
    url, info_hash, data = await torrent.fetch_torrent("http://indexer/start")
    assert (url, info_hash, data) == ("http://indexer/file", torrent.torrent_info_hash(content), content)
    respx.get("http://indexer/magnet").respond(302, headers={"location": MAGNET})
    assert await torrent.fetch_torrent("http://indexer/magnet") == (MAGNET, HASH, None)


@respx.mock
async def test_fetch_bounds_data_and_redirects(monkeypatch):
    monkeypatch.setattr(torrent, "MAX_TORRENT_SIZE", 10)
    respx.get("http://indexer/large").respond(content=b"x" * 11)
    with pytest.raises(ValueError, match="large"):
        await torrent.fetch_torrent("http://indexer/large")
    respx.get("http://indexer/loop").respond(302, headers={"location": "/loop"})
    with pytest.raises(ValueError, match="redirects"):
        await torrent.fetch_torrent("http://indexer/loop")
    respx.get("http://indexer/unsafe").respond(302, headers={"location": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="URL"):
        await torrent.fetch_torrent("http://indexer/unsafe")


async def test_settings_api_masks_keys_and_rejects_changed_destination(db):
    session, _, _ = db
    values = await settings.get_settings(session)
    assert json.loads(values["torznab_indexers"])[0]["api_key"] == MASK
    saved = await settings.update_settings({"torznab_indexers": values["torznab_indexers"]}, session)
    assert json.loads(saved["torznab_indexers"])[0]["api_key"] == MASK
    assert "test-secret" in await settings_service.get(session, "torznab_indexers")
    entries = json.loads(values["torznab_indexers"])
    entries[0]["url"] = "http://other/api"
    with pytest.raises(HTTPException) as exc:
        await settings.update_settings({"torznab_indexers": json.dumps(entries)}, session)
    assert exc.value.status_code == 422


async def test_series_templates_save_validate_and_serialize(db):
    session, tracked, _ = db
    result = await series_api.update_series(tracked.id, SeriesUpdateIn(search_title="French Title", search_templates="{series} v{volume:02d}"), session)
    assert result.search_title == "French Title"
    assert result.search_templates == "{series} v{volume:02d}"
    with pytest.raises(HTTPException) as exc:
        await series_api.update_series(tracked.id, SeriesUpdateIn(search_templates="{issue}"), session)
    assert exc.value.status_code == 422


@respx.mock
async def test_interactive_manual_and_scoped_queries(db):
    session, tracked, _ = db
    tracked.search_title = "French Title"
    tracked.search_templates = "{series} c{chapter:03d}"
    await session.commit()
    route = respx.get(INDEXER["url"]).respond(text=RSS)
    results = await search.search_releases(chapter_id=[tracked.chapters[1].id], session=session)
    assert route.calls.last.request.url.params["q"] == "French Title c002.5"
    assert results[0].source_name == "Torznab / Prowlarr / Tracker"
    await search.search_releases(series_id=tracked.id, q="Manual pack query", session=session)
    assert route.calls.last.request.url.params["q"] == "Manual pack query"


@respx.mock
async def test_file_grab_and_retry_upload_to_qbt_and_keep_provenance(db):
    session, tracked, _ = db
    content = torrent_with("Series c001.cbz")
    url = "http://prowlarr:9696/download/1?apikey=test-secret"
    respx.get(url).respond(content=content)
    respx.post("http://qbt:8080/api/v2/auth/login").respond(text="Ok.")
    respx.post("http://qbt:8080/api/v2/torrents/createCategory").respond(200)
    uploaded = respx.post("http://qbt:8080/api/v2/torrents/add").respond(text="Ok.")
    respx.get("http://qbt:8080/api/v2/torrents/info").respond(json=[])
    result = await queue.grab(GrabIn(series_id=tracked.id, source_name="Torznab / Prowlarr / Tracker", magnet=url, title="Series c001"), session)
    dl = await session.get(Download, result.id)
    assert dl.source_name == "Torznab / Prowlarr / Tracker"
    assert dl.torrent_hash == torrent.torrent_info_hash(content)
    assert dl.payload == url
    assert content in uploaded.calls.last.request.content
    assert b'name="savepath"\r\n\r\n/downloads/mangarr' in uploaded.calls.last.request.content
    event = await session.scalar(select(HistoryEvent))
    assert event.source_name == dl.source_name
    dl.status = DownloadStatus.FAILED
    await session.commit()
    retried = await queue.retry_failed_download(dl.id, session)
    assert retried.status == DownloadStatus.DOWNLOADING
    assert len(uploaded.calls) == 2


@respx.mock
async def test_failed_grab_does_not_expose_signed_url_or_key(db):
    session, tracked, _ = db
    url = "http://prowlarr:9696/download/1?apikey=test-secret"
    respx.get(url).respond(403)
    with pytest.raises(HTTPException) as exc:
        await queue.grab(GrabIn(series_id=tracked.id, magnet=url), session)
    assert exc.value.status_code == 502
    assert "test-secret" not in exc.value.detail


@respx.mock
async def test_automatic_selection_inspects_files_and_respects_failure_block(db):
    session, tracked, values = db
    content = torrent_with("Series v01.cbz")
    respx.get(INDEXER["url"]).respond(text=RSS)
    respx.get("http://prowlarr:9696/download/1?apikey=test-secret").respond(content=content)
    indexers = registry.enabled_torrent_indexers(values)
    selected = await select_best_torrent(tracked, tracked.chapters, indexers, max_size_bytes=2000, min_seeders=1, values=values)
    assert selected.coverage == {1, 2.5}
    assert selected.release.source_name == "Torznab / Prowlarr / Tracker"
    assert await select_best_torrent(tracked, tracked.chapters, indexers, max_size_bytes=2000, min_seeders=1, values=values, blocked_payloads={(selected.release.source_name, selected.release.magnet)}) is None


async def test_automatic_search_skips_active_direct_download(db, monkeypatch):
    session, tracked, values = db
    session.add(Download(series_id=tracked.id, chapter_id=tracked.chapters[0].id, kind=DownloadKind.DIRECT, status=DownloadStatus.QUEUED))
    await session.commit()
    async def unexpected(*args, **kwargs):
        raise AssertionError("must not search while a chapter is in flight")
    monkeypatch.setattr(tasks, "select_best_torrent", unexpected)
    assert await tasks.auto_grab_best_torrent(session, tracked, values, only_monitored=True) is None


async def test_monitor_selection_uses_monitored_chapters_seeders_and_failed_payloads(db, monkeypatch):
    session, tracked, values = db
    tracked.chapters[1].monitored = False
    tracked.chapters[2].excluded = True
    session.add(Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED, source_name="Torznab / Prowlarr", payload="failed", torrent_hash=HASH, error="import failed", updated_at=datetime.now(timezone.utc)))
    await session.commit()
    captured = {}
    async def select(series, chapters, indexers, **kwargs):
        captured.update(kwargs)
        captured["chapters"] = [c.number for c in chapters]
        return None
    monkeypatch.setattr(tasks, "select_best_torrent", select)
    assert await tasks.auto_grab_best_torrent(session, tracked, {**values, "torrent_auto_min_seeders": "0"}, only_monitored=True) is None
    assert captured["chapters"] == [1]
    assert captured["min_seeders"] == 1
    assert captured["blocked_payloads"] == {("Torznab / Prowlarr", "failed")}
    assert captured["blocked_hashes"] == {HASH}


@respx.mock
async def test_retry_existing_torrent_does_not_refetch_expired_url(db):
    session, tracked, _ = db
    dl = Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED, source_name="Torznab / Prowlarr", payload="http://expired.example/file?key=expired", torrent_hash=HASH, error="import failed")
    session.add(dl)
    await session.commit()
    respx.post("http://qbt:8080/api/v2/auth/login").respond(text="Ok.")
    respx.get("http://qbt:8080/api/v2/torrents/info").respond(json=[{"hash": HASH, "name": "Series c001", "progress": 1, "state": "stalledUP", "category": "mangarr"}])
    out = await queue.retry_failed_download(dl.id, session)
    assert out.status == DownloadStatus.DOWNLOADING
    assert dl.torrent_hash == HASH
    assert dl.source_name == "Torznab / Prowlarr"


async def test_torrent_failure_cooldown_stays_visible_and_dismissal_unblocks(db):
    from datetime import timedelta
    session, tracked, _ = db
    failed = Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED, source_name="Torznab / Prowlarr", payload="failed", error="import failed", updated_at=datetime.now(timezone.utc) - timedelta(days=3))
    expired = Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED, error="old failure", updated_at=datetime.now(timezone.utc) - timedelta(days=8))
    session.add_all([failed, expired])
    await session.commit()
    assert [d.id for d in await queue.get_queue(session)] == [failed.id]
    await queue._remove_downloads(session, [failed.id])
    assert await queue.get_queue(session) == []
    assert not (await session.execute(select(Download.id).where(tasks.failed_grab_blocks(datetime.now(timezone.utc), include_series=True)))).all()


async def test_automatic_search_does_not_submit_with_unavailable_root(db, monkeypatch):
    session, tracked, values = db
    async def unavailable(*args):
        return "unmounted"
    async def unexpected(*args, **kwargs):
        raise AssertionError("must not search an unavailable library")
    monkeypatch.setattr(tasks, "library_root_unavailable", unavailable)
    monkeypatch.setattr(tasks, "select_best_torrent", unexpected)
    assert await tasks.auto_grab_best_torrent(session, tracked, values, only_monitored=True) is None


async def test_db_migration_adds_search_options_without_losing_existing_series(tmp_path, monkeypatch):
    from mangarr import db as db_module
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE series (id INTEGER PRIMARY KEY, title VARCHAR)")
            await conn.exec_driver_sql("INSERT INTO series VALUES (1, 'Legacy series')")
        monkeypatch.setattr(db_module, "engine", engine)
        await db_module.init_db()
        await db_module.init_db()
        async with engine.connect() as conn:
            row = (await conn.exec_driver_sql("SELECT title, search_title, search_templates FROM series WHERE id = 1")).one()
        assert tuple(row) == ("Legacy series", "", "")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("pieces", [None, b"", b"invalid", 1])
@respx.mock(assert_all_called=False)
async def test_v2_only_or_invalid_hybrid_is_rejected_before_qbt_upload(db, pieces):
    session, tracked, _ = db
    info = {
        b"file tree": {b"Series c001.cbz": {b"": {b"length": 5, b"pieces root": b"r" * 32}}},
        b"meta version": 2,
        b"name": b"Series c001.cbz",
        b"piece length": 16384,
    }
    if pieces is not None:
        info[b"pieces"] = pieces
    content = bencode({b"info": info})
    url = "http://prowlarr:9696/download/v2"
    respx.get(url).respond(content=content)
    upload = respx.post("http://qbt:8080/api/v2/torrents/add").respond(text="Ok.")
    with pytest.raises(ValueError, match="v2-only"):
        torrent.torrent_info_hash(content)
    with pytest.raises(HTTPException) as exc:
        await queue.grab(GrabIn(series_id=tracked.id, magnet=url, title="Series c001"), session)
    assert exc.value.status_code == 502
    assert not upload.called
    assert await session.scalar(select(Download.id)) is None
    assert await session.scalar(select(HistoryEvent.id)) is None


@respx.mock
async def test_hybrid_torrent_upload_retains_v1_hash(db):
    session, tracked, _ = db
    info = {
        b"file tree": {b"Series c001.cbz": {b"": {b"length": 5, b"pieces root": b"r" * 32}}},
        b"length": 5,
        b"meta version": 2,
        b"name": b"Series c001.cbz",
        b"piece length": 16384,
        b"pieces": hashlib.sha1(b"pages").digest(),
    }
    content = bencode({b"info": info})
    expected = hashlib.sha1(bencode(info)).hexdigest()
    assert torrent.torrent_info_hash(content) == expected
    url = "http://prowlarr:9696/download/hybrid"
    respx.get(url).respond(content=content)
    respx.post("http://qbt:8080/api/v2/auth/login").respond(text="Ok.")
    respx.post("http://qbt:8080/api/v2/torrents/createCategory").respond(200)
    upload = respx.post("http://qbt:8080/api/v2/torrents/add").respond(text="Ok.")
    result = await queue.grab(GrabIn(series_id=tracked.id, magnet=url, title="Series c001"), session)
    assert upload.called
    assert content in upload.calls.last.request.content
    dl = await session.get(Download, result.id)
    assert dl.torrent_hash == expected


@pytest.mark.parametrize("renamed_source", [False, True])
@respx.mock
async def test_rotating_signed_url_cannot_bypass_failed_torrent_hash_cooldown(db, renamed_source):
    session, tracked, values = db
    metadata = torrent_with("Series v01.cbz")
    old_url = "http://prowlarr:9696/download/1?token=old"
    new_url = "http://prowlarr:9696/download/1?token=new"
    failed_source = "Torznab / Original feed" if renamed_source else "Torznab / Prowlarr / Tracker"
    session.add(Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED,
                         source_name=failed_source, payload=old_url,
                         torrent_hash=torrent.torrent_info_hash(metadata), error="import failed"))
    await session.commit()
    respx.get(INDEXER["url"]).respond(text=RSS.replace("http://prowlarr:9696/download/1?apikey=test-secret", new_url))
    respx.get(new_url).respond(content=metadata)
    # This exercises the real failure query and selector. Any qBittorrent
    # request is unexpected, proving the changed link was not re-submitted.
    assert await tasks.auto_grab_best_torrent(session, tracked, values, only_monitored=True) is None
    assert len((await session.execute(select(Download.id))).all()) == 1
    # Without a block, the new URL is retained for a future legitimate grab.
    selected = await select_best_torrent(tracked, tracked.chapters,
        registry.enabled_torrent_indexers(values), max_size_bytes=2000, min_seeders=1, values=values)
    assert selected.release.magnet == new_url


async def test_same_magnet_hash_is_blocked_without_metadata_after_source_rename(db):
    from mangarr.sources.base import TorrentRelease
    session, tracked, values = db
    class MagnetIndexer:
        name = "Renamed feed"
        async def search(self, query):
            return [TorrentRelease(source_name=self.name, title="Series c001", magnet=MAGNET + "&dn=new-title", seeders=3, size_bytes=100)]
        async def get_torrent_metadata(self, release):
            raise AssertionError("A blocked magnet does not need metadata inspection")
    assert await select_best_torrent(tracked, tracked.chapters, [MagnetIndexer()],
        max_size_bytes=2000, min_seeders=1, blocked_hashes={HASH.upper()}) is None


@respx.mock
async def test_rotating_url_redirect_to_failed_magnet_keeps_hash_cooldown(db):
    session, tracked, values = db
    old_url = "http://prowlarr:9696/download/1?token=old"
    new_url = "http://prowlarr:9696/download/1?token=new"
    session.add(Download(series_id=tracked.id, kind=DownloadKind.TORRENT, status=DownloadStatus.FAILED,
                         source_name="Renamed feed", payload=old_url,
                         torrent_hash=HASH, error="import failed"))
    await session.commit()
    respx.get(INDEXER["url"]).respond(text=RSS.replace("http://prowlarr:9696/download/1?apikey=test-secret", new_url))
    respx.get(new_url).respond(302, headers={"location": MAGNET})
    assert await tasks.auto_grab_best_torrent(session, tracked, values, only_monitored=True) is None
    assert len((await session.execute(select(Download.id))).all()) == 1
    selected = await select_best_torrent(tracked, tracked.chapters,
        registry.enabled_torrent_indexers(values), max_size_bytes=2000, min_seeders=1, values=values)
    assert selected.release.magnet == new_url
    assert selected.release.info_hash == HASH
