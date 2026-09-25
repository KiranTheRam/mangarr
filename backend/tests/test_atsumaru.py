import httpx
import respx

from mangarr.sources.atsumaru import CDN_URL, SITE_URL, AtsumaruSource

SEARCH_URL = f"{SITE_URL}/collections/manga/documents/search"


@respx.mock
async def test_search_returns_comics_with_alt_titles_and_skips_novels():
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {"document": {"id": "RxJM9", "title": "Omniscient Reader", "medium": "Comic",
                                  "englishTitle": "Omniscient Reader's Viewpoint",
                                  "otherNames": ["전지적 독자 시점", "ORV", "Omniscient Reader"]}},
                    {"document": {"id": "xD_v", "title": "Omniscient Reader's Viewpoint", "medium": "Novel"}},
                    {"document": {"id": "gone", "title": "Hidden", "medium": "Comic", "hidden": True}},
                ]
            },
        )
    )
    source = AtsumaruSource()
    results = await source.search_series("omniscient reader")

    assert [(r.external_id, r.title) for r in results] == [("RxJM9", "Omniscient Reader")]
    assert results[0].alt_titles == ["Omniscient Reader's Viewpoint", "전지적 독자 시점", "ORV"]
    assert results[0].url == f"{SITE_URL}/manga/RxJM9"
    assert route.calls.last.request.url.params["q"] == "omniscient reader"
    await source._client.aclose()


async def test_search_empty_query_returns_nothing():
    source = AtsumaruSource()
    assert await source.search_series("  ") == []
    await source._client.aclose()


@respx.mock
async def test_list_chapters_takes_best_ranked_group_with_pages():
    respx.get(f"{SITE_URL}/api/manga/page").mock(
        return_value=httpx.Response(
            200,
            json={"mangaPage": {"scanlators": [
                {"id": "low", "name": "Delta", "score": 0},
                {"id": "top", "name": "Alpha", "score": 20},
                {"id": "mid", "name": "Flame", "score": 10},
            ]}},
        )
    )
    respx.get(f"{SITE_URL}/api/manga/info").mock(
        return_value=httpx.Response(
            200,
            json={"chapters": [
                {"id": "a1", "number": 1, "title": "Chapter 1", "pageCount": 20, "scanId": "low"},
                {"id": "b1", "number": 1, "title": "Chapter 1", "pageCount": 21, "scanId": "top"},
                # the preferred group's copy has no pages → fall back to "mid"
                {"id": "b2", "number": 2, "title": "Chapter 2", "pageCount": 0, "scanId": "top"},
                {"id": "c2", "number": 2, "title": "Chapter 2 - The Fall", "pageCount": 18, "scanId": "mid"},
                {"id": "d2", "number": 2, "title": "Chapter 2", "pageCount": 18, "scanId": "unknown"},
                {"id": "e3", "number": 10.5, "title": "Episode 10.5", "pageCount": 5, "scanId": "unknown"},
                {"id": "f4", "number": 11, "title": "# 11", "pageCount": 5, "scanId": "unknown"},
            ]},
        )
    )
    source = AtsumaruSource()
    chapters = await source.list_chapters("RxJM9")

    assert [(c.number, c.external_id, c.title) for c in chapters] == [
        (1.0, "RxJM9|b1", ""),
        (2.0, "RxJM9|c2", "Chapter 2 - The Fall"),
        (10.5, "RxJM9|e3", ""),
        (11.0, "RxJM9|f4", ""),
    ]
    assert chapters[0].url == f"{SITE_URL}/read/RxJM9/b1"
    await source._client.aclose()


@respx.mock
async def test_get_pages_orders_pages_and_resolves_cdn_urls():
    route = respx.get(f"{SITE_URL}/api/read/chapter").mock(
        return_value=httpx.Response(
            200,
            json={"readChapter": {"pages": [
                {"number": 1, "image": "/static/pages/x/1.avif"},
                {"number": 0, "image": "/static/pages/x/0.webp"},
                {"number": 2, "image": "https://cdn.example/2.avif"},
                {"number": 3},
            ]}},
        )
    )
    source = AtsumaruSource()
    pages = await source.get_pages("RxJM9|b1")

    assert pages == [
        f"{CDN_URL}/static/pages/x/0.webp",
        f"{CDN_URL}/static/pages/x/1.avif",
        "https://cdn.example/2.avif",
    ]
    assert dict(route.calls.last.request.url.params) == {"mangaId": "RxJM9", "chapterId": "b1"}
    await source._client.aclose()
