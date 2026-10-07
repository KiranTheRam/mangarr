"""A failed MangaDex login must not take anonymous access down with it:
search, feeds and at-home all work without an account, so the source falls
back to anonymous requests and leaves the auth server alone for a cooldown."""

import asyncio
import logging

import httpx
import pytest
import respx

from mangarr.sources import mangadex
from mangarr.sources.mangadex import API_URL, AUTH_URL, MangaDexSource
from mangarr.util import RateLimiter

BAD_LOGIN = {"error": "invalid_grant", "error_description": "Invalid user credentials"}
TOKEN = {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 900}


class FakeClock:
    """Stands in for the module's `time` so a test can step past cooldowns
    and token expiry without sleeping."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(mangadex, "time", fake)
    return fake


@pytest.fixture
def source(clock, monkeypatch):
    # the real 4 req/s pacing would only slow these tests down
    monkeypatch.setattr(mangadex, "_api_limiter", RateLimiter(rate=1000))
    src = MangaDexSource(client=httpx.AsyncClient())
    src.configure("client-id", "client-secret", "reader", "hunter2")
    return src


def _aggregate_route():
    return respx.get(url__startswith=f"{API_URL}/manga/").respond(
        json={"result": "ok", "volumes": {"1": {"volume": "1", "chapters": {"1": {}}}}}
    )


def _login_failures(caplog):
    return [r for r in caplog.records if "MangaDex login failed" in r.getMessage()]


@respx.mock
async def test_failed_login_falls_back_to_anonymous_requests(source, caplog):
    respx.post(AUTH_URL).respond(401, json=BAD_LOGIN)
    api = _aggregate_route()

    with caplog.at_level(logging.WARNING, logger="mangarr.sources.mangadex"):
        assert await source.get_volume_map("series-a") == {1.0: 1}

    assert "Authorization" not in api.calls.last.request.headers
    [record] = _login_failures(caplog)
    assert record.levelno == logging.WARNING
    assert "Invalid user credentials" in record.getMessage()
    assert "hunter2" not in caplog.text


@respx.mock
async def test_login_is_not_retried_until_the_cooldown_passes(source, clock, caplog):
    auth = respx.post(AUTH_URL).respond(401, json=BAD_LOGIN)
    _aggregate_route()

    with caplog.at_level(logging.WARNING, logger="mangarr.sources.mangadex"):
        await source.get_volume_map("a")
        await source.get_volume_map("b")
        clock.now += 14 * 60
        await source.get_volume_map("c")
        assert auth.call_count == 1
        assert len(_login_failures(caplog)) == 1  # one warning per cooldown, not per call

        clock.now += 2 * 60
        await source.get_volume_map("d")
        assert auth.call_count == 2
        assert len(_login_failures(caplog)) == 2


@respx.mock
async def test_unreachable_auth_server_falls_back_to_anonymous(source):
    auth = respx.post(AUTH_URL).mock(side_effect=httpx.ConnectError("auth down"))
    api = _aggregate_route()

    assert await source.get_volume_map("a") == {1.0: 1}
    await source.get_volume_map("b")

    assert auth.call_count == 1
    assert "Authorization" not in api.calls.last.request.headers


@respx.mock
async def test_concurrent_requests_share_one_login(source):
    async def slow_login(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=TOKEN)

    auth = respx.post(AUTH_URL).mock(side_effect=slow_login)
    api = _aggregate_route()

    await asyncio.gather(*(source.get_volume_map(f"s{i}") for i in range(5)))

    assert auth.call_count == 1
    assert all(c.request.headers["Authorization"] == "Bearer tok-1" for c in api.calls)


@respx.mock
async def test_concurrent_requests_share_one_failed_login(source):
    async def slow_failure(request):
        await asyncio.sleep(0.05)
        return httpx.Response(401, json=BAD_LOGIN)

    auth = respx.post(AUTH_URL).mock(side_effect=slow_failure)
    _aggregate_route()

    maps = await asyncio.gather(*(source.get_volume_map(f"s{i}") for i in range(5)))

    assert auth.call_count == 1
    assert all(m == {1.0: 1} for m in maps)


@respx.mock
async def test_new_credentials_skip_the_cooldown(source):
    auth = respx.post(AUTH_URL)
    auth.side_effect = [
        httpx.Response(401, json=BAD_LOGIN),
        httpx.Response(200, json=TOKEN),
    ]
    api = _aggregate_route()

    await source.get_volume_map("a")
    source.configure("client-id", "client-secret", "reader", "fixed-password")
    await source.get_volume_map("b")

    assert auth.call_count == 2
    assert auth.calls.last.request.content.decode().count("fixed-password") == 1
    assert api.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_expired_session_goes_anonymous_when_relogin_fails(source, clock):
    auth = respx.post(AUTH_URL)
    auth.side_effect = [
        httpx.Response(200, json=TOKEN),
        httpx.Response(400, json={"error": "invalid_grant"}),  # refresh rejected
        httpx.Response(401, json=BAD_LOGIN),  # password rejected
    ]
    api = _aggregate_route()

    await source.get_volume_map("a")
    assert api.calls.last.request.headers["Authorization"] == "Bearer tok-1"

    clock.now += 3600  # the access token has long expired
    await source.get_volume_map("b")

    assert auth.call_count == 3
    # a stale bearer would be refused; the request must go out anonymously
    assert "Authorization" not in api.calls.last.request.headers


@respx.mock
async def test_successful_login_is_reused_until_expiry(source, clock):
    auth = respx.post(AUTH_URL).respond(json=TOKEN)
    api = _aggregate_route()

    await source.get_volume_map("a")
    await source.get_volume_map("b")
    assert auth.call_count == 1
    assert "grant_type=password" in auth.calls.last.request.content.decode()

    clock.now += 900  # expired: renew with the refresh token
    await source.get_volume_map("c")
    assert auth.call_count == 2
    assert "grant_type=refresh_token" in auth.calls.last.request.content.decode()
    assert all(c.request.headers["Authorization"] == "Bearer tok-1" for c in api.calls)


@respx.mock
async def test_library_import_reports_the_failed_login(source):
    respx.post(AUTH_URL).respond(401, json=BAD_LOGIN)
    status = respx.get(f"{API_URL}/manga/status").respond(401)

    with pytest.raises(RuntimeError, match="MangaDex login failed .*Invalid user credentials"):
        await source.library_manga(["reading"])

    assert status.call_count == 0
