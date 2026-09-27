"""Creating library series from a metadata id — shared by the Add dialog,
bulk library import, and import lists, so every path builds the same row
and kicks off the same background refresh."""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .metadata.anilist import provider as anilist
from .metadata.mangaupdates import provider as mangaupdates
from .models import RootFolder, Series, SeriesFolder, SeriesStatus
from .titles import split_alt_titles, unique_titles
from .util import normalize_title, sanitize_filename

log = logging.getLogger(__name__)


class SeriesExists(Exception):
    def __init__(self, series_id: int) -> None:
        super().__init__("Series already in library")
        self.series_id = series_id


class MetadataNotFound(Exception):
    pass


@dataclass
class AddOptions:
    root_folder_id: int | None
    monitored: bool = True
    monitor_mode: str = "all"
    monitor_from: float | None = None
    english_title: str = ""
    alt_titles: list[str] = field(default_factory=list)
    folder_name: str = ""
    # the folder was chosen by the user — scans must not re-adopt another
    folder_pinned: bool = False
    extra_folders: list[str] = field(default_factory=list)


async def normalize_folder_name(session: AsyncSession, series: Series, folder_name: str) -> str:
    """Store the folder relative to the series' root folder when the given path
    is under it (so it survives a root-folder move); otherwise keep as given."""
    folder_name = folder_name.strip()
    if series.root_folder_id is not None and folder_name.startswith("/"):
        root = await session.get(RootFolder, series.root_folder_id)
        if root is not None:
            try:
                return str(Path(folder_name).relative_to(root.path))
            except ValueError:
                pass  # outside the root — keep absolute
    return folder_name.strip("/") if not folder_name.startswith("/") else folder_name


def title_keys(titles: Iterable[str]) -> set[str]:
    return {n for t in titles if (n := normalize_title(t))}


class LibraryIndex:
    """Which library series a metadata entry refers to: by provider id first,
    then by any shared normalized title — a series added from MangaUpdates
    has no AniList id, and an AniList-sourced list entry has no MangaUpdates
    id, so ids alone would add the same manga twice. Built once so matching
    a long list costs one pass over the library, not one per entry.

    A title match only counts when the ids can't tell the two apart: a
    library series with a *different* id from the same provider is another
    manga that happens to share the title."""

    def __init__(self) -> None:
        self._by_id: dict[tuple[str, int], int] = {}
        # title key → [(series id, anilist id, mangaupdates id)]
        self._by_title: dict[str, list[tuple[int, int | None, int | None]]] = {}

    @classmethod
    async def load(cls, session: AsyncSession) -> "LibraryIndex":
        index = cls()
        rows = await session.execute(
            select(Series.id, Series.anilist_id, Series.mangaupdates_id,
                   Series.title, Series.alt_titles)
        )
        for series_id, anilist_id, mu_id, title, alt_titles in rows.all():
            index.add(series_id, anilist_id, mu_id, [title, *split_alt_titles(alt_titles)])
        return index

    def add(self, series_id: int, anilist_id: int | None, mangaupdates_id: int | None,
            titles: Iterable[str]) -> None:
        if anilist_id is not None:
            self._by_id[("anilist", anilist_id)] = series_id
        if mangaupdates_id is not None:
            self._by_id[("mangaupdates", mangaupdates_id)] = series_id
        for key in title_keys(titles):
            self._by_title.setdefault(key, []).append((series_id, anilist_id, mangaupdates_id))

    def find(self, anilist_id: int | None = None, mangaupdates_id: int | None = None,
             titles: Iterable[str] = ()) -> int | None:
        for provider, pid in (("anilist", anilist_id), ("mangaupdates", mangaupdates_id)):
            if pid is not None and (found := self._by_id.get((provider, pid))) is not None:
                return found
        for key in sorted(title_keys(titles)):
            for series_id, lib_anilist, lib_mu in self._by_title.get(key, ()):
                if anilist_id is not None and lib_anilist is not None:
                    continue  # both on AniList, different entries
                if mangaupdates_id is not None and lib_mu is not None:
                    continue  # both on MangaUpdates, different entries
                return series_id
        return None


async def create_series(
    session: AsyncSession,
    opts: AddOptions,
    *,
    anilist_id: int | None = None,
    mangaupdates_id: int | None = None,
) -> Series:
    """Add a series from exactly one provider id and commit it. Raises
    SeriesExists when that id is already in the library and MetadataNotFound
    when the provider doesn't know it. The caller starts the refresh."""
    if (anilist_id is None) == (mangaupdates_id is None):
        raise ValueError("Provide exactly one of mangaupdates_id or anilist_id")
    if mangaupdates_id is not None:
        id_filter = Series.mangaupdates_id == mangaupdates_id
        provider, provider_id = mangaupdates, mangaupdates_id
    else:
        id_filter = Series.anilist_id == anilist_id
        provider, provider_id = anilist, anilist_id
    existing = (await session.execute(select(Series.id).where(id_filter))).scalar_one_or_none()
    if existing is not None:
        raise SeriesExists(existing)
    meta = await provider.get_series(str(provider_id))
    if meta is None:
        raise MetadataNotFound(f"{provider.name} series not found")
    alt_titles = unique_titles([*meta.alt_titles, opts.english_title, *opts.alt_titles])
    series = Series(
        anilist_id=anilist_id,
        mangaupdates_id=mangaupdates_id,
        title=meta.title,
        sort_title=meta.title.lower(),
        alt_titles="\n".join(alt_titles),
        description=meta.description,
        status=SeriesStatus(meta.status),
        year=meta.year,
        cover_url=meta.cover_url,
        banner_url=meta.banner_url,
        genres=",".join(meta.genres),
        total_chapters=meta.total_chapters,
        total_volumes=meta.total_volumes,
        monitored=opts.monitored,
        monitor_mode=opts.monitor_mode,
        monitor_from=opts.monitor_from if opts.monitor_mode == "from_chapter" else None,
        root_folder_id=opts.root_folder_id,
        folder_name=sanitize_filename(meta.title),
        folder_pinned=opts.folder_pinned,
    )
    if opts.folder_name.strip():
        series.folder_name = await normalize_folder_name(session, series, opts.folder_name)
    session.add(series)
    for extra in opts.extra_folders:
        path = await normalize_folder_name(session, series, extra)
        if path and path != series.folder_name:
            series.extra_folders.append(SeriesFolder(path=path))
    await session.commit()
    await session.refresh(series)
    return series


def start_refreshes(
    series_ids: list[int], grab_missing: bool = False, only_monitored: bool = False
) -> None:
    """Link sources and fetch chapters for newly added (or bulk-refreshed)
    series in the background. One series refreshes right away (what the Add
    dialog wants); a batch runs one at a time so importing a whole library
    doesn't fire hundreds of concurrent searches at every source."""
    from .jobs.tasks import REFRESHING, refresh_series_full

    if not series_ids:
        return
    # pre-mark so series pages show the work in progress before the task's
    # first tick
    REFRESHING.update(series_ids)

    async def run() -> None:
        for series_id in series_ids:
            await refresh_series_full(
                series_id, grab_missing=grab_missing, only_monitored=only_monitored
            )

    asyncio.get_running_loop().create_task(run())
