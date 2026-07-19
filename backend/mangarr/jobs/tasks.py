"""Background tasks: series refresh, source linking, grabbing, download
processing, qBittorrent sync, and the monitor loop."""

import asyncio
import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import notifications
from ..chapter_metadata import (
    apply_metadata_rows,
    apply_title,
    apply_volume,
    reconcile_decimal_volumes,
)
from ..db import session_scope
from ..download.direct import download_chapter_to_cbz
from ..download.qbittorrent import QbtClient
from ..library.importer import import_torrent_payload
from ..library.matcher import find_media_files
from ..library.naming import chapter_path
from ..metadata.anilist import provider as anilist
from ..metadata.mangaupdates import provider as mangaupdates
from ..models import (
    Chapter,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Series,
    SeriesStatus,
)
from ..sources import registry
from ..sources.base import DirectSource, SourceChapter
from ..titles import plausible_title_match, split_alt_titles, title_queries
from ..torrent_selection import select_best_torrent
from ..util import normalize_title
from ..volumes import distribute_over_disk_volumes, select_labeled_volume_map

log = logging.getLogger(__name__)

BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")

# error text marking a download the user cancelled — it must not count as a
# source failure (which would stop the monitor from ever retrying that
# chapter on that source)
REMOVED_BY_USER = "removed by user"

# a failed grab blocks that (chapter, source) pair, but not forever — sources
# fix broken chapters, so retry after a while (one request per window is cheap)
FAILED_GRAB_RETRY_AFTER = timedelta(days=7)

# metadata (status, chapter/volume totals) goes stale without this; refreshed
# lazily during monitor passes rather than all at once
METADATA_REFRESH_AFTER = timedelta(days=7)

# a direct download that stops making progress (wedged connection, hung
# source, broken DB session) must not occupy the single queue worker forever:
# it blocks every queued download behind it while looking merely slow in the
# UI. Cancel it and fail visibly instead. Generous because a single page is
# allowed up to 3 attempts x 120s before it fails on its own.
DIRECT_STALL_TIMEOUT = timedelta(minutes=15)

# one monitor/refresh pass fetches each source's chapter list once and shares
# it between update_chapters() and grab_missing_chapters()
ChapterListCache = dict[tuple[str, str], list[SourceChapter]]


class DownloadCancelled(RuntimeError):
    """Raised inside the direct worker when the user removes the queue item."""


async def _raise_if_download_removed(session: AsyncSession, download_id: int) -> None:
    row = (
        await session.execute(
            select(Download.status, Download.error).where(Download.id == download_id)
        )
    ).one_or_none()
    if row and row[0] == DownloadStatus.FAILED and row[1] == REMOVED_BY_USER:
        raise DownloadCancelled(REMOVED_BY_USER)


async def _list_chapters_cached(
    src: DirectSource, external_id: str, cache: ChapterListCache | None
) -> list[SourceChapter]:
    key = (src.name, external_id)
    if cache is not None and key in cache:
        return cache[key]
    chapters = await src.list_chapters(external_id)
    if cache is not None:
        cache[key] = chapters
    return chapters


# ---------------------------------------------------------------- metadata

async def refresh_series_metadata(session: AsyncSession, series: Series) -> None:
    """Refresh from the series' metadata provider. MangaUpdates is primary:
    identity fields (title/description/cover/…) come from the provider the
    series was added with, so an AniList-era library keeps its titles, while
    progress fields (status, chapter/volume totals) prefer MangaUpdates —
    AniList only fills totals once a series finishes."""
    if series.mangaupdates_id is None:
        try:
            await link_mangaupdates(series)
        except Exception as exc:
            log.warning("mangaupdates link failed for %r: %s", series.title, exc)

    identity_from_mu = series.anilist_id is None and series.mangaupdates_id is not None
    mu_meta = None
    if series.mangaupdates_id is not None:
        mu_meta = await mangaupdates.get_series(str(series.mangaupdates_id))
    meta = mu_meta if identity_from_mu else (
        await anilist.get_series(str(series.anilist_id)) if series.anilist_id else None
    )
    if meta is not None:
        series.title = meta.title
        series.alt_titles = "\n".join(meta.alt_titles)
        series.description = meta.description
        series.status = SeriesStatus(meta.status)
        series.year = meta.year
        series.cover_url = meta.cover_url
        series.banner_url = meta.banner_url or series.banner_url
        series.genres = ",".join(meta.genres)
        series.total_chapters = meta.total_chapters
        series.total_volumes = meta.total_volumes
    if mu_meta is not None:
        # progress data: MangaUpdates wins whenever it knows more
        if mu_meta.status != "unknown":
            series.status = SeriesStatus(mu_meta.status)
        if mu_meta.total_chapters:
            series.total_chapters = max(mu_meta.total_chapters, series.total_chapters or 0)
        if mu_meta.total_volumes:
            series.total_volumes = max(mu_meta.total_volumes, series.total_volumes or 0)
        # MangaUpdates' associated titles help cross-source matching
        if meta is None or meta is not mu_meta:
            known = set(series.alt_titles.split("\n"))
            extra = [t for t in mu_meta.alt_titles if t and t not in known]
            series.alt_titles = "\n".join(filter(None, [series.alt_titles, *extra]))
    series.metadata_refreshed_at = datetime.now(timezone.utc)
    await session.commit()


def _metadata_is_stale(series: Series) -> bool:
    if series.metadata_refreshed_at is None:
        return True
    refreshed = series.metadata_refreshed_at
    if refreshed.tzinfo is None:  # SQLite returns naive datetimes
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - refreshed > METADATA_REFRESH_AFTER


