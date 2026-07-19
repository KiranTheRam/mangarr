"""MangaFire JSON API source.

The public site is a JavaScript application, but its reader uses a compact
JSON API for title search, chapter lists, and page URLs.  Using that API is
both less brittle and substantially cheaper than scraping rendered HTML.
"""

import re
from urllib.parse import urljoin

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

BASE_URL = "https://mangafire.to"
API_URL = f"{BASE_URL}/api"

_limiter = RateLimiter(rate=2, per_seconds=1)
_image_limiter = RateLimiter(rate=5, per_seconds=1)
_UPLOAD_TAG = re.compile(r"^\([^()]{1,30}\)$")
_TITLE_NUMBER = re.compile(
    r"^\s*(?:class|chapter|ch\.?|#)\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE
)


class MangaFireSource(DirectSource):
    name = "mangafire"
    image_limiter = _image_limiter

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/",
            },
            timeout=60,
            follow_redirects=True,
        )

    async def _get(self, path: str, params: dict | None = None) -> dict:
        response = await rl_request(
            self._client, "GET", f"{API_URL}{path}", limiter=_limiter, params=params
        )
        response.raise_for_status()
        return response.json()

    def image_headers(self) -> dict:
        return {"Referer": f"{BASE_URL}/"}

    async def search_series(self, query: str) -> list[SourceSeries]:
        data = await self._get("/titles", params={"keyword": query})
        results: list[SourceSeries] = []
        for item in (data.get("items") or [])[:10]:
            hid = str(item.get("hid") or "").strip()
            title = str(item.get("title") or "").strip()
            if not hid or not title:
                continue
            raw_url = str(item.get("url") or f"/title/{hid}-{item.get('slug', '')}")
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=hid,
                    title=title,
                    url=urljoin(BASE_URL, raw_url),
                )
            )
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        chapters: dict[float, SourceChapter] = {}
        page = 1
        while True:
            data = await self._get(
                f"/titles/{external_id}/chapters",
                params={"language": "en", "page": page},
            )
            for item in data.get("items") or []:
                if item.get("language") != "en":
                    continue
                try:
                    number = float(item.get("number"))
                except (TypeError, ValueError):
                    continue
                chapter_id = str(item.get("id") or "").strip()
                if not chapter_id or number in chapters:
                    # The API returns official before unofficial duplicates.
                    # One readable edition per number is enough for Mangarr.
                    continue
                raw_title = str(item.get("name") or "").strip()
                # Some official-volume imports expose positional API numbers
                # such as 0.01/97.01 while their names say "Class 2"/"Class
                # 98".  The explicit display number is the canonical chapter;
                # accepting the positional value would create hundreds of
                # fake decimal specials.
                title_number = _TITLE_NUMBER.match(raw_title)
                if title_number:
                    number = float(title_number.group(1))
                    if number in chapters:
                        continue
                title = "" if _UPLOAD_TAG.fullmatch(raw_title) else raw_title
                chapters[number] = SourceChapter(
                    source_name=self.name,
                    external_id=chapter_id,
                    number=number,
                    title=title,
                    language="en",
                    url=f"{BASE_URL}/title/{external_id}/chapter/{chapter_id}",
                )
            meta = data.get("meta") or {}
            if not meta.get("hasNext"):
                break
            page += 1
        return sorted(chapters.values(), key=lambda chapter: chapter.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        data = await self._get(f"/chapters/{chapter_external_id}")
        chapter = data.get("data") or {}
        return [
            str(page["url"])
            for page in chapter.get("pages") or []
            if isinstance(page, dict) and page.get("url")
        ]


source = MangaFireSource()
