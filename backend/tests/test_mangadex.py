import httpx
import pytest
import respx

from mangarr.sources.mangadex import API_URL, MangaDexSource

AGGREGATE = {
    "result": "ok",
    "volumes": {
        "1": {"volume": "1", "chapters": {"1": {}, "2": {}, "5.5": {}}},
        "10": {"volume": "10", "chapters": {"77": {}, "85.5": {}}},
        "none": {"volume": "none", "chapters": {"239": {}}},
    },
}


@pytest.fixture
def source():
    return MangaDexSource(client=httpx.AsyncClient())


@respx.mock
async def test_get_volume_map(source):
    respx.get(f"{API_URL}/manga/abc/aggregate").respond(json=AGGREGATE)
    mapping = await source.get_volume_map("abc")
    assert mapping == {1.0: 1, 2.0: 1, 5.5: 1, 77.0: 10, 85.5: 10}


@respx.mock
async def test_get_volume_map_empty_list_response(source):
    # MangaDex returns "volumes": [] when a title has no chapters at all
    respx.get(f"{API_URL}/manga/abc/aggregate").respond(json={"result": "ok", "volumes": []})
    assert await source.get_volume_map("abc") == {}
