"""Direct (HTTP) chapter downloader: pages → CBZ → library."""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

import httpx

from .. import USER_AGENT
from ..library.matcher import PageSignature, archive_source_page_signature, page_bytes_signature
from ..models import Chapter, Series
from ..sources.base import DirectSource
from ..sources.routing import validate_proxy_url
from .cbz import build_comicinfo, guess_extension, write_cbz
from .images import ImageOptimization, optimize_page_async

log = logging.getLogger(__name__)

PAGE_CONCURRENCY = 3
MAX_CHAPTER_PAGES = 1_200
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_CHAPTER_BYTES = 128 * 1024 * 1024

ProgressCallback = Callable[[int, int], None | Awaitable[None]]
CancelCallback = Callable[[], None | Awaitable[None]]


class DuplicateChapterPagesError(RuntimeError):
    """A source advertised an existing chapter's exact pages as a new one."""

    def __init__(self, chapter_number: float, existing_number: float, existing_path: str):
        self.chapter_number = chapter_number
        self.existing_number = existing_number
        self.existing_path = existing_path
        super().__init__(
            f"chapter {chapter_number:g} has the same ordered page images as "
            f"chapter {existing_number:g} ({existing_path})"
        )


class DownloadLimitError(ValueError):
    """A provider response exceeds a download safety limit."""


class _BoundedPageStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, *, discard: bool,
                 account_bytes: Callable[[int], None]) -> None:
        self.stream = stream
        self.discard = discard
        self.account_bytes = account_bytes

    async def __aiter__(self):
        if self.discard:
            return
        size = 0
        async for chunk in self.stream:
            self.account_bytes(len(chunk))
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                raise DownloadLimitError("page exceeds the download byte limit")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


async def _bound_page_response(response: httpx.Response, *, account_bytes: Callable[[int], None]) -> None:
    # HTTPX can buffer redirect bodies before it returns the final response.
    # Discard them and reject encodings that could inflate before a size check.
    discard = not response.is_success
    if not discard:
        encoding = response.headers.get("Content-Encoding", "identity")
        if encoding.strip().lower() != "identity":
            raise DownloadLimitError("unsupported page content encoding")
        length = response.headers.get("Content-Length", "")
        if length.isdecimal() and int(length) > MAX_PAGE_BYTES:
            raise DownloadLimitError("page exceeds the download byte limit")
    response.stream = _BoundedPageStream(response.stream, discard=discard, account_bytes=account_bytes)


def _reject_duplicate_pages(
    series: Series, chapter: Chapter, dest_path: Path, candidate: PageSignature
) -> None:
    checked: set[str] = set()
    for existing in getattr(series, "chapters", []):
        if existing is chapter or (chapter.id is not None and existing.id == chapter.id):
            continue
        if not existing.downloaded or not existing.file_path:
            continue
        path = Path(existing.file_path)
        try:
            key = str(path.resolve(strict=False))
            if key == str(dest_path.resolve(strict=False)) or key in checked:
                continue
        except OSError:
            key = str(path.absolute())
        checked.add(key)
        if archive_source_page_signature(path) == candidate:
            raise DuplicateChapterPagesError(
                chapter.number, existing.number, existing.file_path
            )


