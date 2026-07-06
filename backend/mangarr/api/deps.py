from fastapi import Header, HTTPException

from ..config import config

_api_key: str | None = None


def get_api_key() -> str:
    global _api_key
    if _api_key is None:
        _api_key = config.resolve_api_key()
    return _api_key


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != get_api_key():
        raise HTTPException(status_code=401, detail="Invalid API key")