async def link_mangaupdates(series: Series) -> bool:
    """Find the series on MangaUpdates by title (same matching philosophy as
    link_sources) and stamp its id. Returns True when linked."""
    titles = _titles_of(series)
    wanted = {nt for t in titles if (nt := normalize_title(t))}
    for query in titles[:4]:
        if not normalize_title(query):
            continue
        for cand in await mangaupdates.search(query, limit=10):
            cand_titles = {
                n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))
            }
            if wanted & cand_titles:
                series.mangaupdates_id = int(cand.provider_id)
                log.info("Linked %r to mangaupdates:%s (%r)",
                         series.title, cand.provider_id, cand.title)
                return True
    return False


# ---------------------------------------------------------- source linking

def _titles_of(series: Series) -> list[str]:
    return title_queries(series.title, split_alt_titles(series.alt_titles))


# (series_id, source_name) → monotonic time before which auto-linking should
# not retry a source that yielded no match. Searching every source for every
# unlinkable series each monitor cycle is pure waste; the map is in-memory,
# so a restart (or an explicit user refresh, which bypasses it) retries.
LINK_RETRY_AFTER_SECONDS = 24 * 3600.0
_link_retry_at: dict[tuple[int, str], float] = {}


async def link_sources(
    session: AsyncSession, series: Series, values: dict[str, str],
    respect_backoff: bool = False,
) -> None:
    """Auto-match the series on every enabled direct source it isn't linked to.

    `respect_backoff=True` (the scheduled monitor) skips sources that recently
    yielded no match; user-driven refreshes pass False to always retry."""
    linked = {sl.source_name for sl in series.source_links}
    titles = _titles_of(series)
    # normalized titles that are empty (e.g. a title written only in CJK) must
    # not be used for matching — an empty string is a prefix/substring of
    # everything and would match the first result of any catalog
    wanted = {nt for t in titles if (nt := normalize_title(t))}
    for src in registry.enabled_direct_sources(values):
        if src.name in linked:
            continue
        backoff_key = (series.id, src.name)
        if respect_backoff and time.monotonic() < _link_retry_at.get(backoff_key, 0.0):
            continue
        match = None
        searched = False
        # try each known title variant until the source yields a match
        for query in titles[:4]:
            nq = normalize_title(query)
            if not nq:
                continue
            try:
                candidates = await src.search_series(query)
            except Exception as exc:
                log.warning("source %s search failed for %r: %s", src.name, query, exc)
                break
            searched = True
            for cand in candidates:
                cand_titles = {n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))}
                if wanted & cand_titles:
                    match = cand
                    break
            if match is None and candidates:
                # fall back to the top result only when its title plausibly IS
                # this series (spacing variants etc.) — a bare shared prefix
                # must not link "Berserk" to "Berserk of Gluttony"
                top = candidates[0]
                if plausible_title_match(top.title, query):
                    match = top
            if match:
                break
        if match is None and searched:
            _link_retry_at[backoff_key] = time.monotonic() + LINK_RETRY_AFTER_SECONDS
        if match:
            _link_retry_at.pop(backoff_key, None)
            from ..models import SeriesSourceLink

            # append via the relationship so the in-memory collection is
            # current for update_chapters() in the same pass
            series.source_links.append(
                SeriesSourceLink(
                    source_name=src.name,
                    external_id=match.external_id,
                    external_title=match.title,
                    external_url=match.url,
                )
            )
            log.info("Linked %r to %s:%s (%r)", series.title, src.name, match.external_id, match.title)
    await session.commit()


# ------------------------------------------------------------- chapter sync

async def update_chapters(
    session: AsyncSession, series: Series, values: dict[str, str],
    chapter_cache: ChapterListCache | None = None,
) -> int:
    """Union chapter lists from linked sources into the DB. Returns # new."""
    existing = {c.number: c for c in series.chapters}
    added = 0
    links = {sl.source_name: sl for sl in series.source_links}
    enabled_sources = registry.enabled_direct_sources(values)
    claimable_sources = {src.name for src in enabled_sources if src.name in links}
    availability = {
        number: {
            name
            for name in (chapter.available_sources or "").split(",")
            if name in claimable_sources
        }
        for number, chapter in existing.items()
    }
    for src in enabled_sources:
        link = links.get(src.name)
        if link is None:
            continue
        try:
            source_chapters = await _list_chapters_cached(src, link.external_id, chapter_cache)
        except Exception as exc:
            log.warning("chapter list failed on %s for %r: %s", src.name, series.title, exc)
            continue
        # This source answered successfully, so its previous availability
        # claims can be replaced by the fresh list.  A transient source error
        # above deliberately preserves the last known state instead.
        for source_names in availability.values():
            source_names.discard(src.name)
        for sc in source_chapters:
            ch = existing.get(sc.number)
            if ch is None:
                ch = Chapter(
                    number=sc.number,
                    monitored=series.monitored,
                )
                apply_volume(ch, sc.volume, sc.source_name)
                apply_title(ch, sc.title, sc.source_name, series.title)
                # append via the relationship so series.chapters is current for
                # the scan/backfill later in this same pass
                series.chapters.append(ch)
                existing[sc.number] = ch
                availability[sc.number] = set()
                added += 1
            else:
                apply_volume(ch, sc.volume, sc.source_name)
                apply_title(ch, sc.title, sc.source_name, series.title)
            availability.setdefault(sc.number, set()).add(src.name)

    # MangaUpdates tracks releases even when no direct source serves them
    # yet (or ever) — add those chapters so Wanted reflects reality
    if series.mangaupdates_id is not None:
        try:
            release_data = await mangaupdates.get_release_data(series.mangaupdates_id)
        except Exception as exc:
            log.warning("mangaupdates releases failed for %r: %s", series.title, exc)
            release_data = None
        if release_data:
            for number, released_at in sorted(release_data.chapters.items()):
                ch = existing.get(number)
                if ch is None:
                    ch = Chapter(
                        number=number,
                        monitored=series.monitored,
                        released_at=released_at,
                    )
                    series.chapters.append(ch)
                    existing[number] = ch
                    availability[number] = set()
                    added += 1
                elif ch.released_at is None and released_at is not None:
                    ch.released_at = released_at

    # Backfill volume numbers for chapters that came from sources without
    # volume data, and add placeholder chapter rows for explicit volume-map
    # chapters that fill gaps between known chapters. This covers licensed
    # series where MangaDex aggregate knows chapter numbers/volumes, but the
    # feed has no readable entries because chapters are external or removed.
    needs_volume_map = (
        added
        or any(c.volume is None for c in existing.values())
        or _has_internal_number_gap(existing)
    )
    if needs_volume_map:
        _, volume_map, map_sources = await fetch_volume_map(series, values)
        added += _add_volume_map_gap_chapters(series, existing, volume_map, map_sources)
        # refine (and its decimal reconciliation) adds inferred entries the
        # sources never claimed — those fall back to the "disk-inferred" label
        volume_map = refine_volume_map_with_disk(series, volume_map)
        for number, ch in existing.items():
            if number in volume_map:
                apply_volume(ch, volume_map[number],
                             map_sources.get(number, "disk-inferred"))

    # Merge field-level metadata independently of chapter availability. This
    # is where Wikipedia supplies titles and VIZ supplies official volumes.
    # The fetches are independent (and usually cache hits), so run them
    # concurrently; results apply in source order for determinism.
    metadata_sources = [
        (src, links[src.name]) for src in enabled_sources
        if src.name in links
    ]
    results = await asyncio.gather(
        *(src.get_chapter_metadata(link.external_id) for src, link in metadata_sources),
        return_exceptions=True,
    )
    for (src, _), rows in zip(metadata_sources, results):
        if isinstance(rows, BaseException):
            log.warning("chapter metadata failed on %s for %r: %s", src.name, series.title, rows)
            continue
        apply_metadata_rows(existing.values(), rows, series.title)

    for number, chapter in existing.items():
        current = ",".join(sorted(availability.get(number, set())))
        if chapter.available_sources != current:
            chapter.available_sources = current

    await session.commit()
    return added


