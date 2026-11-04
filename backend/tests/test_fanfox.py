import httpx
import pytest
import respx

from mangarr import settings_service
from mangarr.sources import fanfox, registry, routing

SERIES_PATH = "/manga/kingdom/"
CHAPTER = "kingdom/v36/c393.1"
READER_PATH = f"/manga/{CHAPTER}/"
CDN = "https://zjcdn.mangafox.me/store/manga/8198/36-393.1/compressed/"
COVER = "https://fmcdn.mfcdn.net/store/manga/8198/cover.jpg"
SEARCH = f"""
<div class="search-main"><ul class="post-list">
  <li><div class="post-one"><a href="https://m.fanfox.net/manga/kingdom">
    <span class="cover"><img src="{COVER}" title="Kingdom"></span>
    <div class="cover-info"><p class="title">Kingdom</p></div>
  </a></div></li>
  <li><div class="post-one"><a href="//other.example/manga/other">Other</a></div></li>
</ul></div>
"""
SERIES = f"""
<div class="manga-detail">
  <div class="manga-detail-top"><p class="title">Kingdom</p><img src="{COVER}"></div>
  <div class="manga-summary">A series summary.</div>
  <div class="manga-chapters"><dl><dd class="chlist">
    <a href="//m.fanfox.net{READER_PATH}1.html">Ch 393.1 Bonus <span>Jun 29, 2014</span></a>
    <a href="{READER_PATH}1.html">Ch 393.1 Bonus</a>
    <a href="/manga/kingdom/v36/c393.12/1.html">Ch 393.12 Extra</a>
    <a href="/manga/kingdom/v36/c393/1.html">Ch 393</a>
    <a href="/manga/kingdom/v01/c001/1.html">Ch 1 First chapter</a>
    <a href="/manga/kingdom/v00/c001/1.html">Ch 1 Pilot</a>
    <a href="/manga/kingdom/v36/c395/1.html">Ch 394 Wrong number</a>
    <a href="/manga/kingdom/v36/cbonus/1.html">Extra 396</a>
    <a href="/manga/kingdom/c472v0/1.html">Ch 472</a>
    <a href="/manga/other/v36/c396/1.html">Ch 396</a>
  </dd></dl></div>
</div>
"""


def reader(page, *, chapter_id=326751, manifest=(1, 2, 3), cdn=CDN):
    options = "".join(
        f'<option value="//m.fanfox.net{READER_PATH}{n}.html">{n}</option>'
        for n in manifest
    )
    return f"""
    <div id="viewer"><img id="image" src="{cdn}{page}.jpg?token=sample&amp;ttl=123"></div>
    <img src="{CDN}preload.jpg">
    <select class="mangaread-page">{options}</select>
    <script>var series_id = 8198; var chapter_id = {chapter_id}; var series_page = {page};</script>
    """


@pytest.fixture(autouse=True)
def unthrottled(monkeypatch):
    monkeypatch.setattr(fanfox, "_limiter", None)
    monkeypatch.setattr(fanfox.FanFoxSource, "image_limiter", None)


async def test_public_search_details_and_chapters():
    def handle(request):
        if request.url.path == "/search":
            assert request.url.params["k"] == "Kingdom"
            return httpx.Response(200, text=SEARCH)
        assert request.url.path == SERIES_PATH
        return httpx.Response(200, text=SERIES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = fanfox.FanFoxSource(client)
        results = await source.search_series("Kingdom")
        assert [(r.external_id, r.title, r.cover_url) for r in results] == [
            ("kingdom", "Kingdom", COVER)
        ]
        details = await source.get_series_details("kingdom")
        assert details.title == "Kingdom"
        assert details.description == "A series summary."
        assert details.cover_url == COVER
        chapters = await source.list_chapters("kingdom")

    assert [(c.number, c.volume, c.title) for c in chapters] == [
        (393, 36, ""), (393.1, 36, "Bonus"), (393.12, 36, "Extra"),
    ]
    assert chapters[1].external_id == CHAPTER


async def test_public_reader_manifest_and_image_referer():
    def handle(request):
        if request.url.host == "m.fanfox.net":
            assert request.url.path.startswith(READER_PATH)
            page = int(request.url.path.removeprefix(READER_PATH).removesuffix(".html"))
            return httpx.Response(200, text=reader(page))
        assert request.headers["referer"] == f"{fanfox.BASE_URL}/"
        assert "cookie" not in request.headers
        assert str(request.url) == f"{CDN}1.jpg?token=sample&ttl=123"
        return httpx.Response(200, content=b"image bytes")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = fanfox.FanFoxSource(client)
        pages = await source.get_pages(CHAPTER)
        assert pages == [f"{CDN}{page}.jpg?token=sample&ttl=123" for page in (1, 2, 3)]
        assert await source.download_page(client, pages[0]) == b"image bytes"


@pytest.mark.parametrize(("first", "second", "error"), [
    ("<p>Sorry, it’s licensed and not available.</p>", "", "unavailable"),
    (reader(1, manifest=(1, 3)), "", "invalid page list"),
    (reader(1, cdn="https://other.example/store/manga/"), "", "no supported image"),
    (reader(1), reader(1), "wrong page"),
    (reader(1), reader(2, chapter_id=326752), "different chapter"),
])
async def test_incomplete_or_unavailable_reader(first, second, error):
    def handle(request):
        if request.url.path == f"{READER_PATH}1.html":
            return httpx.Response(200, text=first)
        assert request.url.path == f"{READER_PATH}2.html"
        return httpx.Response(200, text=second)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match=error):
            await fanfox.FanFoxSource(client).get_pages(CHAPTER)


