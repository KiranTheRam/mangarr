"""Toonily's public HTML search, chapter lists and reader images.

Each image carries its reader Referer with it, rather than storing mutable
last-chapter state on the shared source. Concurrent chapters can safely use
the same CDN URL. Image requests still use the downloader's configured route.
"""

import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..util import RateLimiter, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

BASE_URL = "https://toonily.com"
BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CHAPTER = re.compile(r"^chapter\s+(\d+(?:\.\d+)?)(?![\d.])(?:\s|$|[-:–—])", re.I)
_SLUG = re.compile(r"^[a-zA-Z0-9_-]+$")
_limiter = RateLimiter(rate=1, per_seconds=1)
_image_limiter = RateLimiter(rate=3, per_seconds=1)


class _PageURL(str):
    """A normal image URL with request-local reader context."""

    def __new__(cls, url: str, referer: str):
        value = super().__new__(cls, url)
        value.referer = referer
        return value


def _site_path(url: str) -> str:
    parsed = urlsplit(urljoin(BASE_URL + "/", url))
    if parsed.scheme != "https" or parsed.netloc != "toonily.com":
        raise ValueError("Not a Toonily URL")
    parts = parsed.path.strip("/").split("/")
    if len(parts) not in (2, 3) or parts[0] != "serie" or not all(
        _SLUG.fullmatch(part) for part in parts[1:]
    ):
        raise ValueError("Invalid Toonily series or chapter path")
    return "/" + "/".join(parts) + "/"


class ToonilySource(DirectSource):
    name = "toonily"
    image_limiter = _image_limiter

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": BROWSER_AGENT, "Referer": BASE_URL + "/"},
            timeout=60, trust_env=False, follow_redirects=True,
        )

    async def _html(self, url: str, **kwargs) -> BeautifulSoup:
        response = await rl_request(self._client, "GET", url, limiter=_limiter, **kwargs)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")

    async def search_series(self, query: str) -> list[SourceSeries]:
        if not query.strip():
            return []
        soup = await self._html(BASE_URL + "/", params={"s": query, "post_type": "wp-manga"})
        results = {}
        for link in soup.select(".post-title a"):
            try:
                path = _site_path(str(link.get("href") or ""))
            except ValueError:
                continue
            title = link.get_text(" ", strip=True)
            if path.count("/") != 3 or not title:
                continue
            slug = path.split("/")[2]
            results.setdefault(slug, SourceSeries(
                source_name=self.name, external_id=slug, title=title, url=BASE_URL + path,
            ))
        return list(results.values())

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        if not _SLUG.fullmatch(external_id):
            raise ValueError("Invalid Toonily series ID")
        series_path = f"/serie/{external_id}/"
        soup = await self._html(BASE_URL + series_path)
        chapters = {}
        for link in soup.select(".wp-manga-chapter a"):
            title = link.get_text(" ", strip=True)
            # Side Story 1 is not main chapter 1. Only expose explicit chapter
            # numbers; guessing numbers for independent extras risks replacing
            # another source's main chapters with unrelated content.
            match = _CHAPTER.match(title)
            if not match:
                continue
            try:
                path = _site_path(str(link.get("href") or ""))
            except ValueError:
                continue
            if not path.startswith(series_path) or path.count("/") != 4:
                continue
            number = float(match.group(1))
            chapters.setdefault(number, SourceChapter(
                source_name=self.name, external_id=path, number=number,
                title=title, url=BASE_URL + path,
            ))
        return sorted(chapters.values(), key=lambda chapter: chapter.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        path = _site_path(chapter_external_id)
        if path.count("/") != 4:
            raise ValueError("Invalid Toonily chapter ID")
        reader_url = BASE_URL + path
        soup = await self._html(reader_url)
        pages = []
        for image in soup.select(".reading-content img.wp-manga-chapter-img"):
            url = str(image.get("data-src") or image.get("data-lazy-src") or image.get("src") or "").strip()
            if not url:
                continue
            url = urljoin(reader_url, url)
            if urlsplit(url).scheme not in ("https", "http") or url in pages:
                continue
            pages.append(_PageURL(url, reader_url))
        return pages

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        if not isinstance(url, _PageURL):
            raise ValueError("Toonily image is missing its chapter Referer")
        headers = {"User-Agent": BROWSER_AGENT, "Referer": url.referer}
        # The CDN varies .jpg responses by Accept and can otherwise serve a
        # lossy WebP derivative. Prefer its JPEG variant without transcoding
        # the returned bytes or changing requests for other image formats.
        if urlsplit(url).path.lower().endswith((".jpg", ".jpeg")):
            headers["Accept"] = "image/jpeg"
        response = await rl_request(
            client, "GET", str(url), limiter=self.image_limiter,
            headers=headers,
        )
        response.raise_for_status()
        return response.content


source = ToonilySource()
