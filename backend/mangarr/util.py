import asyncio
import re
import time


class RateLimiter:
    """Simple async token-bucket limiter shared per source."""

    def __init__(self, rate: float, per_seconds: float = 1.0) -> None:
        self._interval = per_seconds / rate
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    cleaned = ILLEGAL_PATH_CHARS.sub("", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", cleaned) or "Unknown"


# "c002", "ch 21", "Ch. 21", "Chapter 3" — up to two separator chars
# so dotted forms like "Ch. 21" parse too
CHAPTER_PREFIX_PATTERN = re.compile(r"\bc(?:h(?:apter)?)?[ ._]{0,2}(\d+(?:\.\d+)?)", re.I)
TRAILING_NUMBER_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*$")
BRACKET_GROUPS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
VOLUME_PATTERN = re.compile(r"\bv(?:ol(?:ume)?)?[ ._]{0,2}(\d+)", re.I)


def parse_chapter_number(text: str) -> float | None:
    m = CHAPTER_PREFIX_PATTERN.search(text)
    if m:
        return float(m.group(1))
    # scene-style names bury the chapter before tag groups:
    # "Kagurabachi 057 (2024) (Digital) (1r0n)" → strip (…)/[…], then the
    # chapter is the trailing number
    stripped = BRACKET_GROUPS.sub(" ", text).strip()
    m = TRAILING_NUMBER_PATTERN.search(stripped)
    if m:
        return float(m.group(1))
    return None


def parse_volume_number(text: str) -> int | None:
    m = VOLUME_PATTERN.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def normalize_title(title: str) -> str:
    """Loose normalization for cross-source title matching."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()
