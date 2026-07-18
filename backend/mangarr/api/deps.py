from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db import get_session
from ..models import ApiKey, utcnow

_api_key: str | None = None


def get_api_key() -> str:
    global _api_key
    if _api_key is None:
        _api_key = config.resolve_api_key()
    return _api_key


def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps for safe comparison with ``utcnow()``.

    SQLite does not preserve the timezone offset for ``DateTime`` columns, so
    values read back from it are naïve even when the model requests timezone
    awareness. Mangarr writes UTC timestamps, therefore a naïve value from the
    database represents UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def require_api_key(
    x_api_key: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> None:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # Bootstrap key handed to the web UI via initialize.json.
    if x_api_key == get_api_key():
        return
    # User-managed keys created in Settings.
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key == x_api_key))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = utcnow()
    # Throttle writes: only record use at most once a minute per key.
    last_used_at = _as_utc(row.last_used_at) if row.last_used_at is not None else None
    if last_used_at is None or now - last_used_at > timedelta(minutes=1):
        row.last_used_at = now
        await session.commit()
