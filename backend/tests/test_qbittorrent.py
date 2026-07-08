import httpx
import pytest
import respx

from mangarr.download.qbittorrent import QbtClient
from mangarr.jobs.tasks import enqueue_torrent

BASE = "http://qbt:8080"


def client():
    c = QbtClient(BASE, "admin", "pw")
    c._logged_in = True  # skip the login round-trip
    return c


@respx.mock
async def test_default_save_path():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={"save_path": "/downloads"})
    assert await client().default_save_path() == "/downloads"


@respx.mock
async def test_default_save_path_missing_is_empty():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={})
    assert await client().default_save_path() == ""


@respx.mock
async def test_ensure_category_created():
    route = respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(200)
    await client().ensure_category("mangarr", "/downloads/mangarr")
    assert route.called
    sent = route.calls.last.request.content.decode()
    assert "category=mangarr" in sent and "savePath=%2Fdownloads%2Fmangarr" in sent


@respx.mock
async def test_ensure_category_conflict_edits():
    respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(409)
    edit = respx.post(f"{BASE}/api/v2/torrents/editCategory").respond(200)
    await client().ensure_category("mangarr", "/downloads/mangarr")
    assert edit.called  # already exists → path updated instead


@respx.mock
async def test_add_magnet_sends_category_and_savepath():
    route = respx.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Ok.")
    await client().add_magnet("magnet:?xt=urn:btih:abc", category="mangarr",
                              save_path="/downloads/mangarr")
    body = route.calls.last.request.content.decode()
    assert "category=mangarr" in body
    assert "savepath=%2Fdownloads%2Fmangarr" in body
    assert "autoTMM=false" in body


async def test_enqueue_torrent_rejects_magnet_without_btih():
    with pytest.raises(ValueError, match="btih"):
        await enqueue_torrent(None, None, "magnet:?dn=nohash", "bad", {})  # type: ignore[arg-type]
