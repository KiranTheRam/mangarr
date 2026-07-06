from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from ..util import RateLimiter, rl_request


@dataclass
class SourceSeries:
    source_name: str
    external_id: str
    title: str
    url: str = ""
    alt_titles: list[str] = field(default_factory=list)


@dataclass
class SourceChapter:
    source_name: str
    external_id: str  # what get_pages needs to fetch this chapter
    number: float
    volume: int | None = None
    title: str = ""
    language: str = "en"
    url: str = ""


@dataclass
class TorrentRelease:
    source_name: str
    title: str
    magnet: str
    url: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0


class DirectSource(ABC):
    """A site we can search, list chapters on, and pull page images from."""

    name: str
    # per-source proactive limiter for page-image fetches (separate from the
    # HTML/API limiter so images can be pulled a bit faster). Sources override.
    image_limiter: RateLimiter | None = None

    @abstractmethod
    async def search_series(self, query: str) -> list[SourceSeries]: ...

    @abstractmethod
    async def list_chapters(self, external_id: str) -> list[SourceChapter]: ...

    @abstractmethod
    async def get_pages(self, chapter_external_id: str) -> list[str]:
        """Returns ordered page image URLs."""

    def image_headers(self) -> dict:
        """Extra headers image CDNs need (e.g. a Referer). Override per source."""
        return {}

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        """Fetch one page image — rate-limited, with reactive back-off on 429
        and retry on transient errors (shared by all sources)."""
        resp = await rl_request(
            client, "GET", url, limiter=self.image_limiter, headers=self.image_headers()
        )
        resp.raise_for_status()
        return resp.content

    async def get_volume_map(self, external_id: str) -> dict[float, int]:
        """chapter number → volume number, for sources that know volume
        assignments even for chapters they can't serve. Optional."""
        return {}


class TorrentIndexer(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str) -> list[TorrentRelease]: ...