async def test_invalid_ids_do_not_request_a_page():
    def unexpected(request):
        pytest.fail(f"Unexpected request to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        source = fanfox.FanFoxSource(client)
        with pytest.raises(ValueError, match="Invalid FanFox series ID"):
            await source.list_chapters("../other")
        with pytest.raises(ValueError, match="Invalid FanFox chapter ID"):
            await source.get_pages("kingdom/v36/c393.1/2.html")


async def test_opt_in_content_proxy_uses_the_existing_route(monkeypatch):
    values = dict(settings_service.DEFAULTS)
    assert fanfox.source not in registry.enabled_direct_sources(values)
    values.update(
        source_fanfox_enabled="true",
        source_fanfox_proxy_enabled="true",
        download_proxy_url="http://proxy.example:8080",
    )
    settings_service.validate(values)

    async def get_all(_session):
        return values

    monkeypatch.setattr(settings_service, "get_all", get_all)
    await registry.apply_settings(None)
    assert fanfox.source in registry.enabled_direct_sources(values)
    route = routing.route_for("fanfox")
    assert not route.all_requests
    assert route.content_enabled
    assert route.content_url == values["download_proxy_url"]

@respx.mock
async def test_verification_requires_a_readable_image_and_caches_conclusive_results():
    locked_id = "yotsubato/v11/c072.5"
    locked = respx.get(f"{fanfox.BASE_URL}/manga/{locked_id}/1.html").respond(
        200, text="<p>Sorry, it’s licensed and not available.</p>",
    )
    public = respx.get(f"{fanfox.BASE_URL}{READER_PATH}1.html").respond(200, text=reader(1))
    image = respx.get(f"{CDN}1.jpg?token=sample&ttl=123").respond(206, content=b"\xff\xd8\xff" + bytes(29))
    source = fanfox.FanFoxSource()

    assert await source.verify_chapters([locked_id, CHAPTER]) == {CHAPTER}
    assert await source.verify_chapters([locked_id, CHAPTER], max_checks=0) == {CHAPTER}
    assert locked.call_count == public.call_count == image.call_count == 1
    headers = image.calls.last.request.headers
    assert headers["range"] == "bytes=0-31"
    assert headers["referer"] == f"{fanfox.BASE_URL}/"


@respx.mock
async def test_verification_hides_a_bad_cdn_response_and_rechecks_it():
    respx.get(f"{fanfox.BASE_URL}{READER_PATH}1.html").respond(200, text=reader(1))
    image = respx.get(f"{CDN}1.jpg?token=sample&ttl=123").mock(side_effect=[
        httpx.Response(200, text="upstream error"),
        httpx.Response(206, content=b"\xff\xd8\xff" + bytes(29)),
    ])
    source = fanfox.FanFoxSource()

    assert await source.verify_chapters([CHAPTER]) == set()
    assert await source.verify_chapters([CHAPTER]) == {CHAPTER}
    assert image.call_count == 2


@respx.mock
async def test_verification_hides_a_reader_without_a_complete_page_list():
    respx.get(f"{fanfox.BASE_URL}{READER_PATH}1.html").respond(200, text=reader(1, manifest=(1, 3)))

    assert await fanfox.FanFoxSource().verify_chapters([CHAPTER]) == set()


@respx.mock(assert_all_called=False)
async def test_verification_stops_the_batch_when_the_cdn_rate_limits(respx_mock):
    respx_mock.get(f"{fanfox.BASE_URL}{READER_PATH}1.html").respond(200, text=reader(1))
    respx_mock.get(f"{CDN}1.jpg?token=sample&ttl=123").respond(429)
    next_chapter = respx_mock.get(f"{fanfox.BASE_URL}/manga/kingdom/v36/c393/1.html").respond(404)

    assert await fanfox.FanFoxSource().verify_chapters([CHAPTER, "kingdom/v36/c393"]) == set()
    assert not next_chapter.called
