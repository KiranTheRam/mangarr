import httpx
import respx

from mangarr.sources.mangafire import API_URL, MangaFireSource


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
    source = MangaFireSource()
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
    source = MangaFireSource()
    chapters = await source.list_chapters("z2ol")

    assert [(chapter.number, chapter.external_id) for chapter in chapters] == [
        (2.0, "20"),
        (15.5, "10"),
        (16.5, "12"),
    ]
    assert chapters[0].title == "Class 2: Real title"
    assert chapters[1].title == ""
    assert chapters[2].title == "Crossover"
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
    source = MangaFireSource()
    assert await source.get_pages("10") == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    await source._client.aclose()