def _has_internal_number_gap(chapters: dict[float, Chapter], threshold: int = 3) -> bool:
    """Whether known integer chapters have a meaningful internal gap.

    Small holes are common for extras/decimal chapters; a larger gap is a
    signal to consult explicit volume-map metadata for missing chapter rows.
    """
    numbers = sorted(
        int(number) for number in chapters
        if number > 0 and number == int(number)
    )
    return any(b - a > threshold for a, b in zip(numbers, numbers[1:]))


def _add_volume_map_gap_chapters(
    series: Series,
    existing: dict[float, Chapter],
    volume_map: dict[float, int],
    map_sources: dict[float, str] | None = None,
) -> int:
    """Create missing chapters explicitly named by a selected volume map.

    The map is not used to extend a series beyond the observed chapter span:
    it only fills holes between chapters already seen from a chapter/release
    source. That keeps MangaDex's all-language aggregate useful for licensed
    gaps without turning it into a speculative chapter generator.
    """
    if not existing or not volume_map:
        return 0
    low, high = min(existing), max(existing)
    added = 0
    for number, volume in sorted(volume_map.items()):
        if number in existing or number < low or number > high or number <= 0:
            continue
        ch = Chapter(
            number=number, volume=volume,
            volume_source=(map_sources or {}).get(number, "disk-inferred"),
            monitored=series.monitored,
        )
        series.chapters.append(ch)
        existing[number] = ch
        added += 1
    return added


async def collect_volume_maps(
    series: Series, values: dict[str, str]
) -> list[tuple[str, dict[float, int]]]:
    """Every linked source's volume data, labeled by source name, in priority
    order. MangaUpdates' per-release volume tags compete too — usually sparse,
    but listed last so structured source data (MangaDex aggregate) wins ties."""
    links = {sl.source_name: sl for sl in series.source_links}
    maps: list[tuple[str, dict[float, int]]] = []
    for src in registry.enabled_direct_sources(values):
        link = links.get(src.name)
        if link is None:
            continue
        try:
            volume_map = await src.get_volume_map(link.external_id)
        except Exception as exc:
            log.warning("volume map failed on %s for %r: %s", src.name, series.title, exc)
            continue
        if volume_map:
            maps.append((src.name, volume_map))
    if series.mangaupdates_id is not None:
        try:
            release_data = await mangaupdates.get_release_data(series.mangaupdates_id)
            if release_data.volume_anchors:
                maps.append(("mangaupdates", release_data.volume_anchors))
        except Exception as exc:
            log.warning("mangaupdates volume anchors failed for %r: %s", series.title, exc)
    return maps


async def fetch_volume_map(
    series: Series, values: dict[str, str]
) -> tuple[str, dict[float, int], dict[float, str]]:
    """Authority-ranked chapter→volume assignments for a series.

    Returns (primary source name, mapping, per-chapter source labels) so
    callers can stamp honest provenance; see :mod:`mangarr.volumes`.
    """
    return select_labeled_volume_map(await collect_volume_maps(series, values))


def _series_folders(series: Series) -> list[Path]:
    """Directories holding the series' files: the primary folder (adopting a
    matching pre-existing folder when the primary doesn't exist yet) plus any
    extra folders. Empty when no root folder is configured."""
    from ..library.scanner import find_existing_folder, resolve_folders

    if series.root_folder is None:
        return []
    root = Path(series.root_folder.path)
    extras = [f.path for f in series.extra_folders]
    folders = resolve_folders(root, series, extras)
    if not folders[0].exists() and not extras and not series.folder_pinned:
        found = find_existing_folder(root, series)
        if found:
            series.folder_name = found
            folders = resolve_folders(root, series, extras)
    return folders


