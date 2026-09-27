import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..import_lists import (
    PROVIDERS,
    SECRET_FIELDS,
    ImportListError,
    load_config,
    load_list,
    preview_list,
    statuses_of,
    sync_list,
)
from ..models import ImportList, ImportListEntry, RootFolder
from ..schemas import (
    ImportListEntryOut,
    ImportListIn,
    ImportListOut,
    ImportListPreviewItemOut,
    ImportListSyncOut,
)
from .settings import MASK

router = APIRouter(prefix="/importlists", tags=["import lists"])


class ProviderOut(BaseModel):
    kind: str
    label: str
    statuses: dict[str, str]
    default_statuses: list[str]
    needs_username: bool
    needs_password: bool
    needs_client_id: bool


class SkipIn(BaseModel):
    key: str
    title: str = ""


async def _out(session: AsyncSession, lst: ImportList) -> ImportListOut:
    config = load_config(lst)
    counts = dict((await session.execute(
        select(ImportListEntry.status, func.count())
        .where(ImportListEntry.list_id == lst.id)
        .group_by(ImportListEntry.status)
    )).all())
    return ImportListOut(
        id=lst.id,
        name=lst.name,
        kind=lst.kind,
        enabled=lst.enabled,
        username=config.get("username") or "",
        password=MASK if config.get("password") else "",
        client_id=config.get("client_id") or "",
        statuses=statuses_of(lst.kind, config),
        root_folder_id=lst.root_folder_id or 0,
        monitored=lst.monitored,
        monitor_mode=lst.monitor_mode,
        search_now=lst.search_now,
        last_synced_at=lst.last_synced_at,
        last_error=lst.last_error,
        entry_counts=counts,
    )


async def _apply(session: AsyncSession, lst: ImportList, body: ImportListIn) -> None:
    info = PROVIDERS[body.kind]
    if not body.name.strip():
        raise HTTPException(422, "name is required")
    unknown = [s for s in body.statuses if s not in info.statuses]
    if unknown:
        raise HTTPException(422, f"unknown {info.label} status(es): {', '.join(unknown)}")
    if await session.get(RootFolder, body.root_folder_id) is None:
        raise HTTPException(422, "Root folder not found")
    if body.monitor_mode == "from_chapter":
        raise HTTPException(422, "Import lists can't use a from-chapter threshold")
    old = load_config(lst) if lst.kind == body.kind else {}
    config = {
        "username": body.username.strip(),
        "client_id": body.client_id.strip(),
        "statuses": body.statuses or list(info.default_statuses),
    }
    for key in SECRET_FIELDS:
        value = getattr(body, key)
        config[key] = old.get(key, "") if value == MASK else value
    lst.name = body.name.strip()
    lst.kind = body.kind
    lst.enabled = body.enabled
    lst.config = json.dumps(config)
    lst.root_folder_id = body.root_folder_id
    lst.monitored = body.monitored
    lst.monitor_mode = body.monitor_mode
    lst.search_now = body.search_now


async def _require(session: AsyncSession, list_id: int) -> ImportList:
    lst = await load_list(session, list_id)
    if lst is None:
        raise HTTPException(404, "Import list not found")
    return lst


@router.get("/providers", response_model=list[ProviderOut])
async def providers():
    return [
        ProviderOut(
            kind=kind, label=info.label, statuses=info.statuses,
            default_statuses=list(info.default_statuses),
            needs_username=info.needs_username, needs_password=info.needs_password,
            needs_client_id=info.needs_client_id,
        )
        for kind, info in PROVIDERS.items()
    ]


@router.get("", response_model=list[ImportListOut])
async def list_import_lists(session: AsyncSession = Depends(get_session)):
    lists = (await session.execute(select(ImportList).order_by(ImportList.id))).scalars().all()
    return [await _out(session, lst) for lst in lists]


@router.post("", response_model=ImportListOut, status_code=201)
async def create_import_list(body: ImportListIn, session: AsyncSession = Depends(get_session)):
    lst = ImportList()
    await _apply(session, lst, body)
    session.add(lst)
    await session.commit()
    return await _out(session, lst)


@router.put("/{list_id}", response_model=ImportListOut)
async def update_import_list(
    list_id: int, body: ImportListIn, session: AsyncSession = Depends(get_session)
):
    lst = await _require(session, list_id)
    await _apply(session, lst, body)
    await session.commit()
    return await _out(session, lst)


@router.delete("/{list_id}", status_code=204)
async def delete_import_list(list_id: int, session: AsyncSession = Depends(get_session)):
    lst = await _require(session, list_id)
    await session.delete(lst)
    await session.commit()


@router.post("/preview", response_model=list[ImportListPreviewItemOut])
async def preview_import_list(
    body: ImportListIn, list_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    """What a sync of these (possibly unsaved) settings would do, without
    adding anything. With list_id, a masked password and the entries that
    list already handled come from the saved list."""
    lst = await _require(session, list_id) if list_id is not None else ImportList(kind=body.kind)
    scratch = ImportList(kind=lst.kind, config=lst.config or "{}")
    await _apply(session, scratch, body)
    recorded = {e.key: e for e in lst.entries} if list_id is not None else {}
    try:
        result = await preview_list(session, body.kind, load_config(scratch), recorded)
    except ImportListError as exc:
        raise HTTPException(400, str(exc)) from None
    return [
        ImportListPreviewItemOut(
            key=item.entry.key, title=item.entry.title, cover_url=item.entry.cover_url,
            year=item.entry.year, action=item.action, series_id=item.series_id,
        )
        for item in result.preview
    ]


@router.post("/{list_id}/sync", response_model=ImportListSyncOut)
async def sync_import_list(list_id: int, session: AsyncSession = Depends(get_session)):
    lst = await _require(session, list_id)
    try:
        result = await sync_list(session, lst)
    except ImportListError as exc:
        raise HTTPException(400, str(exc)) from None
    return ImportListSyncOut(
        fetched=result.fetched, added=result.added,
        existing=result.existing, failed=result.failed,
    )


@router.get("/{list_id}/entries", response_model=list[ImportListEntryOut])
async def list_entries(list_id: int, session: AsyncSession = Depends(get_session)):
    lst = await _require(session, list_id)
    return sorted(lst.entries, key=lambda e: (e.status != "failed", e.title.lower()))


@router.post("/{list_id}/entries/skip", response_model=ImportListEntryOut, status_code=201)
async def skip_entry(list_id: int, body: SkipIn, session: AsyncSession = Depends(get_session)):
    """Never add this entry (until it is forgotten)."""
    lst = await _require(session, list_id)
    entry = next((e for e in lst.entries if e.key == body.key), None)
    if entry is None:
        entry = ImportListEntry(key=body.key, title=body.title)
        lst.entries.append(entry)
    entry.status, entry.detail = "skipped", "skipped by user"
    await session.commit()
    return entry


@router.delete("/{list_id}/entries/{entry_id}", status_code=204)
async def forget_entry(list_id: int, entry_id: int, session: AsyncSession = Depends(get_session)):
    """Forget a handled entry so the next sync considers it again."""
    entry = await session.get(ImportListEntry, entry_id)
    if entry is None or entry.list_id != list_id:
        raise HTTPException(404, "Entry not found")
    await session.delete(entry)
    await session.commit()
