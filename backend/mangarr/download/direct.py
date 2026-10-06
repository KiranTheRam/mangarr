"""Direct (HTTP) chapter downloader: pages → CBZ → library."""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .. import USER_AGENT
from ..library.matcher import archive_page_signature, page_bytes_signature
from ..models import Chapter, Series
from ..sources.base import DirectSource
from .cbz import build_comicinfo, guess_extension, write_cbz

log = logging.getLogger(__name__)

PAGE_CONCURRENCY = 3

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


def _reject_duplicate_pages(
    series: Series, chapter: Chapter, dest_path: Path, pages: list[bytes]
) -> None:
    candidate = page_bytes_signature(pages)
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
        if archive_page_signature(path) == candidate:
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

    page_urls = await source.get_pages(chapter_external_id)
    if not page_urls:
        raise RuntimeError(f"{source.name} returned no pages for chapter {chapter.number}")

    async def check_cancelled() -> None:
        if cancel_cb is None:
            return
        result = cancel_cb()
        if inspect.isawaitable(result):
            await result

    await check_cancelled()

    pages: list[bytes | None] = [None] * len(page_urls)
    done = 0
    sem = asyncio.Semaphore(PAGE_CONCURRENCY)

    client_options = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": 120,
        "follow_redirects": True,
        # Unchecked means direct even if the container inherits HTTP_PROXY.
        "trust_env": False,
    }
    if proxy_enabled:
        client_options["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_options) as client:

        async def fetch(i: int, url: str) -> None:
            nonlocal done
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
                        pages[i] = content
                        break
                    except Exception as exc:
                        if attempt == 2:
                            raise RuntimeError(f"page {i + 1} failed: {exc}") from exc
                        await asyncio.sleep(2 * (attempt + 1))
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
            raise group.exceptions[0] from None

    if any(p is None for p in pages):
        raise RuntimeError("some pages failed to download")

    await check_cancelled()

    complete_pages: list[bytes] = pages  # type: ignore[assignment]
    _reject_duplicate_pages(series, chapter, Path(dest_path), complete_pages)

    comicinfo = build_comicinfo(
        series=series.title,
        number=chapter.number,
        volume=chapter.volume,
        title=chapter.title,
        summary=series.description if chapter.number in (0, 1) else "",
        web=web_url,
        page_count=len(pages),
    )
    write_cbz(dest_path, complete_pages, comicinfo)
    log.info("Wrote %s (%d pages)", dest_path, len(pages))
