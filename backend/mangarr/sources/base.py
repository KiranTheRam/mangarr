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
class ChapterMetadata:
    """Metadata a source knows about a chapter without necessarily serving it.

    ``number`` is optional because printed tables of contents frequently list
    an "Extra" or "Bonus" by title and position but give it no canonical
    number.  Those rows are reconciled with decimal-numbered local chapters
    later, when the rest of the series provides enough context.
    """

    source_name: str
    number: float | None
    volume: int | None = None
    title: str = ""
    kind: str = "chapter"  # chapter | extra
    url: str = ""


@dataclass
class TorrentRelease:
    source_name: str
    title: str
    magnet: str
    url: str = ""
    torrent_url: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0


class DirectSource(ABC):
    """A site we can search, list chapters on, and pull page images from."""

    name: str
    # Configured at runtime by registry.apply_settings(). Only page/image
    # bytes use this; source API and metadata clients remain direct.
    content_proxy_enabled: bool = False
    content_proxy_url: str = ""
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

    async def get_chapter_metadata(self, external_id: str) -> list[ChapterMetadata]:
        """Optional title/volume metadata for chapters this source cannot
        necessarily serve.  Kept separate from ``list_chapters`` so an
        official catalogue or Wikipedia never appears as a download source.
        """
        return []


class TorrentIndexer(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str) -> list[TorrentRelease]: ...

    async def get_torrent_metadata(self, release: TorrentRelease) -> bytes:
        """Fetch a small .torrent file for coverage inspection, if offered."""
        return b""
