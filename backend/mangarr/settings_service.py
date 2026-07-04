"""Runtime-editable settings stored in the Settings table."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

DEFAULTS: dict[str, str] = {
    # Media management
    "naming_template": "{series} - Vol. {volume:02d} Ch. {chapter:04.1f}",
    "naming_template_no_volume": "{series} - Ch. {chapter:04.1f}",
    # Source priority: comma-separated source names, first = preferred
    "source_priority": "mangadex,weebcentral,nyaa",
    # MangaDex credentials (personal API client)
    "mangadex_client_id": "",
    "mangadex_client_secret": "",
    "mangadex_username": "",
    "mangadex_password": "",
    "mangadex_language": "en",
    # qBittorrent
    "qbittorrent_url": "http://localhost:8080",
    "qbittorrent_username": "admin",
    "qbittorrent_password": "",
    "qbittorrent_category": "mangarr",
    "qbittorrent_enabled": "false",
    # Sources on/off
    "source_mangadex_enabled": "true",
    "source_weebcentral_enabled": "true",
    "source_nyaa_enabled": "false",
    # Jobs
    "monitor_interval_minutes": "15",
}

SECRET_KEYS = {"mangadex_client_secret", "mangadex_password", "qbittorrent_password"}


async def get_all(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(Setting))).scalars().all()
    values = dict(DEFAULTS)
    values.update({r.key: r.value for r in rows if r.key in DEFAULTS})
    return values


async def get(session: AsyncSession, key: str) -> str:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


async def set_many(session: AsyncSession, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    await session.commit()
