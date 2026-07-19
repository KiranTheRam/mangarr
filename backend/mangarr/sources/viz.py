"""VIZ's public chapter archive as official printed-volume metadata.

This is metadata-only: it never bypasses subscriptions or downloads pages.
The archive's own "Vol. N" purchase labels provide exact chapter placement
for VIZ-licensed series; other publishers continue through Wikipedia and the
community fallbacks.
"""

import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import RateLimiter, normalize_title, rl_request
from .base import ChapterMetadata, DirectSource, SourceChapter, SourceSeries

BASE_URL = "https://www.viz.com"
_CHAPTER_PATH = re.compile(r"^/(?:shonenjump|vizmanga)/chapters/[^/?#]+$")
_VOLUME = re.compile(r"\bvol(?:ume)?\.?\s*(\d+)\b", re.I)
_limiter = RateLimiter(rate=1, per_seconds=1)
CACHE_TTL = 24 * 3600.0


def parse_archive(html: str, url: str = "") -> list[ChapterMetadata]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[ChapterMetadata] = []
    seen: set[float] = set()
    # No fallback when the volume containers are missing: treating the whole
    # page as one container would stamp every chapter with the first "Vol. N"
    # label found anywhere — wholesale-wrong data at official authority. If
    # VIZ changes this markup, returning nothing is the honest answer.
    containers = soup.select(".o_chapter-vol-container")
    for container in containers:
        volume = None
        for node in container.select("[aria-label]"):
            match = _VOLUME.search(node.get("aria-label", ""))
            if match:
                volume = int(match.group(1))
                break
        if volume is None:
            continue
        for anchor in container.select("a[name]"):
            raw = str(anchor.get("name", "")).strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                continue
            number = float(raw)
            if number in seen:
                continue
            seen.add(number)
            rows.append(ChapterMetadata(
                source_name="viz", number=number, volume=volume,
                url=urljoin(url or BASE_URL, anchor.get("href", "")),
            ))
    return rows


class VizSource(DirectSource):
    name = "viz"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, trust_env=False,
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, list[ChapterMetadata]]] = {}

    async def search_series(self, query: str) -> list[SourceSeries]:
        response = await rl_request(
            self._client, "GET", f"{BASE_URL}/search", limiter=_limiter,
            params={"search": query},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SourceSeries] = []
        seen: set[str] = set()
        wanted = normalize_title(query)
        for anchor in soup.select("a[href]"):
            path = str(anchor.get("href", "")).split("?", 1)[0]
            if not _CHAPTER_PATH.fullmatch(path) or path in seen:
                continue
            rel = anchor.get("rel") or []
            title = " ".join(rel) if isinstance(rel, list) else str(rel)
            title = title.strip() or anchor.get_text(" ", strip=True)
            if not title:
                continue
            # Search pages can contain promoted unrelated properties.
            normalized = normalize_title(title)
            if wanted and wanted != normalized and wanted not in normalized and normalized not in wanted:
                continue
            seen.add(path)
            results.append(SourceSeries(
                source_name=self.name, external_id=path, title=title,
                url=urljoin(BASE_URL, path),
            ))
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        return []

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        raise NotImplementedError("viz is metadata-only")

    async def get_chapter_metadata(self, external_id: str) -> list[ChapterMetadata]:
        cached = self._cache.get(external_id)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
        url = urljoin(BASE_URL, external_id)
        response = await rl_request(self._client, "GET", url, limiter=_limiter)
        response.raise_for_status()
        rows = parse_archive(response.text, url)
        self._cache[external_id] = (time.monotonic() + CACHE_TTL, rows)
        return list(rows)

    async def get_volume_map(self, external_id: str) -> dict[float, int]:
        return {float(row.number): row.volume
                for row in await self.get_chapter_metadata(external_id)
                if row.number is not None and row.volume is not None}


source = VizSource()
