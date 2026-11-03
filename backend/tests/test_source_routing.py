"""Routing contracts use a real local HTTP proxy, without upstream traffic."""

import asyncio
import json
import traceback
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.api import settings as settings_api
from mangarr.download import direct
from mangarr.models import Base
from mangarr.util import is_transient_error
from mangarr.sources import mangafire, registry, routing, weebcentral
from mangarr.sources._mangafire_vrf import MangaFireTokenError


@pytest.fixture(autouse=True)
def clean_routes():
    token = routing._routes.set({})
    yield
    routing._routes.reset(token)


@asynccontextmanager
async def http_server():
    calls = []
    async def respond(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        method, target, _ = request.splitlines()[0].split()
        calls.append((target, request))
        path = urlsplit(target).path
        if path == "/":
            body = b'<script src="/main.js"></script>'
        elif path == "/main.js":
            body = b'import{t}from"./token.js";'
        elif path == "/token.js":
            body = b'globalThis.getProtectionToken = function(){return "test-token";};'
        elif path == "/api/titles":
            body = json.dumps({"items": [{"hid": "series1", "title": "One Piece"}]}).encode()
        elif path == "/api/titles/series1/chapters":
            body = json.dumps({"items": [{"id": 1, "number": 1, "language": "en"}]}).encode()
        elif path == "/api/chapters/1":
            body = b'{"data":{"pages":[{"url":"http://images.invalid/page.jpg"}]}}'
        elif path == "/search/data":
            body = b'<a href="/series/SERIES1"><img alt="One Piece cover"></a>'
        elif path == "/series/SERIES1/full-chapter-list":
            body = b'<a href="/chapters/CHAPTER1"><span>Chapter 1</span></a>'
        elif path == "/chapters/CHAPTER1/images":
            body = b'<img src="http://images.invalid/page.jpg">'
        elif path == "/page.jpg":
            body = b"\xff\xd8\xff\xe0test-image"
        else:
            body = b"unexpected request"
        writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", calls


@respx.mock
async def test_weebcentral_browser_compatible_headers():
    route = respx.get(f"{weebcentral.BASE_URL}/search/data").respond(200, text="")
    await weebcentral.WeebCentralSource().search_series("Naruto")
    headers = route.calls.last.request.headers
    assert headers["User-Agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    assert headers["Accept"] == "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    assert headers["Accept-Language"] == "en-US,en;q=0.9"
    assert headers["Referer"] == f"{weebcentral.BASE_URL}/"


@pytest.mark.parametrize("name", routing.SOURCE_PROXY_NAMES)
async def test_all_requests_including_signer_manifests_and_images_use_one_proxy(name, monkeypatch, tmp_path):
    # Origins deliberately do not resolve. Only the local proxy can answer.
    monkeypatch.setattr(mangafire, "BASE_URL", "http://mangafire.invalid")
    monkeypatch.setattr(mangafire, "API_URL", "http://mangafire.invalid/api")
    monkeypatch.setattr(weebcentral, "BASE_URL", "http://weebcentral.invalid")
    source = mangafire.MangaFireSource() if name == "mangafire" else weebcentral.WeebCentralSource()
    async with http_server() as (proxy, calls):
        routing.set_routes({name: routing.SourceRoute(True, proxy, True, proxy)})
        results = await source.search_series("One Piece")
        chapters = await source.list_chapters(results[0].external_id)
        pages = await source.get_pages(chapters[0].external_id)
        assert pages == ["http://images.invalid/page.jpg"]
        await direct.download_chapter_to_cbz(
            source, chapters[0].external_id,
            SimpleNamespace(title="One Piece", description=""),
            SimpleNamespace(number=1, volume=1, title=""), tmp_path / "test.cbz",
        )
        assert (tmp_path / "test.cbz").exists()
    assert all(target.startswith("http://") for target, _ in calls)
    assert any("images.invalid/page.jpg" in target for target, _ in calls)
    if name == "mangafire":
        assert sum(urlsplit(target).path == "/" for target, _ in calls) == 1
        assert any("/main.js" in target for target, _ in calls)
        assert any("/token.js" in target for target, _ in calls)
        api_calls = [target for target, _ in calls if "/api/" in target]
        assert len(api_calls) == 4 and all("vrf=test-token" in target for target in api_calls)


@pytest.mark.parametrize("content_only", [False, True])
async def test_legacy_and_default_source_requests_are_direct_even_with_environment_proxy(monkeypatch, content_only):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    async with http_server() as (origin, calls):
        monkeypatch.setattr(weebcentral, "BASE_URL", origin)
        routing.set_routes({"weebcentral": routing.SourceRoute(content_enabled=content_only, content_url="http://127.0.0.1:1")})
        await weebcentral.WeebCentralSource().search_series("One Piece")
    assert len(calls) == 1
    assert calls[0][0].startswith("/search/data")  # origin-form, not proxy-form


@pytest.mark.parametrize("name", routing.SOURCE_PROXY_NAMES)
@pytest.mark.parametrize("proxy", ["", "http://user:private-secret@127.0.0.1:1"])
async def test_missing_or_unreachable_proxy_never_falls_back(name, proxy, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("mangarr.util.asyncio.sleep", no_wait)
    async with http_server() as (origin, calls):
        monkeypatch.setattr(weebcentral, "BASE_URL", origin)
        monkeypatch.setattr(mangafire, "BASE_URL", origin)
        monkeypatch.setattr(mangafire, "API_URL", origin + "/api")
        routing.set_routes({name: routing.SourceRoute(True, proxy, True, proxy)})
        # Even an injected direct client must not bypass routing.
        async with httpx.AsyncClient() as injected:
            source = mangafire.MangaFireSource(client=injected) if name == "mangafire" else weebcentral.WeebCentralSource(client=injected)
            with pytest.raises((RuntimeError, httpx.ProxyError, MangaFireTokenError)) as error:
                await source.search_series("One Piece")
        assert not calls
        rendered = "".join(traceback.format_exception(error.value))
        assert "private-secret" not in rendered
        assert "user:" not in rendered


async def test_route_change_refreshes_signer_without_stale_connections(monkeypatch):
    monkeypatch.setattr(mangafire, "BASE_URL", "http://mangafire.invalid")
    monkeypatch.setattr(mangafire, "API_URL", "http://mangafire.invalid/api")
    source = mangafire.MangaFireSource()
    async with http_server() as (first, first_calls), http_server() as (second, second_calls):
        for proxy in (first, second, first):
            routing.set_routes({source.name: routing.SourceRoute(True, proxy, True, proxy)})
            await source.search_series("One Piece")
    assert sum(urlsplit(t).path == "/" for t, _ in first_calls) == 2
    assert sum(urlsplit(t).path == "/" for t, _ in second_calls) == 1


@pytest.mark.parametrize("name", routing.SOURCE_PROXY_NAMES)
async def test_concurrent_settings_and_downloads_keep_their_own_routes(name, monkeypatch, tmp_path):
    monkeypatch.setattr(mangafire, "BASE_URL", "http://mangafire.invalid")
    monkeypatch.setattr(mangafire, "API_URL", "http://mangafire.invalid/api")
    monkeypatch.setattr(weebcentral, "BASE_URL", "http://weebcentral.invalid")
    source = mangafire.MangaFireSource() if name == "mangafire" else weebcentral.WeebCentralSource()
    manifest_started, settings_changed = asyncio.Event(), asyncio.Event()
    async def settings_for_task(proxy):
        values = dict(settings_service.DEFAULTS)
        values.update({f"source_{name}_proxy_mode": "all", f"source_{name}_proxy_url": proxy})
        return values
    monkeypatch.setattr(settings_service, "get_all", settings_for_task)
    real_get_pages = source.get_pages
    async def paused_manifest(chapter):
        manifest_started.set()
        await settings_changed.wait()
        return await real_get_pages(chapter)
    monkeypatch.setattr(source, "get_pages", paused_manifest)
    async with http_server() as (first, first_calls), http_server() as (second, second_calls):
        async def old_download():
            await registry.apply_settings(first)
            await direct.download_chapter_to_cbz(
                source, "1" if name == "mangafire" else "CHAPTER1",
                SimpleNamespace(title="Series", description=""),
                SimpleNamespace(number=1, volume=1, title=""), tmp_path / "old.cbz",
            )
        async def new_request():
            await manifest_started.wait()
            await registry.apply_settings(second)
            settings_changed.set()
            await source.search_series("One Piece")
        await asyncio.gather(old_download(), new_request())
    assert any("page.jpg" in t for t, _ in first_calls)
    assert not any("page.jpg" in t for t, _ in second_calls)
    assert len(first_calls) == (5 if name == "mangafire" else 2)
    assert len(second_calls) == (4 if name == "mangafire" else 1)


async def test_registry_preserves_disabled_source_requirements_and_legacy_routes(monkeypatch):
    values = dict(settings_service.DEFAULTS)
    values.update(source_mangafire_enabled="false", source_mangafire_proxy_mode="all",
                  source_mangafire_proxy_url="", source_weebcentral_proxy_enabled="true",
                  download_proxy_url="http://shared.example:8080")
    async def get_all(_):
        return dict(values)
    monkeypatch.setattr(settings_service, "get_all", get_all)
    await registry.apply_settings(None)
    mf, wc = registry.DIRECT_SOURCES["mangafire"], registry.DIRECT_SOURCES["weebcentral"]
    assert mf not in registry.enabled_direct_sources(values)
    assert mf.content_proxy_enabled and mf.content_proxy_url == ""
    assert wc.content_proxy_enabled and wc.content_proxy_url == values["download_proxy_url"]
    assert not routing.route_for("weebcentral").all_requests
    with pytest.raises(RuntimeError, match="has no URL"):
        await mf.search_series("One Piece")


@pytest.mark.parametrize("key", ["download_proxy_url", "source_mangafire_proxy_url", "source_weebcentral_proxy_url"])
@pytest.mark.parametrize("url", ["http://user:private-secret@host:bad", "http://host:0", "http://host:99999", "http://[oops", "socks5://host", "http://host/path", "http://host?secret", "http://host#fragment", " http://host", "http://ho st"])
def test_invalid_proxy_urls_do_not_leak_secrets(key, url):
    with pytest.raises(ValueError, match="valid http") as exc:
        settings_service.validate({key: url})
    assert "private-secret" not in str(exc.value)


@pytest.mark.parametrize("name", routing.SOURCE_PROXY_NAMES)
def test_all_mode_validation_and_defaults(name):
    assert settings_service.DEFAULTS[f"source_{name}_proxy_mode"] == "inherit"
    with pytest.raises(ValueError, match="is required"):
        settings_service.validate({f"source_{name}_proxy_mode": "all"})
    with pytest.raises(ValueError, match="inherit or all"):
        settings_service.validate({f"source_{name}_proxy_mode": "typo"})
    settings_service.validate({f"source_{name}_proxy_mode": "all", f"source_{name}_enabled": "false"})
    settings_service.validate({f"source_{name}_proxy_mode": "all", f"source_{name}_proxy_url": "https://user:password@proxy.example:443", f"source_{name}_proxy_enabled": "true"})


async def test_settings_api_masks_and_preserves_proxy_urls_and_rejects_bad_mode(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        body = {"source_mangafire_proxy_mode": "all", "source_mangafire_proxy_url": "http://user:private-secret@proxy.example:8080"}
        saved = await settings_api.update_settings(body, session)
        assert saved["source_mangafire_proxy_url"] == settings_api.MASK
        saved["source_weebcentral_proxy_mode"] = "inherit"
        await settings_api.update_settings(saved, session)
        assert (await settings_service.get_all(session))["source_mangafire_proxy_url"] == body["source_mangafire_proxy_url"]
        with pytest.raises(HTTPException) as error:
            await settings_api.update_settings({"source_weebcentral_proxy_mode": "all"}, session)
        assert error.value.status_code == 422
        assert (await settings_service.get_all(session))["source_weebcentral_proxy_mode"] == "inherit"
    await engine.dispose()


@pytest.mark.parametrize("status,transient", [(403, False), (500, True)])
@respx.mock
async def test_signer_frontend_errors_preserve_status_and_retry_classification(status, transient):
    respx.get(f"{mangafire.BASE_URL}/").respond(status)
    with pytest.raises(MangaFireTokenError, match=f"HTTP {status}") as error:
        await mangafire.MangaFireSource().search_series("One Piece")
    assert is_transient_error(error.value) is transient


@respx.mock
async def test_concurrent_signing_uses_one_loaded_module():
    respx.get(f"{mangafire.BASE_URL}/").respond(200, text='<script src="/token.js"></script>')
    module = respx.get(f"{mangafire.BASE_URL}/token.js").respond(200, text='globalThis.getProtectionToken = function(path){return path;};')
    signer = mangafire.MangaFireSigner(mangafire.BASE_URL)
    async with httpx.AsyncClient() as client:
        tokens = await asyncio.gather(*(signer.sign(client, f"/chapters/{n}") for n in range(5)))
    assert tokens == [f"/chapters/{n}" for n in range(5)]
    assert module.call_count == 1


@pytest.mark.parametrize("status,transient", [(403, False), (500, True)])
async def test_proxied_image_errors_keep_retry_classification_without_secrets(status, transient, monkeypatch, tmp_path):
    source = weebcentral.WeebCentralSource()
    routing.set_routes({source.name: routing.SourceRoute(content_enabled=True, content_url="http://proxy.example:8080")})
    async def manifest(_):
        return ["http://images.invalid/page.jpg"]
    async def fail(client, url):
        response = httpx.Response(status, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("private-secret", request=response.request, response=response)
    async def no_wait(_):
        pass
    monkeypatch.setattr(source, "get_pages", manifest)
    monkeypatch.setattr(source, "download_page", fail)
    monkeypatch.setattr(direct.asyncio, "sleep", no_wait)
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        await direct.download_chapter_to_cbz(
            source, "1", SimpleNamespace(title="Series", description=""),
            SimpleNamespace(number=1, volume=1, title=""), tmp_path / "bad.cbz",
        )
    assert "private-secret" not in "".join(traceback.format_exception(error.value))
    assert is_transient_error(error.value) is transient
    assert not (tmp_path / "bad.cbz").exists()
