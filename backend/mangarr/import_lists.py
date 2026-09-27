"""Import lists: follow a reading list on AniList, MyAnimeList, MangaDex or
MangaUpdates and add its new entries to the library.

Every entry a list produces is recorded (ImportListEntry) and acted on once,
so removing a series from the library doesn't make the next sync add it
back; only entries that failed to add are retried."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from . import USER_AGENT
from .adding import AddOptions, LibraryIndex, SeriesExists, create_series, start_refreshes
from .db import session_scope
from .metadata.anilist import provider as anilist
from .metadata.mangaupdates import API_URL as MU_API_URL
from .metadata.mangaupdates import provider as mangaupdates
from .models import ImportList, ImportListEntry, RootFolder
from .util import RateLimiter, normalize_title, rl_request

log = logging.getLogger(__name__)

MAL_API_URL = "https://api.myanimelist.net/v2"


@dataclass(frozen=True)
class ProviderInfo:
    label: str
    # status key → label, in the provider's own vocabulary
    statuses: dict[str, str]
    default_statuses: tuple[str, ...]
    needs_username: bool = True
    needs_password: bool = False
    needs_client_id: bool = False


PROVIDERS: dict[str, ProviderInfo] = {
    "anilist": ProviderInfo(
        label="AniList",
        statuses={
            "CURRENT": "Reading", "PLANNING": "Planning", "COMPLETED": "Completed",
            "PAUSED": "Paused", "DROPPED": "Dropped", "REPEATING": "Rereading",
        },
        default_statuses=("CURRENT", "PLANNING"),
    ),
    "myanimelist": ProviderInfo(
        label="MyAnimeList",
        statuses={
            "reading": "Reading", "plan_to_read": "Plan to read", "completed": "Completed",
            "on_hold": "On hold", "dropped": "Dropped",
        },
        default_statuses=("reading", "plan_to_read"),
        needs_client_id=True,
    ),
    "mangadex": ProviderInfo(
        label="MangaDex",
        statuses={
            "reading": "Reading", "plan_to_read": "Plan to read", "completed": "Completed",
            "on_hold": "On hold", "dropped": "Dropped", "re_reading": "Re-reading",
        },
        default_statuses=("reading", "plan_to_read"),
        # uses the MangaDex account from Settings
        needs_username=False,
    ),
    "mangaupdates": ProviderInfo(
        label="MangaUpdates",
        # the fixed per-user lists, by list id
        statuses={"0": "Reading list", "1": "Wish list", "2": "Complete list",
                  "3": "Unfinished list", "4": "On hold list"},
        default_statuses=("0", "1"),
        needs_password=True,
    ),
}

SECRET_FIELDS = ("password",)


class ImportListError(RuntimeError):
    """The list could not be read (bad credentials, private profile, …)."""


@dataclass
class ListEntry:
    key: str  # "<kind>:<id on that site>"
    title: str
    alt_titles: list[str] = field(default_factory=list)
    anilist_id: int | None = None
    mangaupdates_id: int | None = None
    cover_url: str = ""
    year: int | None = None


def load_config(lst: ImportList) -> dict:
    try:
        config = json.loads(lst.config or "{}")
    except ValueError:
        config = {}
    return config if isinstance(config, dict) else {}


def statuses_of(kind: str, config: dict) -> list[str]:
    info = PROVIDERS[kind]
    chosen = [s for s in config.get("statuses") or [] if s in info.statuses]
    return chosen or list(info.default_statuses)


_client: httpx.AsyncClient | None = None
# MangaUpdates allows about one request a second; list pages share it with
# the metadata provider's own traffic only loosely, so stay conservative
_mu_limiter = RateLimiter(rate=1, per_seconds=1)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30)
    return _client


# ---------------------------------------------------------------- providers

async def _fetch_anilist(config: dict, statuses: list[str]) -> list[ListEntry]:
    username = (config.get("username") or "").strip()
    try:
        media = await anilist.user_manga_list(username, statuses)
    except RuntimeError as exc:
        raise ImportListError(str(exc)) from None
    entries = []
    for node in media:
        if node.get("format") == "NOVEL":
            continue
        meta = anilist.node_to_metadata(node)
        entries.append(ListEntry(
            key=f"anilist:{meta.provider_id}", title=meta.title, alt_titles=meta.alt_titles,
            anilist_id=int(meta.provider_id), cover_url=meta.cover_url, year=meta.year,
        ))
    return entries


async def _fetch_myanimelist(config: dict, statuses: list[str]) -> list[ListEntry]:
    username = (config.get("username") or "").strip()
    headers = {"X-MAL-CLIENT-ID": (config.get("client_id") or "").strip()}
    nodes: list[dict] = []
    for status in statuses:
        url: str | None = f"{MAL_API_URL}/users/{username}/mangalist"
        params: dict | None = {
            "status": status, "limit": 1000,
            "fields": "alternative_titles,start_date,media_type",
            "nsfw": "true",
        }
        while url:
            resp = await _http().get(url, params=params, headers=headers)
            if resp.status_code in (400, 401, 403):
                # an unknown client id comes back as a bare 400
                raise ImportListError("MyAnimeList rejected the request — check the client ID")
            if resp.status_code == 404:
                raise ImportListError(f"MyAnimeList user {username!r} not found")
            resp.raise_for_status()
            data = resp.json()
            nodes.extend(item["node"] for item in data.get("data") or [] if item.get("node"))
            # the next-page link already carries every query parameter
            url, params = (data.get("paging") or {}).get("next"), None
    nodes = [n for n in nodes if n.get("media_type") not in ("light_novel", "novel")]
    try:
        to_anilist = await anilist.ids_for_mal([int(n["id"]) for n in nodes])
    except Exception as exc:
        # titles still resolve through the MangaUpdates fallback
        log.warning("AniList id lookup for MyAnimeList entries failed: %s", exc)
        to_anilist = {}
    entries = []
    for node in nodes:
        alts = node.get("alternative_titles") or {}
        alt_titles = [t for t in [alts.get("en"), alts.get("ja"), *(alts.get("synonyms") or [])] if t]
        year_raw = str(node.get("start_date") or "")[:4]
        entries.append(ListEntry(
            key=f"myanimelist:{node['id']}", title=node.get("title") or "", alt_titles=alt_titles,
            anilist_id=to_anilist.get(int(node["id"])),
            cover_url=((node.get("main_picture") or {}).get("large")
                       or (node.get("main_picture") or {}).get("medium") or ""),
            year=int(year_raw) if year_raw.isdigit() else None,
        ))
    return entries


def mangaupdates_id_from_link(value: str | None) -> int | None:
    """MangaDex stores MangaUpdates links either as the current base-36 slug
    ("pb8uwds", which decodes to the API's series id) or as a legacy numeric
    id from the old site, which the API no longer accepts."""
    value = (value or "").strip().lower()
    if not value or not value.isalnum():
        return None
    if value.isdigit() and int(value) < 10_000_000:
        return None  # legacy id
    try:
        return int(value, 36)
    except ValueError:
        return None


async def _fetch_mangadex(config: dict, statuses: list[str]) -> list[ListEntry]:
    from .sources.mangadex import source as mangadex

    try:
        records = await mangadex.library_manga(statuses)
    except RuntimeError as exc:
        raise ImportListError(
            f"{exc} — MangaDex lists use the MangaDex account from Settings"
        ) from None
    entries = []
    for record in records:
        attrs = record.get("attributes") or {}
        title, alt_titles = mangadex._pick_title(attrs)
        links = attrs.get("links") or {}
        al = str(links.get("al") or "")
        entries.append(ListEntry(
            key=f"mangadex:{record['id']}", title=title, alt_titles=alt_titles,
            anilist_id=int(al) if al.isdigit() else None,
            mangaupdates_id=mangaupdates_id_from_link(links.get("mu")),
            year=attrs.get("year"),
        ))
    return entries


async def _mu_call(method: str, path: str, token: str = "", **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await rl_request(
        _http(), method, f"{MU_API_URL}{path}", limiter=_mu_limiter, headers=headers, **kwargs
    )


async def _fetch_mangaupdates(config: dict, statuses: list[str]) -> list[ListEntry]:
    login = await _mu_call("PUT", "/account/login", json={
        "username": (config.get("username") or "").strip(),
        "password": config.get("password") or "",
    })
    if login.status_code in (400, 401, 403):
        raise ImportListError("MangaUpdates rejected the username or password")
    login.raise_for_status()
    token = ((login.json().get("context") or {}).get("session_token")) or ""
    if not token:
        raise ImportListError("MangaUpdates login returned no session")
    entries: list[ListEntry] = []
    seen: set[int] = set()
    try:
        for list_id in statuses:
            page = 1
            while True:
                resp = await _mu_call(
                    "POST", f"/lists/{list_id}/search", token,
                    json={"page": page, "perpage": 100},
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results") or []
                for item in results:
                    series = ((item.get("record") or {}).get("series")) or {}
                    series_id = series.get("id")
                    if not series_id or int(series_id) in seen:
                        continue
                    seen.add(int(series_id))
                    entries.append(ListEntry(
                        key=f"mangaupdates:{series_id}", title=series.get("title") or "",
                        mangaupdates_id=int(series_id),
                    ))
                if len(results) < 100 or page * 100 >= int(data.get("total_hits") or 0):
                    break
                page += 1
    finally:
        try:
            await _mu_call("POST", "/account/logout", token)
        except Exception:  # a dangling session expires on its own
            pass
    return entries


_FETCHERS = {
    "anilist": _fetch_anilist,
    "myanimelist": _fetch_myanimelist,
    "mangadex": _fetch_mangadex,
    "mangaupdates": _fetch_mangaupdates,
}


async def fetch_entries(kind: str, config: dict) -> list[ListEntry]:
    info = PROVIDERS.get(kind)
    if info is None:
        raise ImportListError(f"Unknown list type {kind!r}")
    if info.needs_username and not (config.get("username") or "").strip():
        raise ImportListError(f"{info.label} lists need a username")
    if info.needs_password and not config.get("password"):
        raise ImportListError(f"{info.label} lists need the account password")
    if info.needs_client_id and not (config.get("client_id") or "").strip():
        raise ImportListError(f"{info.label} lists need an API client ID")
    try:
        entries = await _FETCHERS[kind](config, statuses_of(kind, config))
    except ImportListError:
        raise
    except httpx.HTTPError as exc:
        raise ImportListError(f"{info.label} request failed: {exc}") from exc
    # one entry per key even when statuses overlap
    return list({e.key: e for e in entries}.values())


async def resolve_mangaupdates_id(entry: ListEntry) -> int | None:
    """For an entry with no metadata id (a MyAnimeList title AniList doesn't
    know), a MangaUpdates series whose title matches exactly."""
    wanted = {n for t in [entry.title, *entry.alt_titles] if (n := normalize_title(t))}
    if not wanted:
        return None
    for cand in await mangaupdates.search(entry.title, limit=10):
        keys = {n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))}
        if wanted & keys:
            return int(cand.provider_id)
    return None


# --------------------------------------------------------------------- sync

@dataclass
class PreviewItem:
    entry: ListEntry
    action: str  # add | in_library | seen
    series_id: int | None = None


@dataclass
class SyncResult:
    fetched: int = 0
    added: int = 0
    existing: int = 0
    failed: int = 0
    preview: list[PreviewItem] = field(default_factory=list)


# one sync at a time: a manual "Sync now" racing the scheduled pass could
# otherwise add the same entry twice
_sync_lock = asyncio.Lock()


async def preview_list(
    session: AsyncSession, kind: str, config: dict, recorded: dict[str, ImportListEntry]
) -> SyncResult:
    entries = await fetch_entries(kind, config)
    result = SyncResult(fetched=len(entries))
    library = await LibraryIndex.load(session)
    for entry in entries:
        rec = recorded.get(entry.key)
        if rec is not None and rec.status != "failed":
            result.preview.append(PreviewItem(entry, "seen", rec.series_id))
            continue
        existing = library.find(
            entry.anilist_id, entry.mangaupdates_id, [entry.title, *entry.alt_titles]
        )
        if existing is not None:
            result.preview.append(PreviewItem(entry, "in_library", existing))
        else:
            result.preview.append(PreviewItem(entry, "add"))
    return result


async def _add_entry(entry: ListEntry, opts: AddOptions) -> tuple[str, int | None, str]:
    """(status, series id, detail) of adding one entry. Runs in its own
    session: a failed commit must not poison the sync's bookkeeping."""
    ids: dict[str, int] = {}
    # the list's own site is the better identity when it is a metadata
    # provider (AniList keeps English titles and covers); MangaUpdates
    # otherwise, falling back to a title search
    if entry.anilist_id is not None:
        ids["anilist_id"] = entry.anilist_id
    elif entry.mangaupdates_id is not None:
        ids["mangaupdates_id"] = entry.mangaupdates_id
    else:
        try:
            mu_id = await resolve_mangaupdates_id(entry)
        except Exception as exc:
            return "failed", None, f"metadata search failed: {exc}"
        if mu_id is None:
            return "failed", None, "no matching MangaUpdates or AniList entry"
        ids["mangaupdates_id"] = mu_id
    async with session_scope() as session:
        try:
            series = await create_series(session, opts, **ids)
        except SeriesExists as exc:
            return "existing", exc.series_id, ""
        except Exception as exc:
            log.warning("import list: adding %r failed: %s", entry.title, exc)
            return "failed", None, str(exc)
    return "added", series.id, ""


async def sync_list(session: AsyncSession, lst: ImportList) -> SyncResult:
    """Fetch the list and add every entry not handled before. The list and
    its entries must be loaded; this commits."""
    async with _sync_lock:
        recorded = {e.key: e for e in lst.entries}
        config = load_config(lst)
        try:
            if lst.root_folder_id is None or await session.get(RootFolder, lst.root_folder_id) is None:
                raise ImportListError("The list has no root folder to add series to")
            result = await preview_list(session, lst.kind, config, recorded)
        except Exception as exc:
            lst.last_error = str(exc) or type(exc).__name__
            await session.commit()
            raise
        opts = AddOptions(
            root_folder_id=lst.root_folder_id,
            monitored=lst.monitored,
            monitor_mode=lst.monitor_mode,
            alt_titles=[],
        )
        added_ids: list[int] = []
        for item in result.preview:
            if item.action == "seen":
                continue
            entry = item.entry
            if item.action == "in_library":
                status, series_id, detail = "existing", item.series_id, ""
            else:
                opts.alt_titles = entry.alt_titles
                status, series_id, detail = await _add_entry(entry, opts)
            if status == "added":
                result.added += 1
                added_ids.append(series_id)
            elif status == "existing":
                result.existing += 1
            else:
                result.failed += 1
            rec = recorded.get(entry.key)
            if rec is None:
                rec = ImportListEntry(key=entry.key)
                lst.entries.append(rec)
                recorded[entry.key] = rec
            rec.title = entry.title
            rec.anilist_id = entry.anilist_id
            rec.mangaupdates_id = entry.mangaupdates_id
            rec.status, rec.series_id, rec.detail = status, series_id, detail
        lst.last_synced_at = datetime.now(timezone.utc)
        lst.last_error = ""
        await session.commit()
        if added_ids:
            log.info("import list %r added %d series", lst.name, len(added_ids))
            start_refreshes(added_ids, grab_missing=lst.search_now)
        return result


async def load_list(session: AsyncSession, list_id: int) -> ImportList | None:
    result = await session.execute(
        select(ImportList).options(selectinload(ImportList.entries)).where(ImportList.id == list_id)
    )
    return result.scalar_one_or_none()


async def sync_all_lists() -> None:
    """Scheduled job: sync every enabled list."""
    async with session_scope() as session:
        list_ids = [
            row[0] for row in (await session.execute(
                select(ImportList.id).where(ImportList.enabled == True)  # noqa: E712
            )).all()
        ]
    for list_id in list_ids:
        try:
            async with session_scope() as session:
                lst = await load_list(session, list_id)
                if lst is not None:
                    await sync_list(session, lst)
        except ImportListError as exc:
            log.warning("import list %d: %s", list_id, exc)
        except Exception:
            # one broken list must not stop the others
            log.exception("import list %d sync failed", list_id)