def disk_volume_numbers(series: Series) -> set[int]:
    """Volume numbers of whole-volume archives present in the series' folders."""
    volumes: set[int] = set()
    for folder in _series_folders(series):
        if not folder.exists():
            continue
        for mf in find_media_files(folder):
            if mf.volume_number is not None and mf.chapter_number is None:
                volumes.add(mf.volume_number)
    return volumes


def refine_volume_map_with_disk(
    series: Series, volume_map: dict[float, int]
) -> dict[float, int]:
    """Extend a source-derived volume map using the volume archives on disk:
    chapters the source couldn't place are distributed across the volumes the
    user's own files prove exist (see distribute_over_disk_volumes). Metadata
    alone is never extrapolated — with no matching files on disk, chapters
    the source can't place stay unassigned."""
    if not volume_map:
        return volume_map
    disk_volumes = disk_volume_numbers(series)
    if not disk_volumes:
        return reconcile_decimal_volumes(
            volume_map, (c.number for c in series.chapters)
        )
    finished = series.status in (SeriesStatus.FINISHED, SeriesStatus.CANCELLED)
    complete = bool(
        finished and series.total_volumes and max(disk_volumes) >= series.total_volumes
    )
    fallback_rate = (
        series.total_chapters / series.total_volumes
        if series.total_chapters and series.total_volumes else None
    )
    mapping = distribute_over_disk_volumes(
        volume_map, [c.number for c in series.chapters], disk_volumes,
        complete=complete, fallback_rate=fallback_rate,
    )
    return reconcile_decimal_volumes(mapping, (c.number for c in series.chapters))


async def reconcile_downloaded_files(session: AsyncSession, series: Series) -> int:
    """Clear downloaded state for chapters whose recorded media file is gone."""
    missing = 0
    for chapter in series.chapters:
        if not chapter.downloaded:
            continue
        if not chapter.file_path or not Path(chapter.file_path).is_file():
            chapter.downloaded = False
            chapter.file_path = ""
            missing += 1
    if missing:
        await session.commit()
        log.info("Marked %d missing file(s) for %r", missing, series.title)
    return missing


async def scan_series_folder(session: AsyncSession, series: Series) -> None:
    """Adopt existing library folders for the series and mark chapters that are
    already on disk as owned (so they aren't re-downloaded)."""
    from ..library.scanner import scan_series

    folders = _series_folders(series)
    if not folders:
        return
    scan_series(series, list(series.chapters), folders)
    await session.commit()


# series ids with a full refresh in flight, so the UI can show what's
# happening on an otherwise-empty freshly added series page. Endpoints that
# spawn the task pre-mark the id (create_task doesn't start synchronously);
# the task itself clears it.
REFRESHING: set[int] = set()
_SERIES_LOCKS: dict[int, asyncio.Lock] = {}


def _series_lock(series_id: int) -> asyncio.Lock:
    lock = _SERIES_LOCKS.get(series_id)
    if lock is None:
        lock = asyncio.Lock()
        _SERIES_LOCKS[series_id] = lock
    return lock


async def acquire_series_lock(series_id: int) -> asyncio.Lock:
    lock = _series_lock(series_id)
    await lock.acquire()
    return lock


async def try_acquire_series_lock(series_id: int) -> asyncio.Lock | None:
    lock = _series_lock(series_id)
    if lock.locked():
        return None
    await lock.acquire()
    return lock


async def refresh_series_full(series_id: int, grab_missing: bool = False) -> None:
    REFRESHING.add(series_id)
    lock = await acquire_series_lock(series_id)
    try:
        async with session_scope() as session:
            series = await _load_series(session, series_id)
            if series is None:
                return
            values = await registry.apply_settings(session)
            try:
                await refresh_series_metadata(session, series)
            except Exception as exc:
                log.warning("metadata refresh failed for series %d: %s", series_id, exc)
                # a failed commit (e.g. a duplicate mangaupdates id hitting the
                # unique index) leaves the session unusable until rolled back
                await session.rollback()
            chapter_cache: ChapterListCache = {}
            await link_sources(session, series, values)
            await update_chapters(session, series, values, chapter_cache)
            # adopt existing on-disk files before the monitor considers grabbing
            if values.get("library_scan_on_add", "true") == "true":
                try:
                    await scan_series_folder(session, series)
                except Exception as exc:
                    log.warning("library scan failed for series %d: %s", series_id, exc)
            await reconcile_downloaded_files(session, series)
            if grab_missing:
                # explicit one-time search (e.g. "search for missing" at add
                # time): runs even for unmonitored series, whose chapters carry
                # monitored=False — the user asked for the missing content now
                torrent_grab = await auto_grab_best_torrent(session, series, values)
                await grab_missing_chapters(
                    session, series, values, only_monitored=False,
                    chapter_cache=chapter_cache,
                    exclude_numbers=(torrent_grab.coverage if torrent_grab else None),
                    allowed_series_download_id=(
                        torrent_grab.download_id if torrent_grab else None
                    ),
                )
    except Exception:
        # this runs as a fire-and-forget task: without this, the exception
        # would only surface when the task object is garbage-collected
        log.exception("full refresh of series %d failed", series_id)
    finally:
        lock.release()
        REFRESHING.discard(series_id)


async def scan_all_series() -> None:
    """Scan every series' folder to adopt on-disk files (background job)."""
    async with session_scope() as session:
        series_ids = [row[0] for row in (await session.execute(select(Series.id))).all()]
    for series_id in series_ids:
        async with session_scope() as session:
            series = await _load_series(session, series_id)
            if series is not None:
                try:
                    await scan_series_folder(session, series)
                except Exception as exc:
                    log.warning("library scan failed for series %d: %s", series_id, exc)


