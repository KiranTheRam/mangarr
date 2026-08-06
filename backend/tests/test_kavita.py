import asyncio
import json

import pytest
import respx
from httpx import Response

from mangarr import kavita
from mangarr.kavita import KavitaError, KavitaLibrary, ScanRequest

BASE = "http://kavita.test"

AUTH_BODY = {"token": "jwt-token", "kavitaVersion": "0.9.0.2", "username": "u"}

LIBRARIES_BODY = [
    {"id": 3, "name": "Manga", "folders": ["/data/manga"]},
    {"id": 5, "name": "DC Comics", "folders": ["/data/comics/DC"]},
]

ENABLED = {
    "kavita_enabled": "true",
    "kavita_url": BASE,
    "kavita_api_key": "key",
    "kavita_scan_mode": "series",
    "kavita_library_map": "",
}


@pytest.fixture(autouse=True)
def _reset_pending():
    kavita._pending.clear()
    kavita._flush_task = None
    yield
    kavita._pending.clear()
    kavita._flush_task = None


def mock_auth():
    return respx.post(f"{BASE}/api/Plugin/authenticate").mock(
        return_value=Response(200, json=AUTH_BODY)
    )


def mock_libraries():
    return respx.get(f"{BASE}/api/Library/libraries").mock(
        return_value=Response(200, json=LIBRARIES_BODY)
    )


# ------------------------------------------------------------------- client

@respx.mock
async def test_authenticate_exchanges_api_key_for_bearer_token():
    auth = mock_auth()
    libs = mock_libraries()
    version, libraries = await kavita.test_connection(BASE, "sekrit")
    assert version == "0.9.0.2"
    assert [lib.name for lib in libraries] == ["Manga", "DC Comics"]
    assert auth.calls[0].request.url.params["apiKey"] == "sekrit"
    assert auth.calls[0].request.url.params["pluginName"] == "mangarr"
    assert libs.calls[0].request.headers["authorization"] == "Bearer jwt-token"


@respx.mock
async def test_bad_api_key_raises():
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(return_value=Response(401))
    with pytest.raises(KavitaError, match="rejected the API key"):
        await kavita.test_connection(BASE, "nope")


@respx.mock
async def test_non_kavita_host_raises_readable_error():
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(
        return_value=Response(200, text="<html>hello</html>")
    )
    with pytest.raises(KavitaError, match="did not return a Kavita API response"):
        await kavita.test_connection(BASE, "key")


@respx.mock
async def test_scan_endpoints_send_kavita_payloads():
    mock_auth()
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    client = kavita.KavitaClient(BASE, "key")
    try:
        await client.scan_library(3)
        await client.scan_series(3, 105)
    finally:
        await client.close()
    assert lib_scan.calls[0].request.url.params["libraryId"] == "3"
    assert json.loads(series_scan.calls[0].request.content) == {
        "libraryId": 3, "seriesId": 105, "forceUpdate": False,
    }


@respx.mock
async def test_find_series_id_matches_loosely_but_rejects_other_titles():
    mock_auth()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [
            {"seriesId": 9, "name": "Spy x Family Bonus", "libraryId": 3},
            {"seriesId": 7, "name": "Spy × Family", "libraryId": 3},
        ]
    }))
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert await client.find_series_id(3, ["Spy x Family"]) == 7
        assert await client.find_series_id(3, ["Chainsaw Man"]) is None
    finally:
        await client.close()


@respx.mock
async def test_find_series_id_ignores_other_libraries():
    mock_auth()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [{"seriesId": 9, "name": "Berserk", "libraryId": 5}]
    }))
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert await client.find_series_id(3, ["Berserk"]) is None
    finally:
        await client.close()


@respx.mock
async def test_expired_token_is_refreshed_once():
    auth = mock_auth()
    route = respx.get(f"{BASE}/api/Library/libraries").mock(
        side_effect=[Response(401), Response(200, json=LIBRARIES_BODY)]
    )
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert len(await client.libraries()) == 2
    finally:
        await client.close()
    assert len(auth.calls) == 2
    assert route.call_count == 2


# ----------------------------------------------------------- library mapping

