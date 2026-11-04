"""Toonily's public HTML catalog and reader."""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..download.cbz import guess_extension
from ..titles import source_title_key, unique_titles
from ..util import RateLimiter, rl_get_limited_bytes, rl_request
from .base import DirectSource, SourceChapter, SourceSeries
from .routing import validate_proxy_url

BASE_URL = "https://toonily.com"
BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SEARCH_PATH = re.compile(r"/search/[^/]+/?")
_CHAPTER = re.compile(r"^chapter\s+([0-9]{1,7}(?:\.[0-9]{1,7})?)(?:\s|$|[-:–—])", re.I)
_SIDE_STORY = re.compile(r"\bside[\s-]+stor(?:y|ies)\b", re.I)
_SLUG = re.compile(r"^[a-zA-Z0-9_-]+$")
_SEARCH_GLUE = {"a", "an", "and", "as", "at", "for", "i", "in", "is", "my", "of", "on", "the", "to"}
MAX_SEARCH_PAGES = 3
MAX_SEARCH_RESULTS = 54
MAX_ALIAS_LOOKUPS = 3
MAX_CHAPTER_CHECKS = 16
PROBE_BATCH_TIMEOUT = 24
MAX_READER_BYTES = 4 * 1024 * 1024
IMAGE_PREFIX_BYTES = 32
AVAILABLE_TTL = 120
UNAVAILABLE_TTL = 60
AVAILABILITY_CACHE_SIZE = 4096

log = logging.getLogger(__name__)
_limiter = RateLimiter(rate=1, per_seconds=1)
_image_limiter = RateLimiter(rate=3, per_seconds=1)


class _PageURL(str):
    """A normal image URL with request-local reader context."""

    def __new__(cls, url: str, referer: str):
        value = super().__new__(cls, url)
        value.referer = referer
        return value


def _site_path(url: str) -> str:
    parsed = urlsplit(urljoin(BASE_URL + "/", url))
    if parsed.scheme != "https" or parsed.netloc != "toonily.com":
        raise ValueError("Not a Toonily URL")
    parts = parsed.path.strip("/").split("/")
    if len(parts) not in (2, 3) or parts[0] != "serie" or not all(
        _SLUG.fullmatch(part) for part in parts[1:]
    ):
        raise ValueError("Invalid Toonily series or chapter path")
    return "/" + "/".join(parts) + "/"


def _chapter_path(external_id: str) -> str:
    path = _site_path(external_id)
    if path.count("/") != 4:
        raise ValueError("Invalid Toonily chapter ID")
    return path


def _search_link(current_url: str, href: str) -> str:
    try:
        parsed = urlsplit(urljoin(current_url, href))
    except ValueError:
        return ""
    if (
        parsed.scheme == "https"
        and parsed.netloc == "toonily.com"
        and not parsed.query
        and not parsed.fragment
        and "\\" not in parsed.path
    ):
        return parsed.path
    return ""


def _fallback_queries(query: str) -> list[str]:
    # Short phrases can survive Toonily's search tokenization when the full
    # title does not. Favor contiguous words with fewer articles and prepositions.
    words = re.findall(r"[^\W_]+", re.sub(r"['’]s\b", "", query, flags=re.I))
    if len(words) < 4:
        return []
    windows = [words[i:i + 3] for i in range(len(words) - 2)]
    windows.sort(key=lambda window: sum(word.casefold() in _SEARCH_GLUE for word in window))
    return list(dict.fromkeys(" ".join(window) for window in windows))[:2]


def _image_headers(page: _PageURL) -> dict[str, str]:
    headers = {"User-Agent": BROWSER_AGENT, "Referer": page.referer}
    # The CDN can otherwise serve a lossy WebP derivative for .jpg URLs.
    if urlsplit(page).path.lower().endswith((".jpg", ".jpeg")):
        headers["Accept"] = "image/jpeg"
    return headers


