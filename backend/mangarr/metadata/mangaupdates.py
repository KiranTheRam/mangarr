"""MangaUpdates (api.mangaupdates.com) metadata provider — the primary one.

Its community tracks scanlation releases per chapter, so `latest_chapter`
and the volume count in the status string stay current for ongoing series
where AniList lags (AniList only fills chapter/volume totals once a series
finishes). Release records also carry occasional volume tags; those become
anchors for the chapter→volume map built in mangarr.volumes.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import MetadataProvider, SeriesMetadata

API_URL = "https://api.mangaupdates.com/v1"

_limiter = RateLimiter(rate=1, per_seconds=1)

# status strings look like "15 Volumes (Hiatus)", "Complete", "Ongoing"
_VOLUMES_RE = re.compile(r"(\d+)\s+Volumes?", re.I)
_STATUS_WORDS = [
    ("cancel", "cancelled"),
    ("dropped", "cancelled"),
    ("hiatus", "hiatus"),
    ("complete", "finished"),
    ("ongoing", "releasing"),
]
# release chapter/volume tokens: "147", "12.5", "365-366"
_NUM_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*$")
# markdown links in descriptions → keep just the text
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

RELEASE_PAGES = 10  # 40 releases/page (page size is fixed server-side)
RELEASE_CACHE_TTL = 6 * 3600.0


@dataclass
class ReleaseData:
    """What the release feed knows: which chapters exist (with dates) and
    which chapters are tagged with a volume."""

    chapters: dict[float, datetime | None] = field(default_factory=dict)
    volume_anchors: dict[float, int] = field(default_factory=dict)


def parse_status(status: str | None, completed: bool | None) -> str:
    if completed:
        return "finished"
    for word, value in _STATUS_WORDS:
        if word in (status or "").lower():
            return value
    return "unknown"


def parse_total_volumes(status: str | None) -> int | None:
    m = _VOLUMES_RE.search(status or "")
    return int(m.group(1)) if m else None


def parse_number_token(token: str | None) -> list[float]:
    """Chapter numbers a release token covers: '147' → [147.0],
    '365-366' → [365.0, 366.0]. Unparsable ('Oneshot', 'Extra') → []."""
    m = _NUM_RANGE_RE.match(token or "")
    if not m:
        return []
    lo = float(m.group(1))
    if m.group(2) is None:
        return [lo]
    hi = float(m.group(2))
    if hi < lo or hi - lo > 50:  # backwards or absurdly wide → distrust
        return []
    if lo != int(lo) or hi != int(hi):
        return [lo, hi]  # decimal endpoints: keep just those
    return [float(n) for n in range(int(lo), int(hi) + 1)]


class MangaUpdatesProvider(MetadataProvider):
    name = "mangaupdates"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30
        )
        self._release_cache: dict[int, tuple[float, ReleaseData]] = {}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await rl_request(
            self._client, method, f"{API_URL}{path}", limiter=_limiter, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def _to_metadata(self, record: dict) -> SeriesMetadata:
        image = (record.get("image") or {}).get("url") or {}
        year_raw = str(record.get("year") or "")
        year = int(year_raw[:4]) if year_raw[:4].isdigit() else None
        description = record.get("description") or ""
        description = _MD_LINK_RE.sub(r"\1", description)
        latest = record.get("latest_chapter")
        return SeriesMetadata(
            provider=self.name,
            provider_id=str(record["series_id"]),
            title=record.get("title") or "Unknown",
            alt_titles=[
                t for a in (record.get("associated") or [])
                if (t := a.get("title")) and t != record.get("title")
            ],
            description=description,
            status=parse_status(record.get("status"), record.get("completed")),
            year=year,
            cover_url=image.get("original") or image.get("thumb") or "",
            genres=[g["genre"] for g in (record.get("genres") or []) if g.get("genre")],
            total_chapters=int(latest) if latest else None,
            total_volumes=parse_total_volumes(record.get("status")),
        )

    async def search(self, query: str, limit: int = 20) -> list[SeriesMetadata]:
        data = await self._request("POST", "/series/search", json={"search": query})
        results = []
        for item in (data.get("results") or [])[:limit]:
            meta = self._to_metadata(item["record"])
            # hit_title is the associated title the query matched — search
            # records don't carry the full associated list, so keep it
            hit = item.get("hit_title")
            if hit and hit != meta.title and hit not in meta.alt_titles:
                meta.alt_titles.append(hit)
            results.append(meta)
        return results

    async def get_series(self, provider_id: str) -> SeriesMetadata | None:
        try:
            record = await self._request("GET", f"/series/{provider_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return self._to_metadata(record)

    async def get_release_data(self, series_id: int) -> ReleaseData:
        """Recent releases for the series (newest first, page size is a fixed
        40 server-side). Cached: the monitor loop calls this every cycle."""
        cached = self._release_cache.get(series_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        data = ReleaseData()
        for page in range(1, RELEASE_PAGES + 1):
            payload = await self._request(
                "POST", "/releases/search",
                json={"search": str(series_id), "search_type": "series", "page": page},
            )
            results = payload.get("results") or []
            for item in results:
                record = item.get("record") or {}
                numbers = parse_number_token(record.get("chapter"))
                released_at = _parse_date(record.get("release_date"))
                volumes = parse_number_token(record.get("volume"))
                volume = int(volumes[0]) if len(volumes) == 1 and volumes[0] >= 1 else None
                for number in numbers:
                    if number <= 0:
                        continue
                    prev = data.chapters.get(number)
                    if prev is None or (released_at and released_at < prev):
                        data.chapters[number] = released_at  # earliest release wins
                    if volume is not None:
                        data.volume_anchors.setdefault(number, volume)
            if len(results) < 40 or page * 40 >= int(payload.get("total_hits") or 0):
                break
        self._release_cache[series_id] = (time.monotonic() + RELEASE_CACHE_TTL, data)
        return data


def _parse_date(raw: str | None) -> datetime | None:
    try:
        return datetime.strptime(raw or "", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


provider = MangaUpdatesProvider()
