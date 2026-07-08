"""Direct (HTTP) chapter downloader: pages → CBZ → library."""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

import httpx

from .. import USER_AGENT
from ..models import Chapter, Series
from ..sources.base import DirectSource
from .cbz import build_comicinfo, write_cbz

log = logging.getLogger(__name__)

PAGE_CONCURRENCY = 3

ProgressCallback = Callable[[int, int], None | Awaitable[None]]


async def download_chapter_to_cbz(
    source: DirectSource,
    chapter_external_id: str,
    series: Series,
    chapter: Chapter,
    dest_path,
    progress_cb: ProgressCallback | None = None,
    web_url: str = "",
) -> None:
    """Fetches all pages of a chapter and writes the CBZ to dest_path.
    progress_cb(done, total) is called as pages finish."""
    page_urls = await source.get_pages(chapter_external_id)
    if not page_urls:
        raise RuntimeError(f"{source.name} returned no pages for chapter {chapter.number}")

    pages: list[bytes | None] = [None] * len(page_urls)
    done = 0
    sem = asyncio.Semaphore(PAGE_CONCURRENCY)

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True
    ) as client:

        async def fetch(i: int, url: str) -> None:
            nonlocal done
            async with sem:
                # retry any per-page error, not just httpx's: source
                # download_page overrides can fail in their own ways
                # (decryption, unexpected payloads) and deserve the same
                # second chance as a network blip
                for attempt in range(3):
                    try:
                        pages[i] = await source.download_page(client, url)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            raise RuntimeError(f"page {i + 1} failed: {exc}") from exc
                        await asyncio.sleep(2 * (attempt + 1))
            done += 1
            if progress_cb:
                result = progress_cb(done, len(page_urls))
                if inspect.isawaitable(result):
                    await result

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

    comicinfo = build_comicinfo(
        series=series.title,
        number=chapter.number,
        volume=chapter.volume,
        title=chapter.title,
        summary=series.description if chapter.number in (0, 1) else "",
        web=web_url,
        page_count=len(pages),
    )
    write_cbz(dest_path, pages, comicinfo)  # type: ignore[arg-type]
    log.info("Wrote %s (%d pages)", dest_path, len(pages))