async def _load_series(session: AsyncSession, series_id: int) -> Series | None:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Series)
        .options(selectinload(Series.chapters), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series_id)
    )
    return result.scalar_one_or_none()


# ------------------------------------------------------------------- grabs

async def enqueue_direct(
    session: AsyncSession, series: Series, chapter: Chapter,
    source_name: str, external_id: str, url: str = "",
    commit: bool = True,
) -> Download:
    """Queue a direct download. `commit=False` lets bulk callers (the monitor)
    batch many grabs into one commit."""
    dl = Download(
        series_id=series.id,
        chapter_id=chapter.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        title=f"{series.title} - Chapter {chapter.number:g}",
        source_name=source_name,
        payload=external_id,
    )
    session.add(dl)
    session.add(HistoryEvent(
        series_id=series.id, chapter_id=chapter.id, event="grabbed",
        source_name=source_name, detail=url or external_id,
    ))
    if commit:
        await session.commit()
    return dl


def magnet_btih_hex(magnet: str) -> str:
    """The magnet's info-hash in the lowercase hex form qBittorrent reports.
    Base32 hashes (older magnet links) are converted — matching against
    qBittorrent by their raw form would never succeed."""
    m = BTIH_RE.search(magnet)
    if not m:
        return ""
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower()
    try:
        return base64.b32decode(raw).hex()
    except (binascii.Error, ValueError):
        return ""


def torrent_save_path(base: str, category: str) -> str | None:
    """Return the category's final path without duplicating its suffix.

    Older installations treated ``downloads_dir`` as the final category
    directory, while newer setup guidance treats it as the downloads root.
    Accept both forms so an existing ``/downloads/mangarr`` setting does not
    become ``/downloads/mangarr/mangarr`` after an upgrade.
    """
    base = base.strip()
    category = category.strip().strip("/")
    if not base:
        return None
    base = base.rstrip("/") or "/"
    if not category:
        return base
    if base != "/" and base.rsplit("/", 1)[-1] == category:
        return base
    return f"/{category}" if base == "/" else f"{base}/{category}"



async def submit_torrent(magnet: str, values: dict[str, str]) -> str:
    """Submit a magnet to qBittorrent and return its normalized info hash.

    Kept separate from the database row creation so a failed torrent can be
    submitted again without replacing its Activity entry.
    """
    torrent_hash = magnet_btih_hex(magnet)
    if not torrent_hash:
        raise ValueError("magnet link must include a valid btih info hash")
    client = QbtClient(
        values["qbittorrent_url"], values["qbittorrent_username"], values["qbittorrent_password"]
    )
    try:
        category = values["qbittorrent_category"]
        # put grabs in a category subfolder so they stay organized and separate
        # from other qBittorrent downloads, regardless of its auto-management.
        # A configured downloads folder wins over qBittorrent's default save
        # path — pick one on the library's filesystem so imports can hardlink.
        base = values.get("downloads_dir", "").strip() or await client.default_save_path()
        save_path = torrent_save_path(base, category)
        await client.ensure_category(category, save_path)
        await client.add_magnet(magnet, category=category, save_path=save_path)
    finally:
        await client.close()
    return torrent_hash


async def enqueue_torrent(
    session: AsyncSession, series: Series | None, magnet: str, title: str, values: dict[str, str],
) -> Download:
    torrent_hash = await submit_torrent(magnet, values)
    dl = Download(
        series_id=series.id if series else None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        title=title,
        source_name="nyaa",
        payload=magnet,
        torrent_hash=torrent_hash,
    )
    session.add(dl)
    session.add(HistoryEvent(
        series_id=series.id if series else None, event="grabbed",
        source_name="nyaa", detail=title,
    ))
    await session.commit()
    return dl


@dataclass(frozen=True)
class AutomaticTorrentGrab:
    download_id: int
    coverage: set[float]


async def auto_grab_best_torrent(
    session: AsyncSession, series: Series, values: dict[str, str]
) -> AutomaticTorrentGrab | None:
    """Inspect Nyaa metadata and queue one best-coverage add-time release.

    Returns the new download id and its expected chapter coverage. The id lets
    the caller distinguish this torrent from an older series-level download;
    older in-flight packs must continue to block duplicate direct grabs.
    """
    if (
        values.get("qbittorrent_enabled") != "true"
        or not series.root_folder
        or not registry.enabled_torrent_indexers(values)
    ):
        return None
    existing_download = await session.scalar(
        select(Download.id)
        .where(
            Download.series_id == series.id,
            Download.chapter_id.is_(None),
            Download.status.in_([
                DownloadStatus.QUEUED,
                DownloadStatus.DOWNLOADING,
                DownloadStatus.IMPORTING,
            ]),
        )
        .limit(1)
    )
    if existing_download is not None:
        log.info(
            "automatic torrent search skipped for %r: series download %d is in flight",
            series.title,
            existing_download,
        )
        return None
    try:
        max_gib = int(values.get("torrent_auto_max_size_gib", "30"))
        min_seeders = int(values.get("torrent_auto_min_seeders", "1"))
        selection = await select_best_torrent(
            series,
            list(series.chapters),
            registry.enabled_torrent_indexers(values),
            max_size_bytes=max_gib * 1024**3,
            min_seeders=min_seeders,
        )
    except Exception as exc:
        log.warning("automatic torrent search failed for %r: %s", series.title, exc)
        return None
    if selection is None:
        if not any(chapter.volume is not None for chapter in series.chapters):
            log.info(
                "automatic torrent search found no exact chapter coverage for %r; "
                "volume-only releases cannot be scored without a chapter-to-volume map",
                series.title,
            )
        else:
            log.info(
                "automatic torrent search found no inspectable coverage for %r",
                series.title,
            )
        return None
    try:
        download = await enqueue_torrent(
            session, series, selection.release.magnet, selection.release.title, values
        )
    except Exception as exc:
        log.warning("automatic torrent grab failed for %r: %s", series.title, exc)
        return None
    log.info(
        "automatic torrent selected %r for %r: %d chapter(s), %d seeder(s), %.2f GiB",
        selection.release.title,
        series.title,
        len(selection.coverage),
        selection.release.seeders,
        selection.release.size_bytes / 1024**3,
    )
    return AutomaticTorrentGrab(
        download_id=download.id,
        coverage=set(selection.coverage),
    )


