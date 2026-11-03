"""Task-local routing for shared source instances.

Each API request/worker reads one settings snapshot. Child tasks inherit it;
other requests cannot mutate it. Clients are scoped to an operation, so proxy
changes never leave a stale connection pool or close another task's client.
"""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

SOURCE_PROXY_NAMES = ("mangafire", "weebcentral")


@dataclass(frozen=True)
class SourceRoute:
    all_requests: bool = False
    proxy_url: str = field(default="", repr=False)
    content_enabled: bool = False
    content_url: str = field(default="", repr=False)


_routes: ContextVar[dict[str, SourceRoute]] = ContextVar("source_routes", default={})


def set_routes(routes: dict[str, SourceRoute]) -> None:
    _routes.set(dict(routes))


def route_for(name: str) -> SourceRoute | None:
    return _routes.get().get(name)


def validate_proxy_url(url: str, key: str) -> None:
    """Validate without reflecting credentials into an API error."""
    try:
        parsed = urlsplit(url)
        valid = (
            url == url.strip()
            and not any(char.isspace() or ord(char) < 32 for char in url)
            and parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.port != 0
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{key} must be a valid http:// or https:// proxy URL")


@asynccontextmanager
async def source_client(name: str, headers: dict, injected: httpx.AsyncClient | None):
    route = route_for(name) or SourceRoute()
    if route.all_requests:
        if not route.proxy_url:
            raise RuntimeError(f"{name} source proxy is enabled but has no URL")
        validate_proxy_url(route.proxy_url, f"{name} source proxy")
    # Injected clients are for adapter tests; never let one bypass a selected proxy.
    if injected is not None and not route.all_requests:
        yield injected
        return
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=60, trust_env=False, follow_redirects=True,
            proxy=route.proxy_url if route.all_requests else None,
        ) as client:
            yield client
    except httpx.HTTPStatusError as exc:
        if not route.all_requests:
            raise
        raise httpx.HTTPStatusError(
            f"{name} returned HTTP {exc.response.status_code} through the source proxy",
            request=exc.request, response=exc.response,
        ) from None
    except httpx.HTTPError as exc:
        if not route.all_requests:
            raise
        raise httpx.ProxyError(
            f"{name} source proxy request failed ({type(exc).__name__}); no direct fallback"
        ) from None
