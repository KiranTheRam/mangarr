"""Central access to configured sources, honoring settings (enabled flags,
credentials, priority order)."""

from sqlalchemy.ext.asyncio import AsyncSession

from .. import settings_service
from .base import DirectSource, TorrentIndexer
from .asura import source as asura_source
from .mangadex import source as mangadex_source
from .mangaplus import source as mangaplus_source
from .nyaa import indexer as nyaa_indexer
from .tcbscans import source as tcbscans_source
from .weebcentral import source as weebcentral_source
from .wikipedia import source as wikipedia_source

DIRECT_SOURCES: dict[str, DirectSource] = {
    mangaplus_source.name: mangaplus_source,
    mangadex_source.name: mangadex_source,
    weebcentral_source.name: weebcentral_source,
    tcbscans_source.name: tcbscans_source,
    asura_source.name: asura_source,
    wikipedia_source.name: wikipedia_source,
}
TORRENT_INDEXERS: dict[str, TorrentIndexer] = {
    nyaa_indexer.name: nyaa_indexer,
}
ALL_SOURCE_NAMES = [*DIRECT_SOURCES, *TORRENT_INDEXERS]


async def apply_settings(session: AsyncSession) -> dict[str, str]:
    """Push runtime settings into source instances; returns the settings dict."""
    values = await settings_service.get_all(session)
    mangadex_source.configure(
        client_id=values["mangadex_client_id"],
        client_secret=values["mangadex_client_secret"],
        username=values["mangadex_username"],
        password=values["mangadex_password"],
        language=values["mangadex_language"],
    )
    return values


def enabled_direct_sources(values: dict[str, str]) -> list[DirectSource]:
    order = [s.strip() for s in values["source_priority"].split(",") if s.strip()]
    sources = [
        DIRECT_SOURCES[name]
        for name in order
        if name in DIRECT_SOURCES and values.get(f"source_{name}_enabled") == "true"
    ]
    # include any enabled source missing from the priority string
    for name, src in DIRECT_SOURCES.items():
        if src not in sources and values.get(f"source_{name}_enabled") == "true":
            sources.append(src)
    return sources


def enabled_torrent_indexers(values: dict[str, str]) -> list[TorrentIndexer]:
    return [
        idx
        for name, idx in TORRENT_INDEXERS.items()
        if values.get(f"source_{name}_enabled") == "true"
    ]