# --------------------------------------------------------- direct downloads

async def process_direct_queue() -> None:
    """Processes all queued direct downloads, one chapter at a time."""
    while True:
        async with session_scope() as session:
            result = await session.execute(
                select(Download)
                .where(Download.kind == DownloadKind.DIRECT,
                       Download.status == DownloadStatus.QUEUED)
                .order_by(Download.id)
                .limit(1)
            )
            dl = result.scalar_one_or_none()
            if dl is None:
                return
            await _run_direct_download(session, dl)


async def _run_direct_download(session: AsyncSession, dl: Download) -> None:
    download_id = dl.id
    source_name = dl.source_name
    values = await registry.apply_settings(session)
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    chapter = await session.get(Chapter, dl.chapter_id) if dl.chapter_id else None
    source = registry.DIRECT_SOURCES.get(source_name)
    if series is None or chapter is None or source is None:
        dl.status = DownloadStatus.FAILED
        dl.error = "series/chapter/source no longer exists"
        await session.commit()
        return
    series_id = series.id
    chapter_id = chapter.id

    root = series.root_folder.path if series.root_folder else None
    if not root:
        dl.status = DownloadStatus.FAILED
        dl.error = "series has no root folder configured"
        await session.commit()
        return

    # page fetches run concurrently and call back into ensure_not_cancelled /
    # on_progress; the shared AsyncSession is not task-safe, so every session
    # use in those callbacks is serialized through this lock
    db_lock = asyncio.Lock()
    last_progress_commit = 0.0
    last_cancel_check = 0.0
    last_activity = time.monotonic()

    async def ensure_not_cancelled(force: bool = False) -> None:
        nonlocal last_cancel_check
        if not force and time.monotonic() - last_cancel_check < 0.5:
            return
        async with db_lock:
            if not force and time.monotonic() - last_cancel_check < 0.5:
                return
            last_cancel_check = time.monotonic()
            await _raise_if_download_removed(session, download_id)

    claimed = await session.execute(
        sa_update(Download)
        .where(Download.id == download_id, Download.status == DownloadStatus.QUEUED)
        .values(status=DownloadStatus.DOWNLOADING)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        log.info("direct download %d was removed before the worker could start it", download_id)
        return
    await session.commit()
    await session.refresh(dl)

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_progress_commit, last_activity
        last_activity = time.monotonic()
        await ensure_not_cancelled()
        progress = done / total
        now = time.monotonic()
        if done < total and now - last_progress_commit < 1.0:
            dl.progress = progress
            return
        async with db_lock:
            now = time.monotonic()
            if done < total and now - last_progress_commit < 1.0:
                dl.progress = progress
                return
            dl.progress = progress
            last_progress_commit = now
            await session.commit()

    dest: Path | None = None
    dest_preexisted = False
    try:
        # inside the try: a broken naming template must fail THIS download
        # (visibly, with an error) instead of crashing the queue worker and
        # leaving the row stuck in "downloading"
        dest = chapter_path(
            Path(root),
            values["naming_template"], values["naming_template_no_volume"],
            series.title, series.folder_name,
            chapter.number, chapter.volume, chapter.title,
        )
        dest_preexisted = dest.exists()
        download_task = asyncio.create_task(download_chapter_to_cbz(
            source, dl.payload, series, chapter, dest,
            progress_cb=on_progress,
            cancel_cb=lambda: ensure_not_cancelled(force=True),
            web_url="",
        ))
        stall_seconds = DIRECT_STALL_TIMEOUT.total_seconds()
        while True:
            finished, _ = await asyncio.wait({download_task},
                                             timeout=min(15.0, stall_seconds))
            if finished:
                download_task.result()
                break
            if time.monotonic() - last_activity > stall_seconds:
                download_task.cancel()
                try:
                    await download_task
                except BaseException:
                    pass  # the stall, not the cancellation, is the error
                raise RuntimeError(
                    f"stalled: no page finished for {stall_seconds / 60:g} minutes; "
                    "download cancelled so the queue can continue"
                )
        await ensure_not_cancelled(force=True)
    except DownloadCancelled:
        await session.rollback()
        if dest is not None and not dest_preexisted:
            for path in (dest, dest.with_suffix(".cbz.partial")):
                try:
                    if path.exists():
                        path.unlink()
                except OSError as exc:
                    log.warning("could not remove cancelled download artifact %s: %s", path, exc)
        log.info("direct download %d cancelled by user", download_id)
        return
    except Exception as exc:
        log.exception("direct download %d failed", download_id)
        # a progress commit may have been cancelled mid-flight when the page
        # fetches were torn down; reset the session before recording failure
        await session.rollback()
        dl = await session.get(Download, download_id)
        if dl is None:
            return
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        session.add(HistoryEvent(
            series_id=series_id, chapter_id=chapter_id, event="failed",
            source_name=source_name, detail=dl.error,
        ))
        await session.commit()
        return

    chapter.downloaded = True
    chapter.file_path = str(dest)
    dl.status = DownloadStatus.DONE
    dl.progress = 1.0
    dl.error = ""
    session.add(HistoryEvent(
        series_id=series.id, chapter_id=chapter.id, event="imported",
        source_name=dl.source_name, detail=str(dest),
    ))
    await session.commit()
    notifications.notify_import(values, series.id, f"Chapter {chapter.number:g}")


# --------------------------------------------------------------- qbt sync

# a torrent this long gone from qBittorrent (deleted there, or an add that
# never registered) will not come back; without a limit the download would sit
# in "downloading" forever — and a series-level zombie blocks all grabs for
# its series. Counters are in-memory: a restart just re-counts from zero.
TORRENT_MISSING_LIMIT = 8  # consecutive sync passes (~1 min at 8s intervals)
_torrent_missing_counts: dict[int, int] = {}

# completed torrents whose content path isn't visible from this container may
# just be a slow mount — but after this many passes it's a config problem, so
# fail with the path in the error instead of retrying silently forever
IMPORT_PATH_MISSING_LIMIT = 40  # ~5 min at 8s intervals
_import_path_missing_counts: dict[int, int] = {}


async def sync_qbittorrent() -> None:
    async with session_scope() as session:
        values = await registry.apply_settings(session)
        if values["qbittorrent_enabled"] != "true":
            return
        result = await session.execute(
            select(Download).where(
                Download.kind == DownloadKind.TORRENT,
                Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                                     DownloadStatus.IMPORTING]),
            )
        )
        downloads = result.scalars().all()
        if not downloads:
            return
        client = QbtClient(
            values["qbittorrent_url"], values["qbittorrent_username"],
            values["qbittorrent_password"],
        )
        try:
            for dl in downloads:
                if not dl.torrent_hash:
                    continue
                torrent = await client.get_torrent(dl.torrent_hash)
                if torrent is None:
                    misses = _torrent_missing_counts.get(dl.id, 0) + 1
                    _torrent_missing_counts[dl.id] = misses
                    if misses >= TORRENT_MISSING_LIMIT:
                        _torrent_missing_counts.pop(dl.id, None)
                        dl.status = DownloadStatus.FAILED
                        dl.error = "torrent not found in qBittorrent (removed externally?)"
                        log.warning("torrent download %d (%r) vanished from qBittorrent; "
                                    "marking failed", dl.id, dl.title)
                        await session.commit()
                    continue
                _torrent_missing_counts.pop(dl.id, None)
                dl.progress = torrent.progress
                if torrent.is_complete and torrent.content_path:
                    dl.status = DownloadStatus.IMPORTING
                    await session.commit()
                    await _import_torrent(session, dl, Path(torrent.content_path), values)
                else:
                    await session.commit()
        finally:
            await client.close()


