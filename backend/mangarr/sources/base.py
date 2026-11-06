from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..util import RateLimiter, rl_request


@dataclass
class SourceSeries:
    source_name: str
    external_id: str
    title: str
    url: str = ""
    alt_titles: list[str] = field(default_factory=list)
    cover_url: str = ""
    description: str = ""


@dataclass
class SourceChapter:
    source_name: str
    external_id: str  # what get_pages needs to fetch this chapter
    number: float
    volume: int | None = None
    title: str = ""
    language: str = "en"
    url: str = ""
    # scanlation group(s) behind this copy, when the source says. Sources
    # that carry several groups' copies of a chapter list every copy, in
    # their own preference order; automation.select_group_variants picks one.
    group: str = ""
    # When this release's early-access window ended, if the source says.
    public_since: datetime | None = None


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
    # Resolved btih identity, including HTTP endpoints that redirect to a
    # magnet and therefore have no downloadable metadata bytes to inspect.
    info_hash: str = ""


class DirectSource(ABC):
    """A site we can search, list chapters on, and pull page images from."""

    name: str
    # Fallbacks support standalone source instances. Registry settings are
    # task-local, even though the adapter objects themselves are singletons.
    _content_proxy_enabled: bool = False
    _content_proxy_url: str = ""

    @property
    def content_proxy_enabled(self) -> bool:
        from .routing import route_for

        route = route_for(self.name)
        return route.content_enabled if route else self._content_proxy_enabled

    @content_proxy_enabled.setter
    def content_proxy_enabled(self, value: bool) -> None:
        self._content_proxy_enabled = value

    @property
    def content_proxy_url(self) -> str:
        from .routing import route_for

        route = route_for(self.name)
        return route.content_url if route else self._content_proxy_url

    @content_proxy_url.setter
    def content_proxy_url(self, value: str) -> None:
        self._content_proxy_url = value

    # per-source proactive limiter for page-image fetches (separate from the
    # HTML/API limiter so images can be pulled a bit faster). Sources override.
    image_limiter: RateLimiter | None = None

    @abstractmethod
    async def search_series(self, query: str) -> list[SourceSeries]: ...

    async def get_series_details(self, external_id: str) -> SourceSeries | None:
        """Optional series metadata supplied by a download source."""
        return None

    @abstractmethod
    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        """Chapters the source serves. May hold several copies of one chapter
        number (one per scanlation group), best first."""

    # Catalog-only chapter lists need an explicit reader check before they
    # can be advertised as downloadable.
    requires_chapter_verification: bool = False

    async def verify_chapters(
        self, external_ids: list[str], *, max_checks: int | None = 60,
    ) -> set[str]:
        """Return the chapter IDs that can be offered as direct releases."""
        return set(external_ids)

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
