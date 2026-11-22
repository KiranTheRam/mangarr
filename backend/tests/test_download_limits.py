from types import SimpleNamespace

import httpx
import pytest

from mangarr.download import direct
from mangarr.sources.base import DirectSource


class PageStream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.read = False
        self.closed = False

    async def __aiter__(self):
        self.read = True
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class Source(DirectSource):
    name = "fixture"
    image_limiter = None

    async def search_series(self, query):
        return []

    async def list_chapters(self, external_id):
        return []

    async def get_pages(self, chapter_external_id):
        return ["https://cdn.example/redirect", "https://cdn.example/second"]


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("success", None),
        ("declared_size", "page exceeds"),
        ("streamed_size", "page exceeds"),
        ("encoding", "unsupported page content encoding"),
        ("chapter_size", "chapter exceeds the download byte limit"),
        ("page_count", "chapter exceeds the page count limit"),
    ],
)
async def test_direct_download_limits(scenario, error, tmp_path, monkeypatch):
    monkeypatch.setattr(direct, "MAX_PAGE_BYTES", 8)
    monkeypatch.setattr(direct, "MAX_CHAPTER_BYTES", 15 if scenario == "chapter_size" else 16)
    monkeypatch.setattr(direct, "MAX_CHAPTER_PAGES", 1 if scenario == "page_count" else 2)
    redirect = PageStream(b"x" * 100)
    page = PageStream(b"\xff\xd8\xffx", b"xxxx", *([b"x"] if scenario == "streamed_size" else []))
    headers = {}
    if scenario == "declared_size":
        headers["Content-Length"] = "9"
    if scenario == "encoding":
        headers["Content-Encoding"] = "gzip"
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Accept-Encoding"] == "identity"
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/page"}, stream=redirect)
        if request.url.path == "/page":
            return httpx.Response(200, headers=headers, stream=page)
        return httpx.Response(200, stream=PageStream(b"\xff\xd8\xffxxxxx"))

    client = httpx.AsyncClient
    monkeypatch.setattr(direct.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    dest = tmp_path / "chapter.cbz"
    series = SimpleNamespace(title="Fixture", description="")
    chapter = SimpleNamespace(title="Chapter", number=1, volume=1)
    if error:
        with pytest.raises(direct.DownloadLimitError, match=error):
            await direct.download_chapter_to_cbz(Source(), "1", series, chapter, dest)
        assert list(tmp_path.iterdir()) == []
    else:
        await direct.download_chapter_to_cbz(Source(), "1", series, chapter, dest)
        assert dest.is_file()
    assert not redirect.read
    if scenario == "page_count":
        assert not requests
    else:
        assert redirect.closed and page.closed
    if scenario in {"encoding", "declared_size"}:
        assert not page.read
