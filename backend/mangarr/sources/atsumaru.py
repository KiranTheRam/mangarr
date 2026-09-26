"""Atsumaru (atsu.moe) source.

Atsumaru is a React app over plain JSON endpoints: a Typesense collection for
title search, ``/api/manga/info`` for the full chapter list, and
``/api/read/chapter`` for page images served from ``cdn.atsu.moe``.

A title usually carries several scanlation groups, each publishing its own
copy of a chapter. The site ranks those groups by community score
(``/api/manga/page``); for every chapter number we take the copy from the
best-ranked group that actually has pages. Search also indexes web novels,
whose chapters have no images, so only comics are returned.

Pages are WebP for older uploads and AVIF for recent ones; both are kept as
served (Kavita and Komga read them directly).
"""

import re
from urllib.parse import urljoin

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

SITE_URL = "https://atsu.moe"
CDN_URL = "https://cdn.atsu.moe"

_limiter = RateLimiter(rate=2, per_seconds=1)
_image_limiter = RateLimiter(rate=5, per_seconds=1)
# "Chapter 12" / "Episode 12" / "# 12" carry no title of their own
_GENERIC_TITLE = re.compile(r"^\s*(?:chapter|episode|ch\.?|ep\.?|#)?\s*\d+(?:\.\d+)?\s*$", re.I)


class AtsumaruSource(DirectSource):
    name = "atsumaru"
    image_limiter = _image_limiter

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": f"{SITE_URL}/",
            },
            timeout=60,
            trust_env=False,
            follow_redirects=True,
        )

    def image_headers(self) -> dict:
        return {"Referer": f"{SITE_URL}/"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        response = await rl_request(
            self._client, "GET", f"{SITE_URL}{path}", limiter=_limiter, params=params
        )
        response.raise_for_status()
        return response.json()

    async def search_series(self, query: str) -> list[SourceSeries]:
        if not query.strip():
            return []
        data = await self._get(
            "/collections/manga/documents/search",
            params={
                "q": query,
                "query_by": "title,englishTitle,otherNames",
                "per_page": 20,
            },
        )
        results: list[SourceSeries] = []
        for hit in data.get("hits") or []:
            doc = hit.get("document") or {}
            manga_id = str(doc.get("id") or "").strip()
            title = str(doc.get("title") or "").strip()
            # novels share the index but have no page images
            if not manga_id or not title or doc.get("hidden") or doc.get("medium") != "Comic":
                continue
            alt_titles = [
                name
                for name in [doc.get("englishTitle"), *(doc.get("otherNames") or [])]
                if isinstance(name, str) and name.strip() and name.strip() != title
            ]
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=manga_id,
                    title=title,
                    alt_titles=list(dict.fromkeys(alt_titles)),
                    url=f"{SITE_URL}/manga/{manga_id}",
                )
            )
        return results

    async def _group_rank(self, manga_id: str) -> dict[str, int]:
        """scanlation group id → rank (0 = the community's preferred group)."""
        page = (await self._get("/api/manga/page", params={"id": manga_id})).get("mangaPage") or {}
        groups = [g for g in page.get("scanlators") or [] if isinstance(g, dict) and g.get("id")]
        # stable sort keeps the site's own order between equally scored groups
        groups.sort(key=lambda g: -(g.get("score") or 0))
        return {g["id"]: rank for rank, g in enumerate(groups)}

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        rank = await self._group_rank(external_id)
        info = await self._get("/api/manga/info", params={"mangaId": external_id})
        best: dict[float, tuple[int, dict]] = {}
        for item in info.get("chapters") or []:
            if not isinstance(item, dict) or not item.get("id") or not item.get("pageCount"):
                continue
            try:
                number = float(item["number"])
            except (KeyError, TypeError, ValueError):
                continue
            group_rank = rank.get(item.get("scanId"), len(rank))
            if number not in best or group_rank < best[number][0]:
                best[number] = (group_rank, item)
        chapters = []
        for number, (_, item) in best.items():
            title = str(item.get("title") or "").strip()
            chapters.append(
                SourceChapter(
                    source_name=self.name,
                    # the reader endpoint needs both ids
                    external_id=f"{external_id}|{item['id']}",
                    number=number,
                    title="" if _GENERIC_TITLE.match(title) else title,
                    url=f"{SITE_URL}/read/{external_id}/{item['id']}",
                )
            )
        return sorted(chapters, key=lambda chapter: chapter.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        manga_id, _, chapter_id = chapter_external_id.partition("|")
        data = await self._get(
            "/api/read/chapter", params={"mangaId": manga_id, "chapterId": chapter_id}
        )
        pages = [
            page
            for page in (data.get("readChapter") or {}).get("pages") or []
            if isinstance(page, dict) and page.get("image")
        ]
        pages.sort(key=lambda page: page.get("number") or 0)
        return [urljoin(f"{CDN_URL}/", page["image"]) for page in pages]

    # download_page inherited: rate-limited image fetch with back-off + Referer


source = AtsumaruSource()
