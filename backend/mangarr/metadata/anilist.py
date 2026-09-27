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

_NODE_FIELDS = """
id type format status
startDate { year }
title { romaji english native }
synonyms
coverImage { large }
"""

RELATED_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: MANGA) {{
    relations {{ edges {{ relationType(version: 2) node {{ {_NODE_FIELDS} }} }} }}
    recommendations(sort: RATING_DESC, perPage: 25) {{
      nodes {{ rating mediaRecommendation {{ {_NODE_FIELDS} }} }}
    }}
  }}
}}
"""

USER_LIST_QUERY = f"""
query ($user: String, $statuses: [MediaListStatus], $chunk: Int) {{
  MediaListCollection(
    userName: $user, type: MANGA, status_in: $statuses, chunk: $chunk, perChunk: 500
  ) {{
    hasNextChunk
    lists {{ entries {{ media {{ {_NODE_FIELDS} }} }} }}
  }}
}}
"""

MAL_IDS_QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(idMal_in: $ids, type: MANGA) { id idMal }
  }
}
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

    async def get_related(self, anilist_id: int) -> tuple[list[dict], list[dict]]:
        """Raw relation edges ({relationType, node}) and recommendation nodes
        ({rating, mediaRecommendation}) of a manga, anime ones included —
        the caller decides what is worth showing."""
        data = await self._query(RELATED_QUERY, {"id": anilist_id})
        media = data.get("Media") or {}
        edges = ((media.get("relations") or {}).get("edges")) or []
        recs = ((media.get("recommendations") or {}).get("nodes")) or []
        return edges, recs

    async def _query_explained(self, query: str, variables: dict) -> dict:
        """_query, but an error answer (AniList sends "Private User" and
        "User not found" as HTTP 404) raises with AniList's own message."""
        try:
            return await self._query(query, variables)
        except httpx.HTTPStatusError as exc:
            try:
                message = exc.response.json()["errors"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise exc from None
            raise RuntimeError(f"AniList: {message}") from None

    async def user_manga_list(self, username: str, statuses: list[str]) -> list[dict]:
        """Media nodes on a public user's manga list with the given statuses
        (CURRENT, PLANNING, COMPLETED, PAUSED, DROPPED, REPEATING)."""
        media: list[dict] = []
        chunk = 1
        while True:
            data = await self._query_explained(
                USER_LIST_QUERY, {"user": username, "statuses": statuses, "chunk": chunk}
            )
            collection = data.get("MediaListCollection") or {}
            for group in collection.get("lists") or []:
                media.extend(e["media"] for e in group.get("entries") or [] if e.get("media"))
            if not collection.get("hasNextChunk") or chunk >= 20:
                return media
            chunk += 1

    async def ids_for_mal(self, mal_ids: list[int]) -> dict[int, int]:
        """MyAnimeList id → AniList id for the manga AniList knows."""
        out: dict[int, int] = {}
        for start in range(0, len(mal_ids), 50):
            page = 1
            while True:
                data = await self._query_explained(
                    MAL_IDS_QUERY, {"ids": mal_ids[start:start + 50], "page": page}
                )
                block = data.get("Page") or {}
                for m in block.get("media") or []:
                    if m.get("idMal"):
                        out[int(m["idMal"])] = int(m["id"])
                if not (block.get("pageInfo") or {}).get("hasNextPage"):
                    break
                page += 1
        return out

    def node_to_metadata(self, node: dict) -> SeriesMetadata:
        """A relation/recommendation node as metadata (it carries the same
        fields a search result does, minus description and totals)."""
        return self._to_metadata(node)

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
