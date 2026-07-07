import httpx
import pytest
import respx

from mangarr.metadata.mangaupdates import (
    API_URL,
    MangaUpdatesProvider,
    parse_number_token,
    parse_status,
    parse_total_volumes,
)

SERIES_RECORD = {
    "series_id": 66296374554,
    "title": "Sousou no Frieren",
    "url": "https://www.mangaupdates.com/series/ugf5dzu/sousou-no-frieren",
    "associated": [{"title": "Frieren"}, {"title": "Frieren: Beyond Journey's End"}],
    "description": "From [Viz](https://viz.com): a journey.",
    "image": {"url": {"original": "https://cdn.mangaupdates.com/image/i525134.jpg"}},
    "type": "Manga",
    "year": "2020",
    "genres": [{"genre": "Adventure"}, {"genre": "Fantasy"}],
    "latest_chapter": 147,
    "status": "15 Volumes (Hiatus)",
    "completed": False,
}


def release(chapter, volume=None, date="2025-10-15"):
    return {"record": {"chapter": chapter, "volume": volume, "release_date": date}}


@pytest.fixture
def provider():
    return MangaUpdatesProvider(client=httpx.AsyncClient())


class TestParsers:
    def test_status_words(self):
        assert parse_status("15 Volumes (Hiatus)", False) == "hiatus"
        assert parse_status("103 Volumes (Ongoing)", False) == "releasing"
        assert parse_status("Complete", False) == "finished"
        assert parse_status("Ongoing", True) == "finished"  # completed flag wins
        assert parse_status(None, None) == "unknown"

    def test_total_volumes(self):
        assert parse_total_volumes("15 Volumes (Hiatus)") == 15
        assert parse_total_volumes("1 Volume (Complete)") == 1
        assert parse_total_volumes("Ongoing") is None

    def test_number_tokens(self):
        assert parse_number_token("147") == [147.0]
        assert parse_number_token("12.5") == [12.5]
        assert parse_number_token("365-366") == [365.0, 366.0]
        assert parse_number_token("1-3") == [1.0, 2.0, 3.0]
        assert parse_number_token("10.5-11.5") == [10.5, 11.5]  # decimal endpoints only
        assert parse_number_token("1-999") == []  # absurd span
        assert parse_number_token("Oneshot") == []
        assert parse_number_token(None) == []


@respx.mock
async def test_get_series(provider):
    respx.get(f"{API_URL}/series/66296374554").respond(json=SERIES_RECORD)
    meta = await provider.get_series("66296374554")
    assert meta.provider == "mangaupdates"
    assert meta.title == "Sousou no Frieren"
    assert "Frieren: Beyond Journey's End" in meta.alt_titles
    assert meta.status == "hiatus"
    assert meta.year == 2020
    assert meta.total_chapters == 147
    assert meta.total_volumes == 15
    assert meta.cover_url.endswith("i525134.jpg")
    assert meta.genres == ["Adventure", "Fantasy"]
    assert "[Viz]" not in meta.description  # markdown links stripped to text
    assert "Viz" in meta.description


@respx.mock
async def test_get_series_404(provider):
    respx.get(f"{API_URL}/series/999").respond(status_code=404)
    assert await provider.get_series("999") is None


@respx.mock
async def test_search_includes_hit_title(provider):
    respx.post(f"{API_URL}/series/search").respond(
        json={
            "total_hits": 1,
            "results": [{"record": SERIES_RECORD, "hit_title": "Frieren at the Funeral"}],
        }
    )
    results = await provider.search("frieren")
    assert len(results) == 1
    assert "Frieren at the Funeral" in results[0].alt_titles


@respx.mock
async def test_get_release_data(provider):
    respx.post(f"{API_URL}/releases/search").respond(
        json={
            "total_hits": 3,
            "results": [
                release("147"),
                release("145-146", date="2025-10-01"),
                release("140", volume="15", date="2025-01-01"),
            ],
        }
    )
    data = await provider.get_release_data(66296374554)
    assert set(data.chapters) == {147.0, 145.0, 146.0, 140.0}
    assert data.chapters[140.0].year == 2025
    assert data.volume_anchors == {140.0: 15}


@respx.mock
async def test_get_release_data_cached(provider):
    route = respx.post(f"{API_URL}/releases/search").respond(
        json={"total_hits": 1, "results": [release("1")]}
    )
    first = await provider.get_release_data(42)
    second = await provider.get_release_data(42)
    assert first is second
    assert route.call_count == 1


@respx.mock
async def test_get_release_data_paginates(provider):
    page1 = {"total_hits": 41, "results": [release(str(n)) for n in range(1, 41)]}
    page2 = {"total_hits": 41, "results": [release("41")]}
    route = respx.post(f"{API_URL}/releases/search")
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]
    data = await provider.get_release_data(7)
    assert route.call_count == 2
    assert len(data.chapters) == 41
