from types import SimpleNamespace

import httpx
import pytest

from mangarr import settings_service
from mangarr.download import direct
from mangarr.sources import registry
from mangarr.sources.base import DirectSource


class FakeSource(DirectSource):
    name = "fake"

    def __init__(self) -> None:
        self.page_calls = 0

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return []

    async def get_pages(self, chapter_external_id):
        return ["https://cdn.example/page-1.jpg"]

    async def download_page(self, client, url):
        self.page_calls += 1
        return b"\xff\xd8\xff\xe0image"


class CapturingClient:
    created: list["CapturingClient"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.created.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def media_objects():
    return (
        SimpleNamespace(title="Test Series", description=""),
        SimpleNamespace(number=1.0, volume=1, title="Chapter One"),
    )


@pytest.fixture(autouse=True)
def capture_clients(monkeypatch):
    CapturingClient.created.clear()
    monkeypatch.setattr(direct.httpx, "AsyncClient", CapturingClient)


async def test_checked_source_uses_proxy_for_page_bytes(tmp_path):
    source = FakeSource()
    source.content_proxy_enabled = True
    source.content_proxy_url = "http://192.168.1.28:8888"
    series, chapter = media_objects()

    await direct.download_chapter_to_cbz(
        source, "chapter-1", series, chapter, tmp_path / "chapter.cbz"
    )

    assert len(CapturingClient.created) == 1
    assert CapturingClient.created[0].kwargs["proxy"] == source.content_proxy_url
    assert CapturingClient.created[0].kwargs["trust_env"] is False


async def test_unchecked_source_is_explicitly_direct(tmp_path):
    source = FakeSource()
    series, chapter = media_objects()

    await direct.download_chapter_to_cbz(
        source, "chapter-1", series, chapter, tmp_path / "chapter.cbz"
    )

    assert len(CapturingClient.created) == 1
    assert "proxy" not in CapturingClient.created[0].kwargs
    assert CapturingClient.created[0].kwargs["trust_env"] is False


async def test_checked_source_without_url_fails_before_any_request(tmp_path):
    source = FakeSource()
    source.content_proxy_enabled = True
    series, chapter = media_objects()

    with pytest.raises(RuntimeError, match="proxy is enabled but has no URL"):
        await direct.download_chapter_to_cbz(
            source, "chapter-1", series, chapter, tmp_path / "chapter.cbz"
        )

    assert CapturingClient.created == []
    assert source.page_calls == 0


async def test_proxy_failure_never_creates_a_direct_fallback(tmp_path, monkeypatch):
    source = FakeSource()
    source.content_proxy_enabled = True
    source.content_proxy_url = "http://127.0.0.1:1"

    async def fail_through_proxy(client, url):
        source.page_calls += 1
        raise httpx.ProxyError("proxy unavailable")

    async def no_wait(_seconds):
        return None

    source.download_page = fail_through_proxy
    monkeypatch.setattr(direct.asyncio, "sleep", no_wait)
    series, chapter = media_objects()

    with pytest.raises(RuntimeError, match="page 1 failed"):
        await direct.download_chapter_to_cbz(
            source, "chapter-1", series, chapter, tmp_path / "chapter.cbz"
        )

    assert source.page_calls == 3
    assert len(CapturingClient.created) == 1
    assert CapturingClient.created[0].kwargs["proxy"] == source.content_proxy_url


async def test_runtime_settings_select_only_content_sources(monkeypatch):
    values = dict(settings_service.DEFAULTS)
    values["download_proxy_url"] = "http://192.168.1.28:8888"
    values["source_mangafire_proxy_enabled"] = "true"
    # Even a manually inserted flag cannot make metadata-only VIZ use it.
    values["source_viz_proxy_enabled"] = "true"

    async def get_all(_session):
        return values

    monkeypatch.setattr(registry.settings_service, "get_all", get_all)
    for source in registry.DIRECT_SOURCES.values():
        monkeypatch.setattr(source, "content_proxy_enabled", False)
        monkeypatch.setattr(source, "content_proxy_url", "")

    await registry.apply_settings(None)

    assert registry.DIRECT_SOURCES["mangafire"].content_proxy_enabled is True
    assert (
        registry.DIRECT_SOURCES["mangafire"].content_proxy_url
        == values["download_proxy_url"]
    )
    assert registry.DIRECT_SOURCES["mangadex"].content_proxy_enabled is False
    assert registry.DIRECT_SOURCES["viz"].content_proxy_enabled is False


async def test_runtime_settings_ignore_proxy_flag_for_disabled_source(monkeypatch):
    values = dict(settings_service.DEFAULTS)
    values["download_proxy_url"] = "http://192.168.1.28:8888"
    values["source_mangafire_enabled"] = "false"
    values["source_mangafire_proxy_enabled"] = "true"

    async def get_all(_session):
        return values

    monkeypatch.setattr(registry.settings_service, "get_all", get_all)

    await registry.apply_settings(None)

    source = registry.DIRECT_SOURCES["mangafire"]
    assert source.content_proxy_enabled is False
    assert source.content_proxy_url == ""
