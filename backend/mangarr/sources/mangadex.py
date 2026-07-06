import time

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, parse_chapter_number, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

API_URL = "https://api.mangadex.org"
AUTH_URL = "https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token"

# Global API limit is ~5 req/s; the at-home (image server) endpoint is 40 req/min.
_api_limiter = RateLimiter(rate=4, per_seconds=1)
_athome_limiter = RateLimiter(rate=35, per_seconds=60)


class MangaDexSource(DirectSource):
    name = "mangadex"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        client_id: str = "",
        client_secret: str = "",
        username: str = "",
        password: str = "",
        language: str = "en",
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True
        )
        self.configure(client_id, client_secret, username, password, language)

    def configure(
        self, client_id: str, client_secret: str, username: str, password: str, language: str = "en"
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._language = language or "en"
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret and self._username and self._password)

    async def _ensure_token(self) -> None:
        if not self.has_credentials:
            return  # anonymous access still works for search/feed, limited for images
        if self._access_token and time.time() < self._token_expires_at - 30:
            return
        if self._refresh_token:
            form = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        else:
            form = {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        resp = await self._client.post(AUTH_URL, data=form)
        if resp.status_code != 200 and form["grant_type"] == "refresh_token":
            # refresh token expired — retry with password grant
            self._refresh_token = None
            await self._ensure_token()
            return
        resp.raise_for_status()
        token = resp.json()
        self._access_token = token["access_token"]
        self._refresh_token = token.get("refresh_token")
        self._token_expires_at = time.time() + int(token.get("expires_in", 900))

    async def _get(self, path: str, params: dict | None = None, athome: bool = False) -> dict:
        await self._ensure_token()
        headers = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        resp = await rl_request(
            self._client, "GET", f"{API_URL}{path}",
            limiter=_athome_limiter if athome else _api_limiter,
            params=params, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _pick_title(attrs: dict) -> tuple[str, list[str]]:
        title_map = attrs.get("title") or {}
        title = title_map.get("en") or next(iter(title_map.values()), "Unknown")
        alts = []
        for alt in attrs.get("altTitles") or []:
            for value in alt.values():
                if value and value != title:
                    alts.append(value)
        return title, alts

    async def search_series(self, query: str) -> list[SourceSeries]:
        data = await self._get(
            "/manga",
            params={
                "title": query,
                "limit": 10,
                "contentRating[]": ["safe", "suggestive", "erotica"],
                "order[relevance]": "desc",
            },
        )
        results = []
        for manga in data.get("data", []):
            title, alts = self._pick_title(manga.get("attributes") or {})
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=manga["id"],
                    title=title,
                    alt_titles=alts,
                    url=f"https://mangadex.org/title/{manga['id']}",
                )
            )
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        chapters: dict[float, SourceChapter] = {}
        offset = 0
        while True:
            data = await self._get(
                f"/manga/{external_id}/feed",
                params={
                    "limit": 500,
                    "offset": offset,
                    "translatedLanguage[]": [self._language],
                    "order[chapter]": "asc",
                    "contentRating[]": ["safe", "suggestive", "erotica"],
                    "includeExternalUrl": 0,  # skip chapters hosted off-site (unfetchable)
                },
            )
            for ch in data.get("data", []):
                attrs = ch.get("attributes") or {}
                raw_number = attrs.get("chapter")
                number = (
                    parse_chapter_number(raw_number or "")
                    if raw_number is not None
                    else None
                )
                if number is None:
                    # oneshots / unnumbered specials → chapter 0
                    number = 0.0
                vol_raw = attrs.get("volume")
                try:
                    volume = int(float(vol_raw)) if vol_raw else None
                except ValueError:
                    volume = None
                # First scanlation group wins per chapter number (feed is ordered)
                if number not in chapters:
                    chapters[number] = SourceChapter(
                        source_name=self.name,
                        external_id=ch["id"],
                        number=number,
                        volume=volume,
                        title=attrs.get("title") or "",
                        language=self._language,
                        url=f"https://mangadex.org/chapter/{ch['id']}",
                    )
            total = data.get("total", 0)
            offset += 500
            if offset >= total:
                break
        return sorted(chapters.values(), key=lambda c: c.number)

    async def get_volume_map(self, external_id: str) -> dict[float, int]:
        """Volume assignments from the aggregate endpoint, across all
        languages — it covers chapters the feed can't serve (e.g. titles
        whose English chapters are external MangaPlus links)."""
        data = await self._get(f"/manga/{external_id}/aggregate")
        volumes = data.get("volumes")
        if not isinstance(volumes, dict):
            return {}
        mapping: dict[float, int] = {}
        for vol_key, vol in volumes.items():
            try:
                vol_num = int(float(vol_key))
            except (TypeError, ValueError):
                continue  # "none" bucket — unassigned chapters
            if vol_num < 1:
                continue  # junk "volume 0" entries (real specials sit in "none")
            chapters = vol.get("chapters")
            if not isinstance(chapters, dict):
                continue
            for ch_key in chapters:
                number = parse_chapter_number(ch_key or "")
                if number is not None:
                    mapping[number] = vol_num
        return mapping

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        data = await self._get(f"/at-home/server/{chapter_external_id}", athome=True)
        base = data["baseUrl"]
        chapter = data["chapter"]
        chapter_hash = chapter["hash"]
        return [f"{base}/data/{chapter_hash}/{page}" for page in chapter["data"]]


source = MangaDexSource()
