from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import settings_service
from ..db import get_session
from ..download.qbittorrent import QbtError, test_connection
from ..schemas import QbtTestIn

router = APIRouter(prefix="/settings", tags=["settings"])

MASK = "••••••••"


@router.get("")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    values = await settings_service.get_all(session)
    for key in settings_service.SECRET_KEYS:
        if values.get(key):
            values[key] = MASK
    return values


@router.put("")
async def update_settings(
    body: dict[str, str], session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    # ignore masked secrets that the user did not change
    to_save = {k: v for k, v in body.items() if v != MASK}
    await settings_service.set_many(session, to_save)
    # apply a changed monitor interval immediately (no restart needed)
    if "monitor_interval_minutes" in to_save:
        from ..jobs.scheduler import reschedule_monitor

        try:
            reschedule_monitor(int(to_save["monitor_interval_minutes"]))
        except (ValueError, TypeError):
            pass
    return await get_settings(session)


@router.post("/qbittorrent/test")
async def qbt_test(body: QbtTestIn, session: AsyncSession = Depends(get_session)):
    password = body.password
    if password == MASK:
        password = await settings_service.get(session, "qbittorrent_password")
    try:
        version = await test_connection(body.url, body.username, password)
    except QbtError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "version": version}
