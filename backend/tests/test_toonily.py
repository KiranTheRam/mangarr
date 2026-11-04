import asyncio
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api import library, queue, search
from mangarr.download.direct import download_chapter_to_cbz
from mangarr.jobs import tasks
from mangarr.models import (
    Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series, SeriesSourceLink,
)
from mangarr.schemas import GrabIn
from mangarr.sources import registry, toonily
from mangarr.sources.base import SourceChapter
from mangarr.sources.routing import SourceRoute, set_routes
from mangarr.sources.toonily import BASE_URL, BROWSER_AGENT, ToonilySource

SERIES = "solo-leveling-574def58"
SERIES_URL = f"{BASE_URL}/serie/{SERIES}/"
CHAPTER = f"/serie/{SERIES}/chapter-1/"
IMAGE = "https://data.tnlycdn.com/chapter/002.jpg"
TBATE = "the-beginning-after-the-end-54f5cb7c"
RANKER = "i-stole-the-number-one-rankers-soul-fd156026"
RANKER_TITLE = "I Stole the Number One Ranker’s Soul"
READER_LABEL = '<ol class="breadcrumb"><li class="active">Chapter 1</li></ol>'
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"page-content"


@pytest.fixture(autouse=True)
def no_rate_wait(monkeypatch):
    monkeypatch.setattr(toonily, "_limiter", None)
    monkeypatch.setattr(ToonilySource, "image_limiter", None)


@respx.mock
async def test_search_redirects_and_deduplicates_public_series():
    respx.get(BASE_URL + "/", params={"s": "Solo Leveling", "post_type": "wp-manga"}).respond(
        302, headers={"Location": "/search/solo-leveling"},
    )
    route = respx.get(BASE_URL + "/search/solo-leveling").respond(200, text=f"""
      <div class="post-title"><a href="{SERIES_URL}">Solo Leveling</a></div>
      <div class="post-title"><a href="{SERIES_URL}">Duplicate</a></div>
      <div class="post-title"><a href="https://other.test/serie/bad/">Ad</a></div>
      <div class="post-title"><a href="{SERIES_URL}chapter-1/">Chapter</a></div>
    """)
    source = ToonilySource()
    results = await source.search_series("Solo Leveling")
    assert [(r.external_id, r.title, r.url) for r in results] == [(SERIES, "Solo Leveling", SERIES_URL)]
    assert route.calls.last.request.headers["User-Agent"] == BROWSER_AGENT
    assert await source.search_series("  ") == []
    await source._client.aclose()


@respx.mock
async def test_chapters_preserve_decimals_and_skip_independent_special_counters():
    respx.get(SERIES_URL).respond(200, text=f"""
      <ul>
        <li class="wp-manga-chapter"><a href="{SERIES_URL}side-story-1/">Side Story 1</a></li>
        <li class="wp-manga-chapter"><a href="{SERIES_URL}chapter-12-25/">Chapter 12.25 - Extra</a></li>
        <li class="wp-manga-chapter"><a href="{SERIES_URL}chapter-1/">Chapter 1</a></li>
        <li class="wp-manga-chapter"><a href="{SERIES_URL}chapter-0/">Chapter 0 - Prologue</a></li>
        <li class="wp-manga-chapter"><a href="{SERIES_URL}chapter-1/">Chapter 1</a></li>
        <li class="wp-manga-chapter"><a href="/serie/other/chapter-2/">Chapter 2</a></li>
        <li class="wp-manga-chapter"><a href="https://other.test/chapter-3/">Chapter 3</a></li>
      </ul>
    """)
    source = ToonilySource()
    chapters = await source.list_chapters(SERIES)
    assert [c.number for c in chapters] == [0, 1, 12.25]
    assert chapters[1].external_id == CHAPTER
    assert chapters[2].title == "Chapter 12.25 - Extra"
    await source._client.aclose()


