import time

import httpx
import pytest
import respx

from mangarr.util import RateLimiter, rl_request

URL = "https://example.test/x"


def client():
    return httpx.AsyncClient()


@respx.mock
async def test_success_passthrough():
    respx.get(URL).respond(200, text="ok")
    resp = await rl_request(client(), "GET", URL)
    assert resp.status_code == 200 and resp.text == "ok"


@respx.mock
async def test_backs_off_then_succeeds_on_429():
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, text="done"),
    ])
    resp = await rl_request(client(), "GET", URL, retries=3)
    assert resp.status_code == 200
    assert route.call_count == 3  # two 429s, then success


@respx.mock
async def test_honors_retry_after_seconds():
    respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, text="ok"),
    ])
    start = time.monotonic()
    resp = await rl_request(client(), "GET", URL, retries=2)
    assert resp.status_code == 200
    assert time.monotonic() - start >= 1.0  # actually waited the Retry-After


@respx.mock
async def test_gives_up_and_returns_last_429():
    respx.get(URL).respond(429, headers={"Retry-After": "0"})
    resp = await rl_request(client(), "GET", URL, retries=2)
    assert resp.status_code == 429  # caller decides what to do


@respx.mock
async def test_retries_transient_network_error():
    respx.get(URL).mock(side_effect=[
        httpx.ConnectError("boom"),
        httpx.Response(200, text="recovered"),
    ])
    resp = await rl_request(client(), "GET", URL, retries=2)
    assert resp.status_code == 200 and resp.text == "recovered"


@respx.mock
async def test_reraises_network_error_after_retries():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(httpx.ConnectError):
        await rl_request(client(), "GET", URL, retries=1)


@respx.mock
async def test_limiter_is_applied():
    respx.get(URL).respond(200)
    limiter = RateLimiter(rate=2, per_seconds=1)  # ~0.5s spacing
    start = time.monotonic()
    for _ in range(3):
        await rl_request(client(), "GET", URL, limiter=limiter)
    assert time.monotonic() - start >= 1.0  # 3 calls at 2/s => >=1s
