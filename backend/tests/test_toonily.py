import asyncio
import zipfile

import httpx
import pytest
import respx

from mangarr import settings_service
from mangarr.download.direct import download_chapter_to_cbz
from mangarr.models import Chapter, Series
from mangarr.sources import registry, toonily
from mangarr.sources.toonily import BASE_URL, BROWSER_AGENT, ToonilySource

SERIES = "solo-leveling-574def58"
SERIES_URL = f"{BASE_URL}/serie/{SERIES}/"
CHAPTER = f"/serie/{SERIES}/chapter-1/"
IMAGE = "https://data.tnlycdn.com/chapter/002.jpg"
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
    respx.get(BASE_URL + CHAPTER).respond(200, text=reader)
    respx.get(BASE_URL + other).respond(200, text=reader)
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
    respx.get(BASE_URL + CHAPTER).respond(200, text=f"""
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
    respx.get(BASE_URL + CHAPTER).respond(200, text=f"""
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
