from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SeriesMetadata:
    provider: str
    provider_id: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    description: str = ""
    status: str = "unknown"  # matches models.SeriesStatus values
    year: int | None = None
    cover_url: str = ""
    banner_url: str = ""
    genres: list[str] = field(default_factory=list)
    total_chapters: int | None = None
    total_volumes: int | None = None


class MetadataProvider(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[SeriesMetadata]: ...

    @abstractmethod
    async def get_series(self, provider_id: str) -> SeriesMetadata | None: ...
