import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import MetadataProvider, SeriesMetadata

API_URL = "https://graphql.anilist.co"

MEDIA_FIELDS = """
id
title { romaji english native }
synonyms
description(asHtml: false)
status
startDate { year }
coverImage { extraLarge large }
bannerImage
genres
chapters
volumes
"""

SEARCH_QUERY = f"""
query ($search: String, $perPage: Int) {{
  Page(perPage: $perPage) {{
    media(search: $search, type: MANGA) {{ {MEDIA_FIELDS} }}
  }}
}}
"""

GET_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: MANGA) {{ {MEDIA_FIELDS} }}
}}
"""

STATUS_MAP = {
    "RELEASING": "releasing",
    "FINISHED": "finished",
    "HIATUS": "hiatus",
    "CANCELLED": "cancelled",
    "NOT_YET_RELEASED": "not_yet_released",
}

# AniList allows ~90 req/min; stay well under it
_limiter = RateLimiter(rate=1, per_seconds=1)


class AniListProvider(MetadataProvider):
    name = "anilist"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30
        )

    async def _query(self, query: str, variables: dict) -> dict:
        resp = await rl_request(
            self._client, "POST", API_URL, limiter=_limiter,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"AniList error: {data['errors']}")
        return data["data"]

    def _to_metadata(self, media: dict) -> SeriesMetadata:
        titles = media.get("title") or {}
        title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Unknown"
        alt = [
            t
            for t in [titles.get("romaji"), titles.get("native"), *(media.get("synonyms") or [])]
            if t and t != title
        ]
        cover = media.get("coverImage") or {}
        return SeriesMetadata(
            provider=self.name,
            provider_id=str(media["id"]),
            title=title,
            alt_titles=alt,
            description=media.get("description") or "",
            status=STATUS_MAP.get(media.get("status") or "", "unknown"),
            year=(media.get("startDate") or {}).get("year"),
            cover_url=cover.get("extraLarge") or cover.get("large") or "",
            banner_url=media.get("bannerImage") or "",
            genres=media.get("genres") or [],
            total_chapters=media.get("chapters"),
            total_volumes=media.get("volumes"),
        )

    async def search(self, query: str, limit: int = 20) -> list[SeriesMetadata]:
        data = await self._query(SEARCH_QUERY, {"search": query, "perPage": limit})
        return [self._to_metadata(m) for m in data["Page"]["media"]]

    async def get_series(self, provider_id: str) -> SeriesMetadata | None:
        try:
            data = await self._query(GET_QUERY, {"id": int(provider_id)})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        media = data.get("Media")
        return self._to_metadata(media) if media else None


provider = AniListProvider()
