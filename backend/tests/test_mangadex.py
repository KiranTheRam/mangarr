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


@respx.mock
async def test_get_volume_map_is_cached(source):
    route = respx.get(f"{API_URL}/manga/abc/aggregate").respond(json=AGGREGATE)
    first = await source.get_volume_map("abc")
    second = await source.get_volume_map("abc")
    assert first == second
    assert route.call_count == 1  # update_chapters asks every cycle; don't refetch
    second[999.0] = 1  # callers get copies — mutating one must not poison the cache
    assert await source.get_volume_map("abc") == first


def test_configure_keeps_tokens_when_credentials_unchanged(source):
    """configure() runs on every settings read; wiping the session each time
    would force a fresh password grant for every API call."""
    source.configure("id", "secret", "user", "pass")
    source._access_token = "live-token"
    source._refresh_token = "refresh"
    source._token_expires_at = 9e9

    source.configure("id", "secret", "user", "pass", language="en")

    assert source._access_token == "live-token"
    assert source._refresh_token == "refresh"


def test_configure_resets_tokens_when_credentials_change(source):
    source.configure("id", "secret", "user", "pass")
    source._access_token = "live-token"

    source.configure("id", "secret", "user", "new-pass")

    assert source._access_token is None
    assert source._token_expires_at == 0.0
