from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import kavita, notifications, settings_service
from ..db import get_session
from ..download.qbittorrent import QbtError, test_connection
from ..schemas import (
    KavitaLibraryOut,
    KavitaTestIn,
    KavitaTestOut,
    QbtTestIn,
    WebhookTestIn,
)

router = APIRouter(prefix="/settings", tags=["settings"])

MASK = "••••••••"

# Exact matching includes scheme, port and base path: a reverse proxy can
# route two paths on one host to entirely different services.
SECRET_DESTINATIONS = {
    "webhook_secret": ("webhook_url",),
    "kavita_api_key": ("kavita_url",),
    "qbittorrent_password": ("qbittorrent_url", "qbittorrent_username"),
}


def retained_secret(current: dict[str, str], key: str, destination: dict[str, str]) -> str:
    if any(destination[k] != current.get(k, "") for k in SECRET_DESTINATIONS[key]):
        raise HTTPException(422, f"Destination changed: re-enter {key} before testing or saving")
    return current.get(key, "")


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
    try:
        current = await settings_service.get_all(session)
        merged = {**current, **to_save}
        for key, fields in SECRET_DESTINATIONS.items():
            if current.get(key) and key not in to_save:
                retained_secret(current, key, {k: merged[k] for k in fields})
        settings_service.validate(merged)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await settings_service.set_many(session, to_save)
    # apply a changed monitor interval immediately (no restart needed)
    if "monitor_interval_minutes" in to_save:
        from ..jobs.scheduler import reschedule_monitor

        reschedule_monitor(int(to_save["monitor_interval_minutes"]))
    if "import_list_sync_hours" in to_save:
        from ..jobs.scheduler import reschedule_import_lists

        reschedule_import_lists(int(to_save["import_list_sync_hours"]))
    return await get_settings(session)


@router.post("/webhook/test")
async def webhook_test(body: WebhookTestIn, session: AsyncSession = Depends(get_session)):
    secret = body.secret
    if secret == MASK:
        secret = retained_secret(await settings_service.get_all(session), "webhook_secret", {"webhook_url": body.url})
    ok = await notifications.send_webhook(
        body.url, secret, {"app": notifications.APP_NAME, "event": "test", "series_id": 0}
    )
    if not ok:
        raise HTTPException(400, "Webhook endpoint rejected the test event or is unreachable")
    return {"ok": True}


@router.post("/kavita/test", response_model=KavitaTestOut)
async def kavita_test(body: KavitaTestIn, session: AsyncSession = Depends(get_session)):
    """Verify the connection and return the libraries, so the settings page can
    offer real libraries to map root folders onto."""
    api_key = body.api_key
    if api_key == MASK or not api_key:
        api_key = retained_secret(await settings_service.get_all(session), "kavita_api_key", {"kavita_url": body.url})
    try:
        version, libraries = await kavita.test_connection(body.url, api_key)
    except kavita.KavitaError as exc:
        raise HTTPException(400, str(exc)) from exc
    return KavitaTestOut(
        ok=True, version=version,
        libraries=[
            KavitaLibraryOut(id=lib.id, name=lib.name, folders=lib.folders)
            for lib in libraries
        ],
    )


@router.post("/qbittorrent/test")
async def qbt_test(body: QbtTestIn, session: AsyncSession = Depends(get_session)):
    password = body.password
    if password == MASK:
        password = retained_secret(await settings_service.get_all(session), "qbittorrent_password", {
            "qbittorrent_url": body.url, "qbittorrent_username": body.username,
        })
    try:
        version = await test_connection(body.url, body.username, password)
    except QbtError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "version": version}