@respx.mock
async def test_reader_order_lazy_images_and_request_local_referers():
    reader = """<div class="reading-content">
      <img class="wp-manga-chapter-img" data-src="https://data.tnlycdn.com/chapter/002.jpg" src="data:image/gif;base64,placeholder">
      <img class="wp-manga-chapter-img" data-lazy-src="//data.tnlycdn.com/chapter/003.jpg">
      <img class="wp-manga-chapter-img" src="https://data.tnlycdn.com/chapter/002.jpg">
      <img class="wp-manga-chapter-img"><img class="wp-manga-chapter-img" src="javascript:bad">
      <img src="https://ads.test/ad.jpg">
    </div>"""
    other = CHAPTER.replace("chapter-1", "chapter-2")
    respx.get(BASE_URL + CHAPTER).respond(200, text=READER_LABEL + reader)
    respx.get(BASE_URL + other).respond(200, text=READER_LABEL + reader)
    seen = []

    def image_response(request):
        seen.append(request.headers["Referer"])
        assert request.headers["User-Agent"] == BROWSER_AGENT
        assert request.headers["Accept"] == "image/jpeg"
        return httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp"})

    respx.get(IMAGE).mock(side_effect=image_response)
    source = ToonilySource()
    first, second = await asyncio.gather(source.get_pages(CHAPTER), source.get_pages(other))
    assert first == [IMAGE, IMAGE.replace("002", "003")]
    async with httpx.AsyncClient() as client:
        await asyncio.gather(source.download_page(client, first[0]), source.download_page(client, second[0]))
        with pytest.raises(ValueError, match="Referer"):
            await source.download_page(client, IMAGE)
    assert set(seen) == {BASE_URL + CHAPTER, BASE_URL + other}
    await source._client.aclose()


@respx.mock
@pytest.mark.parametrize("suffix, accept", [(".JPEG?token=test", "image/jpeg"), (".png", "*/*"), (".webp", "*/*")])
async def test_jpeg_preference_preserves_other_image_formats(suffix, accept):
    image_url = "https://data.tnlycdn.com/chapter/page" + suffix
    respx.get(BASE_URL + CHAPTER).respond(200, text=READER_LABEL + f"""
      <div class="reading-content"><img class="wp-manga-chapter-img" src="{image_url}"></div>
    """)
    image = respx.get(image_url).respond(200, content=WEBP)
    source = ToonilySource()
    pages = await source.get_pages(CHAPTER)
    async with httpx.AsyncClient() as client:
        assert await source.download_page(client, pages[0]) == WEBP
    assert image.calls.last.request.headers["Accept"] == accept
    await source._client.aclose()


