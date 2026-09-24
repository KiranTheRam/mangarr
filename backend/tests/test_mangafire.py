import httpx
import pytest
import respx

from mangarr.sources._mangafire_vrf import MangaFireSigner, MangaFireTokenError
from mangarr.sources.mangafire import (
    API_URL,
    BASE_URL,
    MangaFireSource,
    canonical_chapter_number,
)


class StubSigner:
    def __init__(self):
        self.signed = []
        self.invalidated = 0

    async def sign(self, client, path, params=None):
        self.signed.append((path, dict(params or {})))
        return f"vrf-{len(self.signed)}"

    def invalidate(self):
        self.invalidated += 1


def test_canonical_number_only_repairs_positional_official_entries():
    assert canonical_chapter_number(97.01, "Class 98: Graduation") == 98
    assert canonical_chapter_number(0.01, "Class 2: Introduction") == 2
    assert canonical_chapter_number(105, "Chapter 104-105") == 105
    assert canonical_chapter_number(60.5, "Chapter 60: Bonus") == 60.5


@respx.mock
async def test_search_series_uses_json_api():
    respx.get(f"{API_URL}/titles").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "hid": "z2ol",
                        "slug": "assassination-classroom",
                        "title": "Assassination Classroom",
                        "url": "/title/z2ol-assassination-classroom",
                    }
                ]
            },
        )
    )
    source = MangaFireSource(signer=StubSigner())
    results = await source.search_series("Assassination Classroom")

    assert [(item.external_id, item.title) for item in results] == [
        ("z2ol", "Assassination Classroom")
    ]
    assert results[0].url.endswith("/title/z2ol-assassination-classroom")
    await source._client.aclose()


@respx.mock
async def test_list_chapters_keeps_decimals_and_prefers_first_edition():
    route = respx.get(f"{API_URL}/titles/z2ol/chapters")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "items": [
                    {"id": 10, "number": 15.5, "name": "(1r0n)", "language": "en"},
                    {"id": 99, "number": 15.5, "name": "Spanish", "language": "es"},
                    {"id": 19, "number": 0.01, "name": "Omake", "language": "en"},
                    {"id": 20, "number": 0.01, "name": "Class 2: Real title", "language": "en"},
                ],
                "meta": {"hasNext": True},
            },
        ),
        httpx.Response(
            200,
            json={
                "items": [
                    {"id": 11, "number": 15.5, "name": "Duplicate", "language": "en"},
                    {"id": 12, "number": 16.5, "name": "Crossover", "language": "en"},
                ],
                "meta": {"hasNext": False},
            },
        ),
    ]
    source = MangaFireSource(signer=StubSigner())
    chapters = await source.list_chapters("z2ol")

    assert [(chapter.number, chapter.external_id) for chapter in chapters] == [
        (0.01, "19"),
        (2.0, "20"),
        (15.5, "10"),
        (16.5, "12"),
    ]
    assert chapters[1].title == "Class 2: Real title"
    assert chapters[2].title == ""
    assert chapters[3].title == "Crossover"
    assert route.call_count == 2
    await source._client.aclose()


@respx.mock
async def test_get_pages_reads_reader_payload():
    respx.get(f"{API_URL}/chapters/10").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"pages": [{"url": "https://cdn/1.jpg"}, {"url": "https://cdn/2.jpg"}]}},
        )
    )
    source = MangaFireSource(signer=StubSigner())
    assert await source.get_pages("10") == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    await source._client.aclose()


@respx.mock
async def test_api_calls_carry_a_vrf_token():
    route = respx.get(f"{API_URL}/titles").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    signer = StubSigner()
    source = MangaFireSource(signer=signer)
    await source.search_series("Berserk")

    assert signer.signed == [("/titles", {"keyword": "Berserk"})]
    assert route.calls.last.request.url.params["vrf"] == "vrf-1"
    assert route.calls.last.request.url.params["keyword"] == "Berserk"
    await source._client.aclose()


@respx.mock
async def test_rejected_token_reloads_signer_and_retries_once():
    route = respx.get(f"{API_URL}/chapters/10")
    route.side_effect = [
        httpx.Response(403, json={"message": "Missing token."}),
        httpx.Response(200, json={"data": {"pages": [{"url": "https://cdn/1.jpg"}]}}),
    ]
    signer = StubSigner()
    source = MangaFireSource(signer=signer)

    assert await source.get_pages("10") == ["https://cdn/1.jpg"]
    assert signer.invalidated == 1
    assert [call.request.url.params["vrf"] for call in route.calls] == ["vrf-1", "vrf-2"]
    await source._client.aclose()


@respx.mock
async def test_other_403s_are_not_retried():
    route = respx.get(f"{API_URL}/chapters/10").mock(
        return_value=httpx.Response(403, text="<html>Cloudflare</html>")
    )
    signer = StubSigner()
    source = MangaFireSource(signer=signer)

    with pytest.raises(httpx.HTTPStatusError):
        await source.get_pages("10")
    assert route.call_count == 1 and signer.invalidated == 0
    await source._client.aclose()


# A stand-in for the site's signing module: it depends on the same browser
# globals the real one does and is written as an ES module chunk.
FAKE_SIGNING_MODULE = """
var calls = 0;
globalThis.getProtectionToken = function (path, params) {
  calls++;
  var query = Object.keys(params).sort().map(function (k) { return k + '=' + params[k]; });
  var bytes = new TextEncoder().encode(path + '?' + query.join('&'));
  return navigator.appCodeName + ':' + btoa(String.fromCharCode.apply(null, bytes));
};
export{calls as t};
"""


def _mock_frontend():
    respx.get(f"{BASE_URL}/").mock(
        return_value=httpx.Response(
            200,
            text='<html><script type="module" src="https://cdn.test/build/main-abc.js">'
            "</script></html>",
        )
    )
    respx.get("https://cdn.test/build/main-abc.js").mock(
        return_value=httpx.Response(
            200, text='import{a as r}from"./rolldown-runtime.js";import{t}from"./polyfill-x.js";'
        )
    )
    respx.get("https://cdn.test/build/rolldown-runtime.js").mock(
        return_value=httpx.Response(200, text="var e=1;export{e as a};")
    )
    return respx.get("https://cdn.test/build/polyfill-x.js").mock(
        return_value=httpx.Response(200, text=FAKE_SIGNING_MODULE)
    )


@respx.mock
async def test_signer_finds_and_runs_the_sites_signing_module():
    module_route = _mock_frontend()
    signer = MangaFireSigner(BASE_URL)
    async with httpx.AsyncClient() as client:
        token = await signer.sign(client, "/titles", {"page": 2, "keyword": "ワンピース"})
        again = await signer.sign(client, "/chapters/1", {})

    assert token == "Mozilla:L3RpdGxlcz9rZXl3b3JkPeODr+ODs+ODlOODvOOCuSZwYWdlPTI="
    assert again == "Mozilla:L2NoYXB0ZXJzLzE/"
    # the loaded module is cached between calls
    assert module_route.call_count == 1


@respx.mock
async def test_signer_reports_a_missing_signing_module():
    respx.get(f"{BASE_URL}/").mock(
        return_value=httpx.Response(200, text='<script src="/main.js"></script>')
    )
    respx.get(f"{BASE_URL}/main.js").mock(return_value=httpx.Response(200, text="var a=1;"))
    signer = MangaFireSigner(BASE_URL)
    async with httpx.AsyncClient() as client:
        with pytest.raises(MangaFireTokenError, match="getProtectionToken"):
            await signer.sign(client, "/titles", {"keyword": "x"})