async def _import_torrent(
    session: AsyncSession, dl: Download, content_path: Path, values: dict[str, str]
) -> None:
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    if series is None or not series.root_folder:
        dl.status = DownloadStatus.FAILED
        dl.error = "torrent has no linked series/root folder; import manually"
        await session.commit()
        return
    if not content_path.exists():
        # path as seen by qBittorrent may not be mounted here yet — retry a
        # while, then fail visibly (a path mapping problem never resolves)
        misses = _import_path_missing_counts.get(dl.id, 0) + 1
        _import_path_missing_counts[dl.id] = misses
        if misses >= IMPORT_PATH_MISSING_LIMIT:
            _import_path_missing_counts.pop(dl.id, None)
            dl.status = DownloadStatus.FAILED
            dl.error = (f"content path not visible from mangarr: {content_path} — "
                        "check that qBittorrent's download path is mounted here")
        else:
            dl.error = f"content path not found: {content_path}"
        await session.commit()
        return
    try:
        imported = import_torrent_payload(
            content_path, series, list(series.chapters), Path(series.root_folder.path),
            values["naming_template"], values["naming_template_no_volume"],
            import_mode=values.get("import_mode", "hardlink"),
        )
    except FileNotFoundError as exc:
        # qBittorrent may finish downloading and then atomically move the
        # payload from its temporary directory to the category directory.
        # A move between discovery and hardlink/copy is transient: let the
        # next sync fetch the torrent's new content_path and retry.  Import is
        # idempotent because already-created destinations are not overwritten.
        misses = _import_path_missing_counts.get(dl.id, 0) + 1
        _import_path_missing_counts[dl.id] = misses
        if misses >= IMPORT_PATH_MISSING_LIMIT:
            _import_path_missing_counts.pop(dl.id, None)
            dl.status = DownloadStatus.FAILED
            dl.error = (f"content kept disappearing during import: {exc} — "
                        "check qBittorrent's temporary and category paths")[:500]
            log.exception("torrent import %d repeatedly lost its content path", dl.id)
        else:
            dl.status = DownloadStatus.DOWNLOADING
            dl.error = f"content moved during import; retrying: {exc}"[:500]
            log.info("torrent import %d content moved; retrying on next sync", dl.id)
        await session.commit()
        return
    except Exception as exc:
        log.exception("torrent import %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        await session.commit()
        return
    _import_path_missing_counts.pop(dl.id, None)
    for dest, chapter, volume in imported:
        if chapter is not None:
            chapter.downloaded = True
            chapter.file_path = str(dest)
        elif volume is not None:
            # a volume archive covers every chapter assigned to that volume
            for ch in series.chapters:
                if ch.volume == volume and not ch.downloaded:
                    ch.downloaded = True
                    ch.file_path = str(dest)
    dl.status = DownloadStatus.DONE
    dl.progress = 1.0
    dl.error = ""
    session.add(HistoryEvent(
        series_id=series.id, event="imported", source_name="nyaa",
        detail=f"{len(imported)} file(s) from {dl.title}",
    ))
    await session.commit()
    notifications.notify_import(values, series.id, f"{len(imported)} file(s) from torrent")


# ------------------------------------------------------------ monitor loop

async def grab_missing_chapters(
    session: AsyncSession, series: Series, values: dict[str, str],
    only_monitored: bool = True,
    chapter_cache: ChapterListCache | None = None,
    exclude_numbers: set[float] | None = None,
    allowed_series_download_id: int | None = None,
) -> int:
    """Queue missing monitored chapters from linked direct sources.

    This is used by both the scheduled monitor and the add-time refresh path,
    so a newly added series starts pulling available chapters as soon as its
    source links and chapter list have been created. `only_monitored=False`
    (explicit user-requested search) also grabs unmonitored missing chapters.
    """
    result = await session.execute(
        select(Download.id, Download.chapter_id).where(
            Download.series_id == series.id,
            Download.status.in_([
                DownloadStatus.QUEUED,
                DownloadStatus.DOWNLOADING,
                DownloadStatus.IMPORTING,
            ]),
        )
    )
    active_rows = result.all()
    active_chapters = {
        chapter_id for _, chapter_id in active_rows if chapter_id is not None
    }
    series_downloads = {
        download_id for download_id, chapter_id in active_rows if chapter_id is None
    }
    if series_downloads and series_downloads != {allowed_series_download_id}:
        # a series-level download (e.g. a Nyaa volume pack) is in flight — its
        # chapter coverage is unknown until it imports, so grabbing per-chapter
        # now would duplicate everything
        log.info("monitor: %r has a series-level download in flight; skipping grabs", series.title)
        return 0

    excluded = exclude_numbers or set()
    wanted = [
        c for c in series.chapters
        if (c.monitored or not only_monitored)
        and not c.downloaded
        and c.id not in active_chapters
        and c.number not in excluded
    ]
    if not wanted:
        return 0

    # a chapter that recently failed on a source shouldn't be retried there —
    # fall through to the next source instead. Old failures expire (sources
    # fix broken chapters), and user cancellations don't count as failures.
    retry_cutoff = datetime.now(timezone.utc) - FAILED_GRAB_RETRY_AFTER
    result = await session.execute(
        select(Download.chapter_id, Download.source_name).where(
            Download.series_id == series.id,
            Download.status == DownloadStatus.FAILED,
            Download.chapter_id.isnot(None),
            Download.error != REMOVED_BY_USER,
            Download.updated_at >= retry_cutoff,
        )
    )
    failed_pairs = {(cid, name) for cid, name in result.all()}

    queued = 0
    links = {sl.source_name: sl for sl in series.source_links}
    remaining = {c.number: c for c in wanted}
    for src in registry.enabled_direct_sources(values):
        if not remaining:
            break
        link = links.get(src.name)
        if link is None:
            continue
        try:
            source_chapters = await _list_chapters_cached(src, link.external_id, chapter_cache)
        except Exception as exc:
            log.warning("monitor: %s list failed for %r: %s", src.name, series.title, exc)
            continue
        for sc in source_chapters:
            ch = remaining.get(sc.number)
            if ch is None or (ch.id, src.name) in failed_pairs:
                continue
            remaining.pop(sc.number, None)
            await enqueue_direct(session, series, ch, src.name, sc.external_id, sc.url,
                                 commit=False)
            queued += 1
    if queued:
        await session.commit()
    else:
        # e.g. only MangaDex is linked but its chapters are all external —
        # say so instead of failing silently
        log.info("monitor: no linked source serves any of the %d missing chapter(s) of %r",
                 len(remaining), series.title)
    return queued


async def recover_interrupted_downloads() -> None:
    """Startup pass: direct downloads left mid-flight by a crash/restart go
    back to queued (the worker only picks up QUEUED, so they would otherwise
    sit in 'downloading' forever — and block re-grabs of their chapters)."""
    async with session_scope() as session:
        result = await session.execute(
            select(Download).where(
                Download.kind == DownloadKind.DIRECT,
                Download.status == DownloadStatus.DOWNLOADING,
            )
        )
        stuck = result.scalars().all()
        for dl in stuck:
            dl.status = DownloadStatus.QUEUED
            dl.progress = 0.0
        if stuck:
            await session.commit()
            log.info("Requeued %d direct download(s) interrupted by restart", len(stuck))


async def monitor_all() -> None:
    """Refresh monitored series and grab missing monitored chapters."""
    async with session_scope() as session:
        result = await session.execute(select(Series.id).where(Series.monitored == True))  # noqa: E712
        series_ids = [row[0] for row in result.all()]

    for series_id in series_ids:
        lock = await try_acquire_series_lock(series_id)
        if lock is None:
            log.info("monitor: series %d is already refreshing; skipping this pass", series_id)
            continue
        try:
            try:
                async with session_scope() as session:
                    values = await registry.apply_settings(session)
                    series = await _load_series(session, series_id)
                    if series is None:
                        continue
                    if _metadata_is_stale(series):
                        try:
                            await refresh_series_metadata(session, series)
                        except Exception as exc:
                            log.warning("metadata refresh failed for series %d: %s", series_id, exc)
                            await session.rollback()
                    chapter_cache: ChapterListCache = {}
                    await link_sources(session, series, values, respect_backoff=True)
                    await update_chapters(session, series, values, chapter_cache)
                    # adopt whatever is on disk before deciding what's missing, so
                    # files that appeared since the last pass aren't re-downloaded
                    try:
                        await scan_series_folder(session, series)
                    except Exception as exc:
                        log.warning("library scan failed for series %d: %s", series_id, exc)
                    await grab_missing_chapters(session, series, values,
                                                chapter_cache=chapter_cache)
            except Exception:
                # one broken series must not abort the whole monitor pass
                log.exception("monitor pass failed for series %d", series_id)
        finally:
            lock.release()