def test_match_library_matches_on_shared_path_tail():
    libs = [
        KavitaLibrary(3, "Manga", ["/data/manga"]),
        KavitaLibrary(5, "Comics", ["/data/comics/DC"]),
    ]
    # identical mount
    assert kavita.match_library(libs, "/data/manga").id == 3
    # same share, different mount point in each container
    assert kavita.match_library(libs, "/mnt/media/manga").id == 3
    assert kavita.match_library(libs, "/srv/comics/DC").id == 5


def test_match_library_rejects_a_differently_named_folder():
    libs = [KavitaLibrary(3, "Manga", ["/data/manga"])]
    # "data" is shared, but the library folders themselves are unrelated
    assert kavita.match_library(libs, "/data/books") is None
    assert kavita.match_library(libs, "") is None


def test_match_library_prefers_the_longest_tail():
    libs = [
        KavitaLibrary(1, "Broad", ["/media/manga"]),
        KavitaLibrary(2, "Exact", ["/tank/library/manga"]),
    ]
    assert kavita.match_library(libs, "/tank/library/manga").id == 2


def test_parse_library_map_survives_garbage():
    assert kavita.parse_library_map('{"1": 3, "2": 5}') == {1: 3, 2: 5}
    assert kavita.parse_library_map("") == {}
    assert kavita.parse_library_map("not json") == {}
    assert kavita.parse_library_map("[1,2]") == {}
    assert kavita.parse_library_map('{"1": "x", "y": 2, "3": 4}') == {3: 4}


@respx.mock
async def test_explicit_mapping_wins_over_path_match():
    mock_auth()
    mock_libraries()
    client = kavita.KavitaClient(BASE, "key")
    values = {**ENABLED, "kavita_library_map": '{"1": 5}'}
    try:
        # path says Manga (3), the explicit map says DC Comics (5)
        assert await kavita.resolve_library_id(client, values, 1, "/data/manga") == 5
        # a root folder with no override still falls back to the path match
        assert await kavita.resolve_library_id(client, values, 2, "/data/manga") == 3
        # nothing matches: refuse rather than scan an unrelated library
        assert await kavita.resolve_library_id(client, values, 2, "/elsewhere") is None
    finally:
        await client.close()


# ------------------------------------------------------------------ scanning

@respx.mock
async def test_known_series_gets_a_partial_scan():
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [{"seriesId": 105, "name": "One-Punch Man", "libraryId": 3}]
    }))
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {ScanRequest(1, "/data/manga", ("One-Punch Man",))})
    assert series_scan.called
    assert not lib_scan.called


@respx.mock
async def test_series_kavita_has_never_seen_falls_back_to_a_library_scan():
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {ScanRequest(1, "/data/manga", ("Brand New Series",))})
    assert not series_scan.called
    assert lib_scan.calls[0].request.url.params["libraryId"] == "3"


@respx.mock
async def test_library_mode_never_searches_or_scans_per_series():
    mock_auth()
    mock_libraries()
    search = respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={}))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(
        {**ENABLED, "kavita_scan_mode": "library"},
        {ScanRequest(1, "/data/manga", ("One-Punch Man",))},
    )
    assert not search.called
    assert lib_scan.called


@respx.mock
async def test_a_library_scan_subsumes_series_scans_in_that_library():
    mock_auth()
    mock_libraries()
    # "Known" resolves; "New" does not, forcing a scan of the whole library
    def search(request):
        query = request.url.params["queryString"]
        return Response(200, json={
            "series": [{"seriesId": 105, "name": "Known", "libraryId": 3}]
            if query == "Known" else []
        })

    respx.get(f"{BASE}/api/Search/search").mock(side_effect=search)
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {
        ScanRequest(1, "/data/manga", ("Known",)),
        ScanRequest(1, "/data/manga", ("New",)),
    })
    assert lib_scan.call_count == 1
    assert not series_scan.called


@respx.mock
async def test_unmappable_root_folder_scans_nothing():
    mock_auth()
    mock_libraries()
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {ScanRequest(9, "/nowhere/at/all", ("X",))})
    assert not lib_scan.called


# ------------------------------------------------------------------ notify

@respx.mock
async def test_notify_import_debounces_a_burst_into_one_scan(monkeypatch):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    for _ in range(20):
        kavita.notify_import(ENABLED, 1, "/data/manga", ["One-Punch Man"])
    await asyncio.sleep(0.3)
    assert lib_scan.call_count == 1


