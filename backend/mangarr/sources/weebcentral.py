"""WeebCentral scraper.

The site is HTMX-driven: search and chapter lists are fetched as HTML
fragments from dedicated endpoints, which makes scraping fairly stable.
"""

import re

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import RateLimiter, parse_chapter_number
from .base import DirectSource, SourceChapter, SourceSeries

BASE_URL = "https://weebcentral.com"
SERIES_URL_RE = re.compile(r"/series/([A-Z0-9]+)")
CHAPTER_URL_RE = re.compile(r"/chapters/([A-Z0-9]+)")

_limiter = RateLimiter(rate=1, per_seconds=1)


class WeebCentralSource(DirectSource):
    name = "weebcentral"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/"},
            timeout=60,
            follow_redirects=True,
        )

    async def _get_html(self, url: str, params: dict | None = None) -> BeautifulSoup:
        await _limiter.acquire()
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    async def search_series(self, query: str) -> list[SourceSeries]:
        soup = await self._get_html(
            f"{BASE_URL}/search/data",
            params={
                "text": query,
                "limit": 10,
                "offset": 0,
                "sort": "Best Match",
                "order": "Descending",
                "official": "Any",
                "display_mode": "Full Display",
            },
        )
        results = []
        seen: set[str] = set()
        for link in soup.find_all("a", href=SERIES_URL_RE):
            href = link.get("href", "")
            m = SERIES_URL_RE.search(href)
            if not m or m.group(1) in seen:
                continue
            # cover img alt ("<Title> cover") is cleaner than link text, which
            # can include badge ribbons like "Official"
            img = link.find("img")
            title = (img.get("alt", "") if img else "").removesuffix(" cover").strip()
            if not title:
                title = link.get_text(strip=True)
            if not title:
                continue
            seen.add(m.group(1))
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=m.group(1),
                    title=title,
                    url=href if href.startswith("http") else f"{BASE_URL}{href}",
                )
            )
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        soup = await self._get_html(f"{BASE_URL}/series/{external_id}/full-chapter-list")
        chapters: dict[float, SourceChapter] = {}
        for link in soup.find_all("a", href=CHAPTER_URL_RE):
            m = CHAPTER_URL_RE.search(link.get("href", ""))
            if not m:
                continue
            text = link.get_text(" ", strip=True)
            number = parse_chapter_number(text)
            if number is None:
                continue
            if number not in chapters:
                chapters[number] = SourceChapter(
                    source_name=self.name,
                    external_id=m.group(1),
                    number=number,
                    url=f"{BASE_URL}/chapters/{m.group(1)}",
                )
        return sorted(chapters.values(), key=lambda c: c.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        soup = await self._get_html(
            f"{BASE_URL}/chapters/{chapter_external_id}/images",
            params={"is_prev": "False", "reading_style": "long_strip"},
        )
        urls = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http"):
                urls.append(src)
        return urls

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        # Image CDN requires the site referer
        resp = await client.get(url, headers={"Referer": f"{BASE_URL}/"})
        resp.raise_for_status()
        return resp.content


source = WeebCentralSource()
