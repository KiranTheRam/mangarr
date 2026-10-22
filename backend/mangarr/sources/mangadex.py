import asyncio
import logging
import time

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, parse_chapter_number, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

log = logging.getLogger(__name__)

API_URL = "https://api.mangadex.org"
AUTH_URL = "https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token"

# Global API limit is ~5 req/s; the at-home (image server) endpoint is 40 req/min.
_api_limiter = RateLimiter(rate=4, per_seconds=1)
_athome_limiter = RateLimiter(rate=35, per_seconds=60)

# After a failed login the source carries on anonymously (search, feeds and
# at-home all work without an account) and leaves the auth server alone for
# this long. 15 minutes is the access-token lifetime MangaDex issues, so a
# recovered auth server is back in use within one token cycle, while a wrong
# password costs at most four login attempts an hour instead of one per API
# call. Saving different credentials clears it (see configure()).
AUTH_RETRY_COOLDOWN = 15 * 60.0


def _auth_failure_summary(exc: Exception) -> str:
    """A short reason for a failed login, safe to log (never the form data)."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")[:200]
        return f"HTTP {exc.response.status_code}" + (f": {detail}" if detail else "")
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

# volume assignments change rarely; update_chapters() asks for them every
# monitor cycle, so cache like the other volume-map sources do
VOLUME_MAP_CACHE_TTL = 6 * 3600.0


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
            headers={"User-Agent": USER_AGENT}, timeout=60, trust_env=False,
            follow_redirects=True,
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at = 0.0
        self._auth_lock = asyncio.Lock()
        self._auth_retry_at = 0.0  # time.monotonic() before which no login is tried
        self._auth_error = ""  # why the last login failed
        self._map_cache: dict[str, tuple[float, dict[float, int]]] = {}
        self.configure(client_id, client_secret, username, password, language)

    def configure(
        self, client_id: str, client_secret: str, username: str, password: str, language: str = "en"
    ) -> None:
        # configure() runs on every settings read (each request/job), so it
        # must not discard a live session unless the credentials changed —
        # otherwise every call re-does a password grant against the auth API
        creds = (client_id, client_secret, username, password)
        if creds != getattr(self, "_creds", None):
            self._creds = creds
            self._access_token = None
            self._refresh_token = None
            self._token_expires_at = 0.0
            # new credentials get an immediate login, not the cooldown left
            # behind by the old ones
            self._auth_retry_at = 0.0
            self._auth_error = ""
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._language = language or "en"

    @property
    def has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret and self._username and self._password)

    def _token_is_fresh(self) -> bool:
        return bool(self._access_token) and time.time() < self._token_expires_at - 30

    def _auth_cooling_down(self) -> bool:
        return time.monotonic() < self._auth_retry_at

    async def _ensure_token(self) -> None:
        """Log in when an account is configured and the token is stale.

        A failed login never fails the request: the source drops to anonymous
        access and waits AUTH_RETRY_COOLDOWN before trying the auth server
        again."""
        if not self.has_credentials:
            return  # anonymous access still works for search/feed, limited for images
        if self._token_is_fresh() or self._auth_cooling_down():
            return
        # one login at a time: concurrent callers wait here, then reuse the
        # new token (or the failure) instead of each hitting the auth server
        async with self._auth_lock:
            if self._token_is_fresh() or self._auth_cooling_down():
                return
            creds = self._creds
            try:
                token = await self._request_token()
                access_token = token["access_token"]
                expires_in = int(token.get("expires_in", 900))
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                if self._creds != creds:
                    return  # credentials changed mid-login; the next call tries the new ones
                # an expired bearer would be refused outright, so go anonymous
                self._access_token = None
                self._auth_error = _auth_failure_summary(exc)
                self._auth_retry_at = time.monotonic() + AUTH_RETRY_COOLDOWN
                log.warning(
                    "MangaDex login failed (%s); continuing anonymously, "
                    "next login attempt in %d min",
                    self._auth_error, AUTH_RETRY_COOLDOWN // 60,
                )
                return
            if self._creds != creds:
                return  # the token belongs to credentials that were just replaced
            if self._auth_error:
                log.info("MangaDex login succeeded; using the account again")
                self._auth_error = ""
            self._access_token = access_token
            self._refresh_token = token.get("refresh_token")
            self._token_expires_at = time.time() + expires_in

    async def _request_token(self) -> dict:
        """The refresh grant while a refresh token is held, else (or once it
        has expired) the password grant. Raises if the login fails."""
        if self._refresh_token:
            resp = await self._post_auth({
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            })
            if resp.status_code == 200:
                return resp.json()
            # refresh token expired — retry with password grant
            self._refresh_token = None
        resp = await self._post_auth({
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        resp.raise_for_status()
        return resp.json()

    async def _post_auth(self, form: dict) -> httpx.Response:
        # logins count against the same MangaDex budget as API calls
        await _api_limiter.acquire()
        return await self._client.post(AUTH_URL, data=form)

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

    async def library_manga(self, statuses: list[str]) -> list[dict]:
        """Manga records in the account's library with one of `statuses`
        (reading, plan_to_read, completed, on_hold, dropped, re_reading).
        Needs the account credentials — the library is private."""
        if not self.has_credentials:
            raise RuntimeError("MangaDex account credentials are not configured")
        await self._ensure_token()
        if not self._access_token:
            # anonymous requests only get a 401 here, so say why up front
            raise RuntimeError(f"MangaDex login failed ({self._auth_error or 'no token'})")
        data = await self._get("/manga/status")
        wanted = set(statuses)
        ids = [mid for mid, status in (data.get("statuses") or {}).items() if status in wanted]
        records: list[dict] = []
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            # the default content filter would silently drop mature titles
            # the user explicitly put in their library
            page = await self._get("/manga", params={
                "ids[]": batch, "limit": len(batch),
                "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
            })
            records.extend(page.get("data") or [])
        return records

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
        chapters: list[SourceChapter] = []
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
                    "includes[]": ["scanlation_group"],
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
                # every group's copy is kept, in feed order (the first one
                # is the default pick when the series prefers no group)
                chapters.append(SourceChapter(
                    source_name=self.name,
                    external_id=ch["id"],
                    number=number,
                    volume=volume,
                    title=attrs.get("title") or "",
                    language=self._language,
                    url=f"https://mangadex.org/chapter/{ch['id']}",
                    group=_group_names(ch),
                ))
            total = data.get("total", 0)
            offset += 500
            if offset >= total:
                break
        # stable: copies of one chapter keep their feed order
        return sorted(chapters, key=lambda c: c.number)

    async def get_volume_map(self, external_id: str) -> dict[float, int]:
        """Volume assignments from the aggregate endpoint, across all
        languages — it covers chapters the feed can't serve (e.g. titles
        whose English chapters are external MangaPlus links)."""
        cached = self._map_cache.get(external_id)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
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
        self._map_cache[external_id] = (time.monotonic() + VOLUME_MAP_CACHE_TTL, mapping)
        return dict(mapping)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        data = await self._get(f"/at-home/server/{chapter_external_id}", athome=True)
        base = data["baseUrl"]
        chapter = data["chapter"]
        chapter_hash = chapter["hash"]
        return [f"{base}/data/{chapter_hash}/{page}" for page in chapter["data"]]


def _group_names(chapter: dict) -> str:
    """Scanlation group names from a feed entry's expanded relationships;
    a joint release lists each group."""
    names = [
        ((rel.get("attributes") or {}).get("name") or "").strip()
        for rel in chapter.get("relationships") or []
        if isinstance(rel, dict) and rel.get("type") == "scanlation_group"
    ]
    return " & ".join(name for name in names if name)


source = MangaDexSource()
