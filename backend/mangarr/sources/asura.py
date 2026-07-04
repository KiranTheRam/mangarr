"""Asura Scans source.

Asura is an Astro site backed by a JSON API (api.asurascans.com). It's
manhwa/manhua-focused (Korean/Chinese webtoons), not Japanese manga. We use
the API for search, chapter list, and page images, and skip premium chapters
whose early-access window hasn't opened yet.
"""

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, parse_chapter_number
from .base import DirectSource, SourceChapter, SourceSeries

SITE_URL = "https://asurascans.com"
API_URL = "https://api.asurascans.com"

_limiter = RateLimiter(rate=2, per_seconds=1)


class AsuraSource(DirectSource):
    name = "asura"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Origin": SITE_URL,
                "Referer": f"{SITE_URL}/",
            },
            timeout=60,
            follow_redirects=True,
        )

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await _limiter.acquire()
        resp = await self._client.get(f"{API_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _public_slug(item: dict) -> str:
        # public_url is "/comics/<slug>-<hash>"; the hashed slug is what the
        # chapter endpoints expect. Fall back to the bare slug.
        url = item.get("public_url") or ""
        if url.startswith("/comics/"):
            return url[len("/comics/"):]
        return item.get("slug", "")

    async def search_series(self, query: str) -> list[SourceSeries]:
        data = await self._get("/api/series", params={"search": query})
        results = []
        for item in data.get("data") or []:
            slug = self._public_slug(item)
            if not slug:
                continue
            alts = item.get("alt_titles") or []
            if isinstance(alts, str):
                alts = [a.strip() for a in alts.split("•") if a.strip()]
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=slug,
                    title=item.get("title") or "Unknown",
                    alt_titles=alts,
                    url=f"{SITE_URL}/comics/{slug}",
                )
            )
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        data = await self._get(f"/api/series/{external_id}/chapters")
        rows = data.get("data") if isinstance(data.get("data"), list) else data
        if not isinstance(rows, list):
            rows = []
        chapters: dict[float, SourceChapter] = {}
        for row in rows:
            if not isinstance(row, dict) or "number" not in row:
                continue
            # premium chapters are locked until early_access_until passes;
            # get_pages returns nothing for them, so skip to avoid empty grabs
            if row.get("is_premium") and (row.get("page_count") or 0) == 0:
                continue
            number = parse_chapter_number(str(row.get("number")))
            if number is None:
                continue
            if number not in chapters:
                chapters[number] = SourceChapter(
                    source_name=self.name,
                    # get_pages needs both the series slug and chapter number
                    external_id=f"{external_id}|{row['number']}",
                    number=number,
                    title=row.get("title") or "",
                    url=f"{SITE_URL}/comics/{external_id}/chapter/{row['number']}",
                )
        return sorted(chapters.values(), key=lambda c: c.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        # chapter_external_id is "<series_slug>|<chapter_number>"
        series_slug, _, number = chapter_external_id.rpartition("|")
        data = await self._get(f"/api/series/{series_slug}/chapters/{number}")
        chapter = (data.get("data") or {}).get("chapter") or {}
        pages = chapter.get("pages") or []
        urls = []
        for page in pages:
            url = page.get("url") if isinstance(page, dict) else page
            if url:
                urls.append(url)
        return urls

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        resp = await client.get(url, headers={"Referer": f"{SITE_URL}/"})
        resp.raise_for_status()
        return resp.content


source = AsuraSource()
