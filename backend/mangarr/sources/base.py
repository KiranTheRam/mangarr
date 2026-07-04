from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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

    @abstractmethod
    async def search_series(self, query: str) -> list[SourceSeries]: ...

    @abstractmethod
    async def list_chapters(self, external_id: str) -> list[SourceChapter]: ...

    @abstractmethod
    async def get_pages(self, chapter_external_id: str) -> list[str]:
        """Returns ordered page image URLs."""

    async def download_page(self, client, url: str) -> bytes:
        """Fetch one page image. Override for sources needing special headers."""
        resp = await client.get(url)
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