async def test_notify_import_disabled_schedules_nothing():
    # would raise on an unmocked request if it tried to reach the network
    kavita.notify_import({**ENABLED, "kavita_enabled": "false"}, 1, "/data/manga", ["X"])
    kavita.notify_import({**ENABLED, "kavita_url": ""}, 1, "/data/manga", ["X"])
    kavita.notify_import({**ENABLED, "kavita_api_key": ""}, 1, "/data/manga", ["X"])
    kavita.notify_import(ENABLED, 1, "", ["X"])
    await asyncio.sleep(0.01)
    assert not kavita._pending


@respx.mock
async def test_unreachable_kavita_never_raises_into_the_import_path(monkeypatch, caplog):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(return_value=Response(500))
    kavita.notify_import(ENABLED, 1, "/data/manga", ["One-Punch Man"])
    await asyncio.sleep(0.3)
    assert "Kavita scan request failed" in caplog.text


# ---------------------------------------------------------------- validation

def test_validate_settings_requires_a_usable_connection_when_enabled():
    kavita.validate_settings({"kavita_enabled": "false", "kavita_url": ""})
    kavita.validate_settings(ENABLED)
    with pytest.raises(ValueError, match="kavita_url is required"):
        kavita.validate_settings({**ENABLED, "kavita_url": ""})
    with pytest.raises(ValueError, match="kavita_api_key is required"):
        kavita.validate_settings({**ENABLED, "kavita_api_key": ""})
    with pytest.raises(ValueError, match="valid http"):
        kavita.validate_settings({**ENABLED, "kavita_url": "kavita:5000"})
    with pytest.raises(ValueError, match="kavita_scan_mode"):
        kavita.validate_settings({**ENABLED, "kavita_scan_mode": "everything"})
    with pytest.raises(ValueError, match="valid JSON"):
        kavita.validate_settings({**ENABLED, "kavita_library_map": "{oops"})
    with pytest.raises(ValueError, match="JSON object"):
        kavita.validate_settings({**ENABLED, "kavita_library_map": "[1]"})


# ------------------------------------------------- wiring into the import path

@respx.mock
async def test_import_path_notifies_kavita_with_every_known_title(monkeypatch):
    """tasks._notify_kavita is what downloads actually call: it must hand over
    the series' root folder and all the names Kavita might have parsed."""
    from mangarr.jobs import tasks
    from mangarr.models import RootFolder, Series

    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    search = respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))

    series = Series(
        id=1, title="Spy x Family", folder_name="Spy x Family (2019)",
        alt_titles="SPY×FAMILY\nスパイファミリー", root_folder_id=1,
    )
    series.root_folder = RootFolder(id=1, path="/library/manga")
    tasks._notify_kavita(ENABLED, series)
    await asyncio.sleep(0.3)

    queried = {call.request.url.params["queryString"] for call in search.calls}
    assert "Spy x Family" in queried
    assert "Spy x Family (2019)" in queried
    assert "SPY×FAMILY" in queried
    # /library/manga matched Kavita's /data/manga on the folder name
    assert lib_scan.calls[0].request.url.params["libraryId"] == "3"


async def test_series_without_a_root_folder_notifies_nothing():
    from mangarr.jobs import tasks
    from mangarr.models import Series

    # would hit the unmocked network if it scheduled anything
    tasks._notify_kavita(ENABLED, Series(id=1, title="X", folder_name="X", alt_titles=""))
    await asyncio.sleep(0.01)
    assert not kavita._pending


@respx.mock
async def test_an_import_arriving_during_a_scan_is_not_dropped(monkeypatch):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    scanning = asyncio.Event()
    release = asyncio.Event()

    async def slow_scan(request):
        scanning.set()
        await release.wait()
        return Response(200)

    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(side_effect=slow_scan)

    kavita.notify_import(ENABLED, 1, "/data/manga", ["First"])
    await asyncio.wait_for(scanning.wait(), 1)
    # a second import lands mid-scan, so the timer that would have batched it
    # has already fired and drained the pending set
    kavita.notify_import(ENABLED, 2, "/data/comics/DC", ["Second"])
    release.set()
    await asyncio.sleep(0.4)
    assert {call.request.url.params["libraryId"] for call in lib_scan.calls} == {"3", "5"}