@respx.mock
async def test_real_downloader_packages_webp_at_jpg_url_and_uses_proxy(tmp_path, monkeypatch):
    respx.get(BASE_URL + CHAPTER).respond(200, text=READER_LABEL + f"""
      <div class="reading-content"><img class="wp-manga-chapter-img" src="{IMAGE}"></div>
    """)
    respx.get(IMAGE).respond(200, content=WEBP)
    source = ToonilySource()
    source.content_proxy_enabled = True
    source.content_proxy_url = "http://test-proxy:8118"
    real_client = httpx.AsyncClient
    options = []

    def client_factory(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("mangarr.download.direct.httpx.AsyncClient", client_factory)
    dest = tmp_path / "chapter.cbz"
    await download_chapter_to_cbz(
        source, CHAPTER, Series(title="Solo Leveling", chapters=[]), Chapter(number=1), dest,
    )
    assert options[0]["proxy"] == "http://test-proxy:8118"
    assert options[0]["trust_env"] is False
    with zipfile.ZipFile(dest) as archive:
        assert archive.read("001.webp") == WEBP
        assert "ComicInfo.xml" in archive.namelist()
    await source._client.aclose()


@respx.mock
async def test_http_errors_propagate_instead_of_looking_empty():
    respx.get(SERIES_URL).respond(403)
    source = ToonilySource()
    with pytest.raises(httpx.HTTPStatusError):
        await source.list_chapters(SERIES)
    await source._client.aclose()


@pytest.mark.parametrize("bad", ["https://other.test/serie/a/chapter-1/", "/serie/a/../chapter-1/", "//localhost/a", "/serie/a/"])
async def test_invalid_chapter_ids_make_no_requests(bad):
    source = ToonilySource()
    with pytest.raises(ValueError):
        await source.get_pages(bad)
    await source._client.aclose()


def test_registered_opt_in_source_supports_content_proxy():
    assert "toonily" in registry.DIRECT_SOURCES
    assert settings_service.DEFAULTS["source_toonily_enabled"] == "false"
    assert "toonily" in settings_service.CONTENT_SOURCE_NAMES
    values = {**settings_service.DEFAULTS, "source_toonily_enabled": "true"}
    assert registry.DIRECT_SOURCES["toonily"] in registry.enabled_direct_sources(values)


def search_page(query, slug, text):
    respx.get(BASE_URL + "/", params={"s": query, "post_type": "wp-manga"}).respond(
        302, headers={"Location": f"/search/{slug}"},
    )
    return respx.get(f"{BASE_URL}/search/{slug}").respond(200, text=text)


def card(slug, title):
    return f'<div class="post-title"><a href="/serie/{slug}/">{title}</a></div>'


def next_link(url):
    return f'<a aria-label="Next Page" class="nextpostslink" href="{url}" rel="next">»</a>'


@respx.mock
async def test_search_follows_public_pages_deduplicates_and_stops_at_three(monkeypatch):
    root = f"{BASE_URL}/search/love"
    first_page = card("love-rebooted-625ab4fa", "Love Rebooted")
    first_page += "".join(card(f"first-{i}", f"First {i}") for i in range(17))
    search_page("Love", "love", first_page + next_link(root + "/page/2/"))
    second_page = card("love-rebooted-625ab4fa", "Duplicate")
    second_page += "".join(card(f"second-{i}", f"Second {i}") for i in range(2))
    second_page += card("red-fox", "Red Fox")
    respx.get(root + "/page/2/").respond(200, text=second_page + next_link(root + "/page/3/"))
    respx.get(root + "/page/3/").respond(
        200, text=card("third", "Third page result") + next_link(root + "/page/4/"),
    )
    async with httpx.AsyncClient() as client:
        source = ToonilySource(client)

        async def fake_load(*args):
            pass

        async def fake_settings(*args):
            return {}

        monkeypatch.setattr(library, "_load", fake_load)
        monkeypatch.setattr(registry, "apply_settings", fake_settings)
        monkeypatch.setitem(registry.DIRECT_SOURCES, "toonily", source)
        results = await library.source_search(1, "toonily", "Love", session=None)
    assert len(results) == 22
    assert results[0].title == "Love Rebooted"
    assert [result.title for result in results[-2:]] == ["Red Fox", "Third page result"]


@respx.mock
@pytest.mark.parametrize("next_url", [
    "https://outside.test/search/love/page/2/",
    "https://toonily.com@outside.test/search/love/page/2/",
    "/search/other/page/2/",
    "/search/love/",
])
async def test_search_ignores_offsite_cross_query_and_repeated_next_links(next_url):
    cards = "".join(card(f"item-{i}", f"Item {i}") for i in range(4))
    search_page("Love", "love", cards + next_link(next_url))
    async with httpx.AsyncClient() as client:
        assert len(await ToonilySource(client).search_series("Love")) == 4
    assert len(respx.calls) == 2


@respx.mock
@pytest.mark.parametrize("apostrophe", ["'", "’"])
async def test_ranker_fallback_requires_a_full_title_match(apostrophe):
    search_page(
        RANKER_TITLE.replace("’", apostrophe),
        "i-stole-the-number-one-rankers-soul", "",
    )
    search_page(
        "Number One Ranker", "number-one-ranker",
        card("different", RANKER_TITLE + " Returns") + card(RANKER, RANKER_TITLE),
    )
    async with httpx.AsyncClient() as client:
        matches = await search._find_direct_source_ids(
            ToonilySource(client), [RANKER_TITLE.replace("’", apostrophe)],
        )
    assert [match[0] for match in matches] == [RANKER]
    assert len(respx.calls) == 4


@respx.mock
async def test_search_can_link_published_aliases_without_matching_unrelated_korean_titles(monkeypatch):
    search_page("TBATE", "tbate", card(TBATE, "The Beginning After the End"))
    details = respx.get(f"{BASE_URL}/serie/{TBATE}/").respond(200, text="""
      <div class="post-title">
        <h1>The Beginning After the End <span class="manga-title-badges top">TOP</span></h1>
      </div>
      <div class="post-content_item">
        <div class="summary-heading"><h5>Alt Name(s)</h5></div>
        <div class="summary-content">끝이 아닌 시작, TBATE</div>
      </div>
    """)
    async with httpx.AsyncClient() as client:
        source = ToonilySource(client)
        hits = await source.search_series("TBATE")
        assert hits[0].alt_titles == ["끝이 아닌 시작", "TBATE"]
        assert (await source.get_series_details(TBATE)).title == "The Beginning After the End"
        monkeypatch.setattr(registry, "enabled_direct_sources", lambda _: [source])
        series = Series(id=53, title="TBATE")
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add(series)
                await tasks.link_sources(session, series, {})
            assert [link.external_id for link in series.source_links] == [TBATE]
        finally:
            await engine.dispose()
    assert details.call_count == 3
    assert search._candidate_matches_series(["끝이 아닌 시작"], {"끝이 아닌 시작"})
    assert not search._candidate_matches_series(["랭킹 1위를 영혼까지 털어버림"], {"1"})


@respx.mock
async def test_reader_and_catalog_reject_unmapped_side_story_episodes():
    path = f"/serie/{TBATE}/"
    teaser = path + "chapter-175-8/"
    final = path + "chapter-175-8_11/"
    final_label = "Chapter 175.8 - Side Story - Jasmine Wind-Borne - Ep. 10 (The End)"
    respx.get(BASE_URL + path).respond(200, text=f"""
      <li class="wp-manga-chapter"><a href="{final}">{final_label}</a></li>
      <li class="wp-manga-chapter"><a href="{teaser}">Chapter 175.8 - Side Story - Jasmine Wind-Borne - Ep. 0 TEASER</a></li>
      <li class="wp-manga-chapter"><a href="{path}chapter-175/">Chapter 175</a></li>
      <li class="wp-manga-chapter"><a href="{path}chapter-175/">Chapter 175</a></li>
      <li class="wp-manga-chapter"><a href="{path}chapter-174-a/">Chapter 174 - Part A</a></li>
      <li class="wp-manga-chapter"><a href="{path}chapter-174-b/">Chapter 174 - Part B</a></li>
    """)
    respx.get(BASE_URL + final).respond(200, text=f"""
      <ol class="breadcrumb"><li class="active">{final_label}</li></ol>
      <div class="reading-content"><img class="wp-manga-chapter-img" src="{IMAGE}"></div>
    """)
    old_part = path + "chapter-174-a/"
    respx.get(BASE_URL + old_part).respond(200, text=f"""
      <ol class="breadcrumb"><li class="active">Chapter 174 - Part A</li></ol>
      <div class="chapter-option" data-value="chapter-174-b">Chapter 174 - Part B</div>
      <div class="reading-content"><img class="wp-manga-chapter-img" src="{IMAGE}"></div>
    """)
    async with httpx.AsyncClient() as client:
        source = ToonilySource(client)
        assert [chapter.number for chapter in await source.list_chapters(TBATE)] == [175]
        assert not await source.verify_chapters([final, old_part])
        assert not await source.get_pages(final)


class PrefixOnlyImage(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield WEBP[:5]
        yield WEBP[5:] + b"\x00" * 32
        pytest.fail("verification continued reading the image body")


def reader_html(image):
    return READER_LABEL + f"""
      <div class="reading-content"><img class="wp-manga-chapter-img" data-src="{image}"></div>
    """


@respx.mock
async def test_verification_reads_a_prefix_and_caches_per_task_route(monkeypatch):
    reader = respx.get(BASE_URL + CHAPTER).respond(200, text=reader_html(IMAGE))
    proxy_calls = []
    client_factory = httpx.AsyncClient

    def image_client(**kwargs):
        assert kwargs["trust_env"] is False
        proxy_calls.append(kwargs["proxy"])
        return client_factory(**kwargs)

    def first_image(request):
        assert request.headers["Range"] == "bytes=0-31"
        assert request.headers["Accept"] == "image/jpeg"
        assert request.headers["Referer"] == BASE_URL + CHAPTER
        return httpx.Response(200, stream=PrefixOnlyImage())

    img = respx.get(IMAGE).mock(side_effect=first_image)
    now = 1000
    monkeypatch.setattr(toonily, "time", SimpleNamespace(monotonic=lambda: now))
    async with client_factory() as client:
        source = ToonilySource(client)
        monkeypatch.setattr(toonily.httpx, "AsyncClient", image_client)
        proxy = "http://test-proxy:8118"

        async def check(route):
            set_routes({"toonily": route})
            return await source.verify_chapters([CHAPTER, BASE_URL + CHAPTER])

        assert await asyncio.gather(
            check(SourceRoute()),
            check(SourceRoute(content_enabled=True, content_url=proxy)),
            check(SourceRoute(content_enabled=True)),
        ) == [{CHAPTER, BASE_URL + CHAPTER}, {CHAPTER, BASE_URL + CHAPTER}, set()]
        assert proxy_calls == [None, proxy]
        assert await source.verify_chapters([CHAPTER], max_checks=0) == {CHAPTER}
        now += toonily.AVAILABLE_TTL
        assert await source.verify_chapters([CHAPTER], max_checks=0) == set()
    assert reader.call_count == img.call_count == 2


@respx.mock
async def test_verification_checks_only_bounded_candidates_and_rejects_unsafe_responses(monkeypatch):
    offsite, redirected, too_large, later = [CHAPTER.replace("chapter-1", f"chapter-{n}") for n in range(2, 6)]
    respx.get(BASE_URL + CHAPTER).respond(200, text=reader_html(IMAGE))
    respx.get(BASE_URL + offsite).respond(200, text=reader_html("https://data.tnlycdn.com@127.0.0.1/image"))
    respx.get(IMAGE).respond(302, headers={"Location": "http://127.0.0.1/image"})
    respx.get(BASE_URL + redirected).respond(302, headers={"Location": "http://127.0.0.1/reader"})
    respx.get(BASE_URL + too_large).respond(
        200, headers={"Content-Length": str(toonily.MAX_READER_BYTES + 1)},
    )
    unchecked = respx.get(BASE_URL + later).respond(200, text=reader_html(IMAGE))
    monkeypatch.setattr(toonily, "MAX_CHAPTER_CHECKS", 2)
    async with httpx.AsyncClient() as client:
        source = ToonilySource(client)
        assert not await source.verify_chapters([CHAPTER, offsite, later], max_checks=None)
        assert not unchecked.called
        assert not await source.verify_chapters([redirected])
        assert not await source.verify_chapters([too_large])
    assert len(respx.calls) == 5


@respx.mock
async def test_release_workflows_fall_through_to_another_source(db_toonily, monkeypatch):
    session, series = db_toonily
    respx.get(SERIES_URL).respond(
        200, text=f'<li class="wp-manga-chapter"><a href="{CHAPTER}">Chapter 1</a></li>',
    )
    reader = respx.get(BASE_URL + CHAPTER).respond(200, text=reader_html(IMAGE))
    image = respx.get(IMAGE).respond(200, stream=HtmlImage())
    async with httpx.AsyncClient() as client:
        source = ToonilySource(client)

        async def list_alternates(_):
            return [SourceChapter("alternate", "chapter-1", 1)]

        async def no_metadata(_):
            return []

        async def no_volumes(_):
            return {}

        alternate = SimpleNamespace(
            name="alternate", list_chapters=list_alternates,
            get_chapter_metadata=no_metadata, get_volume_map=no_volumes,
        )
        sources = [source, alternate]
        monkeypatch.setattr(registry, "enabled_direct_sources", lambda _: sources)
        monkeypatch.setattr(registry, "series_direct_sources", lambda *_: sources)
        monkeypatch.setitem(registry.DIRECT_SOURCES, "toonily", source)
        monkeypatch.setitem(registry.DIRECT_SOURCES, "alternate", alternate)
        values = {"qbittorrent_enabled": "false"}

        await tasks.update_chapters(session, series, values)
        assert not reader.called
        chapter = series.chapters[0]
        assert chapter.available_sources == "alternate"
        releases = await search.search_releases(
            chapter_id=[chapter.id], include_torrents=False, session=session,
        )
        assert [release.source_name for release in releases] == ["alternate"]

        with pytest.raises(HTTPException, match="could not verify"):
            await queue.grab(GrabIn(
                chapter_id=chapter.id, source_name="toonily", external_id=CHAPTER,
            ), session)
        failed = Download(
            chapter_id=chapter.id, series_id=series.id, source_name="toonily",
            payload=CHAPTER, status=DownloadStatus.FAILED, kind=DownloadKind.DIRECT,
        )
        session.add(failed)
        await session.commit()
        with pytest.raises(HTTPException, match="could not verify"):
            await queue.retry_failed_download(failed.id, session)

        assert await tasks.grab_missing_chapters(session, series, values) == 1
        queued = (await session.execute(select(Download).where(
            Download.status == DownloadStatus.QUEUED,
        ))).scalar_one()
        assert queued.source_name == "alternate"
        assert reader.call_count == image.call_count == 1


class HtmlImage(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"<html>locked or broken image</html>"


@pytest.fixture
async def db_toonily(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            series = Series(title="Solo Leveling", root_folder=RootFolder(path=str(tmp_path)))
            series.chapters.append(Chapter(number=1, monitored=True))
            series.source_links.extend([
                SeriesSourceLink(source_name="toonily", external_id=SERIES),
                SeriesSourceLink(source_name="alternate", external_id="alt"),
            ])
            session.add(series)
            await session.commit()
            yield session, series
    finally:
        await engine.dispose()