async def download_chapter_to_cbz(
    source: DirectSource,
    chapter_external_id: str,
    series: Series,
    chapter: Chapter,
    dest_path,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: CancelCallback | None = None,
    web_url: str = "",
    optimization: ImageOptimization = ImageOptimization(),
) -> None:
    """Fetches all pages of a chapter and writes the CBZ to dest_path.
    progress_cb(done, total) is called as pages finish."""
    # Snapshot routing before the manifest request yields to other jobs.
    proxy_enabled = source.content_proxy_enabled
    proxy_url = source.content_proxy_url
    if proxy_enabled and not proxy_url:
        # Never turn a proxy configuration error into a direct request. This
        # can only happen if the database was edited outside the settings API.
        raise RuntimeError(f"{source.name} content proxy is enabled but has no URL")

    if proxy_enabled:
        validate_proxy_url(proxy_url, f"{source.name} content proxy")

    page_urls = await source.get_pages(chapter_external_id)
    if not page_urls:
        raise RuntimeError(f"{source.name} returned no pages for chapter {chapter.number}")
    if len(page_urls) > MAX_CHAPTER_PAGES:
        raise DownloadLimitError("chapter exceeds the page count limit")

    async def check_cancelled() -> None:
        if cancel_cb is None:
            return
        result = cancel_cb()
        if inspect.isawaitable(result):
            await result

    await check_cancelled()

    pages: list[bytes | None] = [None] * len(page_urls)
    original_identities: list[tuple[int, int] | None] = [None] * len(page_urls)
    done = 0
    downloaded_bytes = 0
    retained_bytes = 0
    transferred_bytes = 0
    sem = asyncio.Semaphore(PAGE_CONCURRENCY)

    def account_bytes(size: int) -> None:
        nonlocal transferred_bytes
        transferred_bytes += size
        if transferred_bytes > MAX_CHAPTER_BYTES:
            raise DownloadLimitError("chapter exceeds the download byte limit")

    client_options = {
        "headers": {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        "event_hooks": {"response": [partial(_bound_page_response, account_bytes=account_bytes)]},
        "timeout": 120,
        "follow_redirects": True,
        # Unchecked means direct even if the container inherits HTTP_PROXY.
        "trust_env": False,
    }
    if proxy_enabled:
        client_options["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_options) as client:

        async def fetch(i: int, url: str) -> None:
            nonlocal done, downloaded_bytes, retained_bytes
            async with sem:
                # retry any per-page error, not just httpx's: source
                # download_page overrides can fail in their own ways
                # (decryption, unexpected payloads) and deserve the same
                # second chance as a network blip
                for attempt in range(3):
                    try:
                        content = await source.download_page(client, url)
                        if not guess_extension(content, fallback=""):
                            raise ValueError("response is not a supported image")
                        break
                    except DownloadLimitError:
                        raise
                    except Exception as exc:
                        if attempt == 2:
                            if proxy_enabled and isinstance(exc, httpx.HTTPError):
                                # Proxy errors can include the address and userinfo.
                                detail = (
                                    f"HTTP {exc.response.status_code}"
                                    if isinstance(exc, httpx.HTTPStatusError)
                                    else type(exc).__name__
                                )
                                message = (
                                    f"page {i + 1} failed through configured proxy "
                                    f"({detail}); no direct fallback"
                                )
                                if isinstance(exc, httpx.HTTPStatusError):
                                    cause = httpx.HTTPStatusError(
                                        message, request=exc.request, response=exc.response
                                    )
                                else:
                                    cause = httpx.TransportError(message)
                                raise RuntimeError(message) from cause
                            raise RuntimeError(f"page {i + 1} failed: {exc}") from exc
                        await asyncio.sleep(2 * (attempt + 1))
                if len(content) > MAX_PAGE_BYTES:
                    raise DownloadLimitError("page exceeds the download byte limit")
                downloaded_bytes += len(content)
                if downloaded_bytes > MAX_CHAPTER_BYTES:
                    raise DownloadLimitError("chapter exceeds the download byte limit")
                original_identities[i] = page_bytes_signature([content])[0]
                # Transform while this fetch owns its bounded page slot. Only
                # compressed output is retained for the rest of the chapter;
                # progress counts optimized pages, keeping the stall watchdog
                # alive even for long chapters. Codec work has a global slot.
                await check_cancelled()
                optimized = await optimize_page_async(content, optimization)
                retained_bytes += len(optimized)
                if retained_bytes > MAX_CHAPTER_BYTES:
                    raise DownloadLimitError("chapter exceeds the archive byte limit")
                pages[i] = optimized
                del content
            done += 1
            await check_cancelled()
            if progress_cb:
                result = progress_cb(done, len(page_urls))
                if inspect.isawaitable(result):
                    await result
            await check_cancelled()

        # TaskGroup cancels the remaining fetches when one fails permanently —
        # gather() would leave them running against a client being closed
        try:
            async with asyncio.TaskGroup() as tg:
                for i, u in enumerate(page_urls):
                    tg.create_task(fetch(i, u))
        except* Exception as group:
            # keep the page's own cause (a network blip or not decides how
            # soon the chapter is retried), but not the group as context
            first = group.exceptions[0]
            raise first from first.__cause__

    if any(p is None for p in pages):
        raise RuntimeError("some pages failed to download")

    await check_cancelled()
    complete_pages: list[bytes] = pages  # type: ignore[assignment]
    source_signature: PageSignature = tuple(original_identities)  # type: ignore[arg-type]
    _reject_duplicate_pages(series, chapter, Path(dest_path), source_signature)

    original_signature = None
    if optimization.enabled:
        # No provenance is needed when every page kept its byte identity.
        if page_bytes_signature(complete_pages) != source_signature:
            original_signature = source_signature
        log.info("Chapter image optimization: %d → %d bytes (%s, quality %d)",
                 sum(size for _, size in source_signature), sum(map(len, complete_pages)),
                 optimization.format, optimization.quality)

    # The final cancellation/library check and atomic write stay together,
    # with no await after the check.
    await check_cancelled()
    provider_title = getattr(series, "provider_title", "")
    localized_series = provider_title if provider_title and series.title != provider_title else None
    comicinfo = build_comicinfo(
        series=series.title,
        number=chapter.number,
        volume=chapter.volume,
        title=chapter.title,
        summary=series.description if chapter.number in (0, 1) else "",
        web=web_url,
        page_count=len(pages),
        localized_series=localized_series,
    )
    if original_signature is None:
        write_cbz(dest_path, complete_pages, comicinfo)
    else:
        write_cbz(dest_path, complete_pages, comicinfo, original_signature=original_signature)
    log.info("Wrote %s (%d pages)", dest_path, len(pages))
