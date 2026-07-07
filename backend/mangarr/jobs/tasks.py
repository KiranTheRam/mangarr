"""Background tasks: series refresh, source linking, grabbing, download
processing, qBittorrent sync, and the monitor loop."""

import asyncio
import logging
import re
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from ..sources.base import DirectSource
from ..titles import split_alt_titles, title_queries
from ..util import normalize_title
from ..volumes import build_volume_map, distribute_over_disk_volumes

log = logging.getLogger(__name__)

BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")


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
    await session.commit()


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


async def link_sources(session: AsyncSession, series: Series, values: dict[str, str]) -> None:
    """Auto-match the series on every enabled direct source it isn't linked to."""
    linked = {sl.source_name for sl in series.source_links}
    titles = _titles_of(series)
    # normalized titles that are empty (e.g. a title written only in CJK) must
    # not be used for matching — an empty string is a prefix/substring of
    # everything and would match the first result of any catalog
    wanted = {nt for t in titles if (nt := normalize_title(t))}
    for src in registry.enabled_direct_sources(values):
        if src.name in linked:
            continue
        match = None
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
            for cand in candidates:
                cand_titles = {n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))}
                if wanted & cand_titles:
                    match = cand
                    break
            if match is None and candidates and len(nq) >= 4:
                # fall back to the top result only if it shares a real title
                # prefix (guard the length so short/empty queries can't match)
                top = candidates[0]
                if normalize_title(top.title).startswith(nq[:12]):
                    match = top
            if match:
                break
        if match:
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

async def update_chapters(session: AsyncSession, series: Series, values: dict[str, str]) -> int:
    """Union chapter lists from linked sources into the DB. Returns # new."""
    existing = {c.number: c for c in series.chapters}
    added = 0
    links = {sl.source_name: sl for sl in series.source_links}
    for src in registry.enabled_direct_sources(values):
        link = links.get(src.name)
        if link is None:
            continue
        try:
            source_chapters = await src.list_chapters(link.external_id)
        except Exception as exc:
            log.warning("chapter list failed on %s for %r: %s", src.name, series.title, exc)
            continue
        for sc in source_chapters:
            ch = existing.get(sc.number)
            if ch is None:
                ch = Chapter(
                    number=sc.number,
                    volume=sc.volume,
                    title=sc.title,
                    monitored=series.monitored,
                )
                # append via the relationship so series.chapters is current for
                # the scan/backfill later in this same pass
                series.chapters.append(ch)
                existing[sc.number] = ch
                added += 1
            else:
                if ch.volume is None and sc.volume is not None:
                    ch.volume = sc.volume
                if not ch.title and sc.title:
                    ch.title = sc.title

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
                    added += 1
                elif ch.released_at is None and released_at is not None:
                    ch.released_at = released_at

    # backfill volume numbers for chapters that came from sources without
    # volume data (e.g. WeebCentral, or MangaDex titles whose chapters are
    # external and thus never appear in the feed). Volume archives already
    # on disk extend the map past the sources' last anchor, so a library
    # being adopted gets its later volumes mapped on the first pass instead
    # of needing a manual volume resync.
    if any(c.volume is None for c in existing.values()):
        volume_map = await fetch_volume_map(series, values)
        volume_map = refine_volume_map_with_disk(series, volume_map)
        for number, ch in existing.items():
            if ch.volume is None and number in volume_map:
                ch.volume = volume_map[number]

    await session.commit()
    return added


