"""TCB Scans scraper.

TCB is a small, fast scanlation group for major Shonen Jump titles (One
Piece, Jujutsu Kaisen, My Hero Academia, Chainsaw Man…). It has no search
endpoint, so we match against its full /projects catalog, which is small
enough to fetch and cache.
"""

import re
import time

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import RateLimiter, normalize_title, parse_chapter_number
from .base import DirectSource, SourceChapter, SourceSeries

BASE_URL = "https://tcbonepiecechapters.com"
MANGA_URL_RE = re.compile(r"/mangas/(\d+/[^\"']+)")
CHAPTER_URL_RE = re.compile(r"/chapters/(\d+/[^\"']+)")

_limiter = RateLimiter(rate=1, per_seconds=1)
_CATALOG_TTL = 600  # seconds


class TCBScansSource(DirectSource):
    name = "tcbscans"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"{BASE_URL}/",
            },
            timeout=60,
            follow_redirects=True,
        )
        self._catalog: list[SourceSeries] = []
        self._catalog_at = 0.0

    async def _get_html(self, url: str) -> BeautifulSoup:
        await _limiter.acquire()
        resp = await self._client.get(url)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    async def _load_catalog(self) -> list[SourceSeries]:
        if self._catalog and time.time() - self._catalog_at < _CATALOG_TTL:
            return self._catalog
        soup = await self._get_html(f"{BASE_URL}/projects")
        catalog: list[SourceSeries] = []
        seen: set[str] = set()
        for link in soup.find_all("a", href=MANGA_URL_RE):
            m = MANGA_URL_RE.search(link.get("href", ""))
            if not m or m.group(1) in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            seen.add(m.group(1))
            catalog.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=m.group(1),  # "5/one-piece"
                    title=title,
                    url=f"{BASE_URL}/mangas/{m.group(1)}",
                )
            )
        self._catalog = catalog
        self._catalog_at = time.time()
        return catalog

    async def search_series(self, query: str) -> list[SourceSeries]:
        catalog = await self._load_catalog()
        nq = normalize_title(query)
        scored: list[tuple[int, SourceSeries]] = []
        for series in catalog:
            nt = normalize_title(series.title)
            if nt == nq:
                score = 3
            elif nt.startswith(nq) or nq.startswith(nt):
                score = 2
            elif nq in nt or nt in nq:
                score = 1
            else:
                continue
            scored.append((score, series))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [s for _, s in scored]

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        soup = await self._get_html(f"{BASE_URL}/mangas/{external_id}")
        chapters: dict[float, SourceChapter] = {}
        for link in soup.find_all("a", href=CHAPTER_URL_RE):
            m = CHAPTER_URL_RE.search(link.get("href", ""))
            if not m:
                continue
            text = link.get_text(" ", strip=True)
            number = parse_chapter_number(text)
            if number is None:
                continue
            # manga page is newest-first; keep the first (canonical) link seen
            if number not in chapters:
                chapters[number] = SourceChapter(
                    source_name=self.name,
                    external_id=m.group(1),  # "7991/one-piece-chapter-1187"
                    number=number,
                    title=text,
                    url=f"{BASE_URL}/chapters/{m.group(1)}",
                )
        return sorted(chapters.values(), key=lambda c: c.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        soup = await self._get_html(f"{BASE_URL}/chapters/{chapter_external_id}")
        urls = []
        # page images carry the fixed-ratio-content class; the site logo does not
        for img in soup.find_all("img", class_="fixed-ratio-content"):
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http"):
                urls.append(src)
        return urls

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        resp = await client.get(url, headers={"Referer": f"{BASE_URL}/"})
        resp.raise_for_status()
        return resp.content


source = TCBScansSource()