class ToonilySource(DirectSource):
    name = "toonily"
    image_limiter = _image_limiter
    manual_search_limit = MAX_SEARCH_RESULTS
    requires_chapter_verification = True

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": BROWSER_AGENT, "Referer": BASE_URL + "/"},
            timeout=60, trust_env=False,
        )
        self._availability: OrderedDict[tuple[str, str | None], tuple[float, bool]] = OrderedDict()

    async def _series_html(self, external_id: str, **kwargs) -> BeautifulSoup:
        if not _SLUG.fullmatch(external_id):
            raise ValueError("Invalid Toonily series ID")
        response = await rl_request(
            self._client, "GET", f"{BASE_URL}/serie/{external_id}/", limiter=_limiter,
            follow_redirects=False, **kwargs,
        )
        response.raise_for_status()
        return BeautifulSoup(response.content, "lxml", from_encoding="utf-8")

    async def _search_catalog(self, query: str, max_pages: int) -> dict[str, SourceSeries]:
        response = await rl_request(
            self._client, "GET", BASE_URL + "/", limiter=_limiter,
            params={"s": query, "post_type": "wp-manga"},
            follow_redirects=False, retries=0, timeout=8,
        )
        if response.is_redirect:
            path = _search_link(str(response.url), response.headers.get("location", ""))
            if _SEARCH_PATH.fullmatch(path):
                response = await rl_request(
                    self._client, "GET", BASE_URL + path, limiter=_limiter,
                    follow_redirects=False, retries=0, timeout=8,
                )
        response.raise_for_status()
        root = response.url.raw_path.decode("ascii").rstrip("/")
        results: dict[str, SourceSeries] = {}
        for page in range(1, max_pages + 1):
            soup = BeautifulSoup(response.content, "lxml", from_encoding="utf-8")
            previous_count = len(results)
            for link in soup.select(".post-title a[href]"):
                try:
                    path = _site_path(link["href"])
                except ValueError:
                    continue
                title = link.get_text(" ", strip=True)
                if path.count("/") != 3 or not title:
                    continue
                slug = path.split("/")[2]
                results.setdefault(slug, SourceSeries(self.name, slug, title, BASE_URL + path))
                if len(results) >= MAX_SEARCH_RESULTS:
                    return results

            if page == max_pages or len(results) == previous_count or not _SEARCH_PATH.fullmatch(root):
                break
            next_link = soup.select_one("a.nextpostslink[rel=next][href]")
            if next_link is None:
                break
            path = _search_link(str(response.url), next_link["href"])
            if path.rstrip("/") != f"{root}/page/{page + 1}":
                break
            try:
                response = await rl_request(
                    self._client, "GET", BASE_URL + path, limiter=_limiter,
                    follow_redirects=False, retries=0, timeout=8,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Toonily search page %d failed (%s)", page + 1, type(exc).__name__)
                break
        return results

    async def search_series(self, query: str) -> list[SourceSeries]:
        query = query.strip()
        wanted = source_title_key(query)
        if not wanted:
            return []
        results = await self._search_catalog(query, MAX_SEARCH_PAGES)
        if any(source_title_key(result.title) == wanted for result in results.values()):
            return list(results.values())

        # Search cards have no aliases. Look them up for small result sets,
        # such as the single result returned for a published abbreviation.
        if len(results) <= MAX_ALIAS_LOOKUPS:
            for result in results.values():
                try:
                    details = await self.get_series_details(result.external_id)
                except httpx.HTTPError as exc:
                    log.warning("Toonily alias lookup failed (%s)", type(exc).__name__)
                    break
                if details:
                    result.alt_titles = details.alt_titles
            if any(
                source_title_key(alias) == wanted
                for result in results.values()
                for alias in result.alt_titles
            ):
                return list(results.values())

        for phrase in _fallback_queries(query):
            try:
                candidates = await self._search_catalog(phrase, 1)
            except httpx.HTTPError as exc:
                log.warning("Toonily fallback search failed (%s)", type(exc).__name__)
                break
            # A shortened query is only a discovery aid. Require the full
            # original title to match before exposing a fallback candidate.
            matches = {
                slug: result for slug, result in candidates.items()
                if source_title_key(result.title) == wanted
            }
            if matches:
                remainder = [result for slug, result in results.items() if slug not in matches]
                return (list(matches.values()) + remainder)[:MAX_SEARCH_RESULTS]
        return list(results.values())

    async def get_series_details(self, external_id: str) -> SourceSeries | None:
        soup = await self._series_html(external_id, retries=0, timeout=8)
        heading = soup.select_one(".post-title h1")
        if heading is None:
            return None
        for badge in heading.select(".manga-title-badges"):
            badge.decompose()
        title = heading.get_text(" ", strip=True)
        if not title:
            return None
        alt_titles = []
        for item in soup.select(".post-content_item"):
            label = item.select_one(".summary-heading h5")
            content = item.select_one(".summary-content")
            if label and content and label.get_text(strip=True).casefold() == "alt name(s)":
                aliases = re.split(r"[,;\n]+", content.get_text("\n", strip=True))
                alt_titles = [
                    alias for alias in unique_titles(aliases)
                    if source_title_key(alias) != source_title_key(title)
                ]
                break
        return SourceSeries(
            self.name, external_id, title, f"{BASE_URL}/serie/{external_id}/",
            alt_titles=alt_titles,
        )

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        soup = await self._series_html(external_id)
        series_path = f"/serie/{external_id}/"
        chapters: dict[float, dict[str, SourceChapter]] = {}
        for link in soup.select(".wp-manga-chapter a[href]"):
            title = link.get_text(" ", strip=True)
            match = _CHAPTER.match(title)
            if not match:
                continue
            try:
                path = _chapter_path(link["href"])
            except ValueError:
                continue
            if not path.startswith(series_path):
                continue
            number = float(match[1])
            chapters.setdefault(number, {}).setdefault(path, SourceChapter(
                source_name=self.name, external_id=path, number=number,
                title=title, url=BASE_URL + path,
            ))

        # Mangarr identifies chapters by number. Distinct URLs sharing a
        # number (such as a multi-episode side story) cannot be reconciled.
        return [
            chapter
            for _, copies in sorted(chapters.items())
            if len(copies) == 1
            for chapter in copies.values()
            if not _SIDE_STORY.search(chapter.title)
        ]

    async def _reader_pages(self, path: str, **kwargs) -> list[_PageURL]:
        reader_url = BASE_URL + path
        content = await rl_get_limited_bytes(
            self._client, reader_url, limit=MAX_READER_BYTES, limiter=_limiter,
            follow_redirects=False, headers={"Accept-Encoding": "identity"}, **kwargs,
        )
        soup = BeautifulSoup(content, "lxml", from_encoding="utf-8")
        label = soup.select_one(".breadcrumb li.active")
        title = label.get_text(" ", strip=True) if label else ""
        chapter = _CHAPTER.match(title)
        if not chapter or _SIDE_STORY.search(title):
            return []
        # Older queued IDs may refer to one of several parts at this number.
        chapter_slug = path.strip("/").rsplit("/", 1)[-1]
        number = float(chapter[1])
        for option in soup.select(".chapter-option[data-value]"):
            if option["data-value"] == chapter_slug:
                continue
            other = _CHAPTER.match(option.get_text(" ", strip=True))
            if other and float(other[1]) == number:
                return []
        pages = {}
        for image in soup.select(".reading-content img.wp-manga-chapter-img"):
            url = str(image.get("data-src") or image.get("data-lazy-src") or image.get("src") or "").strip()
            if not url:
                continue
            try:
                url = urljoin(reader_url, url)
                parsed = urlsplit(url)
            except ValueError:
                return []
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.scheme != "https" or parsed.netloc != "data.tnlycdn.com":
                return []
            pages.setdefault(url, _PageURL(url, reader_url))
        return list(pages.values())

    async def _probe_chapter(self, images: httpx.AsyncClient, path: str) -> bool:
        try:
            pages = await self._reader_pages(path, retries=0, timeout=8)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return False
            raise
        if not pages:
            return False

        page = pages[0]
        if self.image_limiter is not None:
            await self.image_limiter.acquire()
        headers = {
            **_image_headers(page), "Range": f"bytes=0-{IMAGE_PREFIX_BYTES - 1}",
            "Accept-Encoding": "identity",
        }
        async with images.stream("GET", page, headers=headers, follow_redirects=False) as response:
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            if response.status_code not in (200, 206):
                return False
            prefix = bytearray()
            async for chunk in response.aiter_raw():
                prefix.extend(chunk[:IMAGE_PREFIX_BYTES - len(prefix)])
                if len(prefix) == IMAGE_PREFIX_BYTES:
                    break
        return bool(guess_extension(prefix, fallback=""))

    async def verify_chapters(
        self, external_ids: list[str], *, max_checks: int | None = MAX_CHAPTER_CHECKS,
    ) -> set[str]:
        proxy = self.content_proxy_url if self.content_proxy_enabled else None
        if proxy is not None:
            try:
                validate_proxy_url(proxy, "Toonily content proxy")
            except ValueError:
                log.warning("Cannot verify Toonily chapters: invalid content proxy")
                return set()

        available: set[str] = set()
        pending: dict[str, list[str]] = {}
        now = time.monotonic()
        for external_id in dict.fromkeys(external_ids):
            try:
                path = _chapter_path(external_id)
            except ValueError:
                continue
            key = (path, proxy)
            cached = self._availability.get(key)
            if cached and cached[0] > now:
                self._availability.move_to_end(key)
                if cached[1]:
                    available.add(external_id)
            else:
                pending.setdefault(path, []).append(external_id)

        limit = MAX_CHAPTER_CHECKS if max_checks is None else min(max_checks, MAX_CHAPTER_CHECKS)
        if not pending or limit <= 0:
            return available
        try:
            async with asyncio.timeout(PROBE_BATCH_TIMEOUT), httpx.AsyncClient(
                timeout=8, trust_env=False, proxy=proxy,
            ) as images:
                for path in list(pending)[:limit]:
                    key = (path, proxy)
                    result = await self._probe_chapter(images, path)
                    ttl = AVAILABLE_TTL if result else UNAVAILABLE_TTL
                    self._availability[key] = (time.monotonic() + ttl, result)
                    self._availability.move_to_end(key)
                    if len(self._availability) > AVAILABILITY_CACHE_SIZE:
                        self._availability.popitem(last=False)
                    if result:
                        available.update(pending[path])
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError) as exc:
            # Unverified releases stay hidden, and transport failures are not cached.
            log.warning("Toonily availability check stopped (%s)", type(exc).__name__)
        return available

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        return list(await self._reader_pages(_chapter_path(chapter_external_id)))

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        if not isinstance(url, _PageURL):
            raise ValueError("Toonily image is missing its chapter Referer")
        response = await rl_request(
            client, "GET", str(url), limiter=self.image_limiter,
            headers=_image_headers(url), follow_redirects=False,
        )
        response.raise_for_status()
        return response.content


source = ToonilySource()