async def fetch_volume_map(series: Series, values: dict[str, str]) -> dict[float, int]:
    """Chapter→volume assignments for a series: the union of every linked
    source's volume data, sanitized (stray mislabeled chapters dropped) and
    with gaps between known volumes filled in (see mangarr.volumes)."""
    links = {sl.source_name: sl for sl in series.source_links}
    maps: list[dict[float, int]] = []
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
            maps.append(volume_map)
    # MangaUpdates release volume tags are sparse but real anchors — lowest
    # priority so structured source data (MangaDex aggregate) wins on overlap
    if series.mangaupdates_id is not None:
        try:
            release_data = await mangaupdates.get_release_data(series.mangaupdates_id)
            if release_data.volume_anchors:
                maps.append(release_data.volume_anchors)
        except Exception as exc:
            log.warning("mangaupdates volume anchors failed for %r: %s", series.title, exc)
    return build_volume_map(maps, [c.number for c in series.chapters])


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
    if not folders[0].exists() and not extras:
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
    chapters the sources couldn't place are distributed across the volumes the
    files prove exist (see mangarr.volumes.distribute_over_disk_volumes)."""
    if not volume_map:
        return volume_map
    disk_volumes = disk_volume_numbers(series)
    if not disk_volumes:
        return volume_map
    finished = series.status in (SeriesStatus.FINISHED, SeriesStatus.CANCELLED)
    complete = bool(
        finished and series.total_volumes and max(disk_volumes) >= series.total_volumes
    )
    fallback_rate = (
        series.total_chapters / series.total_volumes
        if series.total_chapters and series.total_volumes else None
    )
    return distribute_over_disk_volumes(
        volume_map, [c.number for c in series.chapters], disk_volumes,
        complete=complete, fallback_rate=fallback_rate,
    )


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


async def refresh_series_full(series_id: int, grab_missing: bool = False) -> None:
    async with session_scope() as session:
        series = await _load_series(session, series_id)
        if series is None:
            return
        values = await registry.apply_settings(session)
        try:
            await refresh_series_metadata(session, series)
        except Exception as exc:
            log.warning("metadata refresh failed for series %d: %s", series_id, exc)
        await link_sources(session, series, values)
        await update_chapters(session, series, values)
        # adopt existing on-disk files before the monitor considers grabbing
        if values.get("library_scan_on_add", "true") == "true":
            try:
                await scan_series_folder(session, series)
            except Exception as exc:
                log.warning("library scan failed for series %d: %s", series_id, exc)
        await reconcile_downloaded_files(session, series)
        if grab_missing:
            # explicit one-time search (e.g. "search for missing" at add time):
            # runs even for unmonitored series, whose chapters carry
            # monitored=False — the user asked for the missing content now
            await grab_missing_chapters(session, series, values, only_monitored=False)


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

async def find_release_for_chapter(
    series: Series, chapter: Chapter, values: dict[str, str]
) -> tuple[DirectSource, str, str] | None:
    """Best direct release: (source, chapter_external_id, url), by priority."""
    links = {sl.source_name: sl for sl in series.source_links}
    for src in registry.enabled_direct_sources(values):
        link = links.get(src.name)
        if link is None:
            continue
        try:
            source_chapters = await src.list_chapters(link.external_id)
        except Exception as exc:
            log.warning("list_chapters failed on %s: %s", src.name, exc)
            continue
        for sc in source_chapters:
            if sc.number == chapter.number:
                return src, sc.external_id, sc.url
    return None


async def enqueue_direct(
    session: AsyncSession, series: Series, chapter: Chapter,
    source_name: str, external_id: str, url: str = "",
) -> Download:
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
    await session.commit()
    return dl


async def enqueue_torrent(
    session: AsyncSession, series: Series | None, magnet: str, title: str, values: dict[str, str],
) -> Download:
    m = BTIH_RE.search(magnet)
    torrent_hash = m.group(1).lower() if m else ""
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
        save_path = f"{base.rstrip('/')}/{category}" if base else None
        await client.ensure_category(category, save_path)
        await client.add_magnet(magnet, category=category, save_path=save_path)
    finally:
        await client.close()
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
    values = await registry.apply_settings(session)
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    chapter = await session.get(Chapter, dl.chapter_id) if dl.chapter_id else None
    source = registry.DIRECT_SOURCES.get(dl.source_name)
    if series is None or chapter is None or source is None:
        dl.status = DownloadStatus.FAILED
        dl.error = "series/chapter/source no longer exists"
        await session.commit()
        return

    root = series.root_folder.path if series.root_folder else None
    if not root:
        dl.status = DownloadStatus.FAILED
        dl.error = "series has no root folder configured"
        await session.commit()
        return

    dl.status = DownloadStatus.DOWNLOADING
    await session.commit()

    dest = chapter_path(
        Path(root),
        values["naming_template"], values["naming_template_no_volume"],
        series.title, series.folder_name,
        chapter.number, chapter.volume, chapter.title,
    )

    progress_lock = asyncio.Lock()
    last_progress_commit = 0.0

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_progress_commit
        progress = done / total
        now = time.monotonic()
        if done < total and now - last_progress_commit < 1.0:
            dl.progress = progress
            return
        async with progress_lock:
            now = time.monotonic()
            if done < total and now - last_progress_commit < 1.0:
                dl.progress = progress
                return
            dl.progress = progress
            last_progress_commit = now
            await session.commit()

    try:
        await download_chapter_to_cbz(
            source, dl.payload, series, chapter, dest,
            progress_cb=on_progress, web_url="",
        )
    except Exception as exc:
        log.exception("direct download %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        session.add(HistoryEvent(
            series_id=series.id, chapter_id=chapter.id, event="failed",
            source_name=dl.source_name, detail=dl.error,
        ))
        await session.commit()
        return

    chapter.downloaded = True
    chapter.file_path = str(dest)
    dl.status = DownloadStatus.DONE
    dl.progress = 1.0
    session.add(HistoryEvent(
        series_id=series.id, chapter_id=chapter.id, event="imported",
        source_name=dl.source_name, detail=str(dest),
    ))
    await session.commit()


# --------------------------------------------------------------- qbt sync

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
                    continue
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
        # path as seen by qBittorrent may not be mounted here yet
        dl.error = f"content path not found: {content_path}"
        await session.commit()
        return
    try:
        imported = import_torrent_payload(
            content_path, series, list(series.chapters), Path(series.root_folder.path),
            values["naming_template"], values["naming_template_no_volume"],
            import_mode=values.get("import_mode", "hardlink"),
        )
    except Exception as exc:
        log.exception("torrent import %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        await session.commit()
        return
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
    session.add(HistoryEvent(
        series_id=series.id, event="imported", source_name="nyaa",
        detail=f"{len(imported)} file(s) from {dl.title}",
    ))
    await session.commit()


# ------------------------------------------------------------ monitor loop

async def grab_missing_chapters(
    session: AsyncSession, series: Series, values: dict[str, str],
    only_monitored: bool = True,
) -> int:
    """Queue missing monitored chapters from linked direct sources.

    This is used by both the scheduled monitor and the add-time refresh path,
    so a newly added series starts pulling available chapters as soon as its
    source links and chapter list have been created. `only_monitored=False`
    (explicit user-requested search) also grabs unmonitored missing chapters.
    """
    result = await session.execute(
        select(Download.chapter_id).where(
            Download.series_id == series.id,
            Download.status.in_([
                DownloadStatus.QUEUED,
                DownloadStatus.DOWNLOADING,
                DownloadStatus.IMPORTING,
            ]),
        )
    )
    active = {row[0] for row in result.all()}
    if None in active:
        # a series-level download (e.g. a Nyaa volume pack) is in flight — its
        # chapter coverage is unknown until it imports, so grabbing per-chapter
        # now would duplicate everything
        log.info("monitor: %r has a series-level download in flight; skipping grabs", series.title)
        return 0

    wanted = [
        c for c in series.chapters
        if (c.monitored or not only_monitored) and not c.downloaded and c.id not in active
    ]
    if not wanted:
        return 0

    # a chapter that already failed on a source shouldn't be retried there —
    # fall through to the next source instead.
    result = await session.execute(
        select(Download.chapter_id, Download.source_name).where(
            Download.series_id == series.id,
            Download.status == DownloadStatus.FAILED,
            Download.chapter_id.isnot(None),
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
            source_chapters = await src.list_chapters(link.external_id)
        except Exception as exc:
            log.warning("monitor: %s list failed for %r: %s", src.name, series.title, exc)
            continue
        for sc in source_chapters:
            ch = remaining.get(sc.number)
            if ch is None or (ch.id, src.name) in failed_pairs:
                continue
            remaining.pop(sc.number, None)
            await enqueue_direct(session, series, ch, src.name, sc.external_id, sc.url)
            queued += 1
    return queued


async def monitor_all() -> None:
    """Refresh monitored series and grab missing monitored chapters."""
    async with session_scope() as session:
        values = await registry.apply_settings(session)
        result = await session.execute(select(Series.id).where(Series.monitored == True))  # noqa: E712
        series_ids = [row[0] for row in result.all()]

    for series_id in series_ids:
        async with session_scope() as session:
            values = await registry.apply_settings(session)
            series = await _load_series(session, series_id)
            if series is None:
                continue
            await link_sources(session, series, values)
            await update_chapters(session, series, values)
            # adopt whatever is on disk before deciding what's missing, so
            # files that appeared since the last pass aren't re-downloaded
            try:
                await scan_series_folder(session, series)
            except Exception as exc:
                log.warning("library scan failed for series %d: %s", series_id, exc)
            await grab_missing_chapters(session, series, values)
